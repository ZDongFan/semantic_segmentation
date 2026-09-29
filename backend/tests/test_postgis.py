"""在显式 PostGIS 测试数据库验收事务、版本、并发和下载一致性。"""

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor

import fiona
import pytest

from backend.service import ServiceError
from .conftest import make_draft


def create_scene(service, draft, name="image"):
    project = service.create_project("项目")
    image = add_image(service, project["id"], draft, name)
    return project, image


def add_image(service, project_id, draft, name):
    path = service.settings.storage_root.parent / (name + ".tif")
    path.write_bytes(b"reference")
    image = service.register_image(project_id, name, str(path), None)
    upload = draft.with_name(str(uuid.uuid4()) + ".gpkg")
    shutil.copy2(draft, upload)
    saved = service.save_draft(project_id, image["id"], upload, None)
    image["draft_id"] = saved["draft_id"]
    return image


def publish(service, project, image, base=None, key=None):
    drafts = [(row["id"], row["draft_id"]) for row in service.images(project["id"]) if row["draft_id"]]
    return service.publish(project["id"], drafts, base, key or uuid.uuid4())


def test_versions_carry_other_images_and_pagination(postgis, draft):
    project, first = create_scene(postgis, draft)
    v1 = publish(postgis, project, first)
    second = add_image(postgis, project["id"], draft, "second")
    v2 = publish(postgis, project, second, v1["id"])
    assert v1["image_ids"] == [first["id"]]
    assert set(v2["image_ids"]) == {first["id"], second["id"]}
    page = postgis.features(project["id"], None, 0, 1)
    assert page["version_id"] == v2["id"] and page["next_offset"] == 1
    v3 = publish(postgis, project, first, v2["id"])
    following = postgis.features(project["id"], page["version_id"], 1, 1)
    assert following["version_id"] == v2["id"]
    assert len(following["features"]) == 1
    assert postgis.detail(project["id"])["version"]["id"] == v3["id"]
    old = postgis.download(project["id"], v1["id"])
    assert len(fiona.listlayers(postgis.file_path(old["file_path"]))) == 1
    current = postgis.download(project["id"])
    assert len(fiona.listlayers(postgis.file_path(current["file_path"]))) == 2


def test_idempotency_and_cross_project_version(postgis, draft):
    project, image = create_scene(postgis, draft)
    key = uuid.uuid4()
    first = publish(postgis, project, image, key=key)
    assert publish(postgis, project, image, key=key) == first
    with pytest.raises(ServiceError, match="幂等"):
        publish(postgis, project, image, first["id"], key)
    other = postgis.create_project("其他项目")
    with pytest.raises(ServiceError) as error:
        postgis.detail(other["id"], first["id"])
    assert error.value.status == 404


def test_concurrent_publish_has_one_winner(postgis, draft):
    project, image = create_scene(postgis, draft)

    def attempt():
        try:
            return publish(postgis, project, image)["id"]
        except ServiceError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count(409) == 1
    assert len(postgis.versions(project["id"])) == 1


def test_file_failure_retains_current_version(postgis, draft, monkeypatch):
    project, image = create_scene(postgis, draft)
    first = publish(postgis, project, image)
    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr("backend.service.os.replace", fail)
    with pytest.raises(OSError):
        publish(postgis, project, image, first["id"])
    assert postgis.detail(project["id"])["version"]["id"] == first["id"]
    assert len(postgis.versions(project["id"])) == 1


def test_database_failure_retains_current_version(postgis, draft):
    project, image = create_scene(postgis, draft)
    first = publish(postgis, project, image)
    with postgis.connection() as db:
        db.execute("""CREATE FUNCTION reject_feature() RETURNS trigger LANGUAGE plpgsql AS $$
                      BEGIN RAISE EXCEPTION 'simulated database failure'; END $$""")
        db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON lcc_features
                      FOR EACH ROW EXECUTE FUNCTION reject_feature()""")
    import psycopg
    with pytest.raises(psycopg.Error):
        publish(postgis, project, image, first["id"])
    assert postgis.detail(project["id"])["version"]["id"] == first["id"]


def test_draft_conflict_and_unpublished_visibility(postgis, draft):
    project, image = create_scene(postgis, draft)
    assert postgis.projects() == []
    modified = make_draft(draft.with_name("empty.gpkg"), [])
    with pytest.raises(ServiceError) as error:
        postgis.save_draft(project["id"], image["id"], modified, None)
    assert error.value.status == 409
    assert postgis.draft(project["id"], image["id"])["id"] == image["draft_id"]


def test_publish_all_unpublished_scenes_once(postgis, draft):
    project, first = create_scene(postgis, draft)
    second = add_image(postgis, project["id"], draft, "second")
    result = publish(postgis, project, first)
    assert set(result["image_ids"]) == {first["id"], second["id"]}
    assert len(postgis.versions(project["id"])) == 1
    assert len(fiona.listlayers(postgis.file_path(postgis.download(project["id"])["file_path"]))) == 2


def test_publish_rejects_missing_stale_or_foreign_drafts(postgis, draft):
    project, first = create_scene(postgis, draft)
    second = add_image(postgis, project["id"], draft, "second")
    for refs in ([(first["id"], first["draft_id"])],
                 [(first["id"], uuid.uuid4()), (second["id"], second["draft_id"])],
                 [(uuid.uuid4(), second["draft_id"]) ]):
        with pytest.raises(ServiceError) as exc:
            postgis.publish(project["id"], refs, None, uuid.uuid4())
        assert exc.value.status == 409
    assert postgis.projects() == []


def test_publish_empty_and_duplicate_references(postgis, draft):
    project, first = create_scene(postgis, draft)
    item = (first["id"], first["draft_id"])
    for refs in ([], [item, item]):
        with pytest.raises(ServiceError) as exc:
            postgis.publish(project["id"], refs, None, uuid.uuid4())
        assert exc.value.status == 422


def test_dem_can_change_without_discarding_draft(postgis, draft):
    project, image = create_scene(postgis, draft)
    first = publish(postgis, project, image)
    dem = postgis.settings.storage_root.parent / "dem.tif"
    dem.write_bytes(b"dem")
    updated = postgis.register_image(project["id"], image["name"], image["image_path"], str(dem))
    assert updated["id"] == image["id"]
    assert updated["draft_id"] == image["draft_id"]
    assert updated["dem_path"] == str(dem)
    assert postgis.detail(project["id"])["version"]["id"] == first["id"]
