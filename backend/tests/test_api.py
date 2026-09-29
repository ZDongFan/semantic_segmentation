"""验证应用隔离、参数边界、认证和信息隐藏。"""

import uuid
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from backend.api import create_app
from backend.service import Service, ServiceError


def test_keys_and_routes_are_separate(settings):
    service = Mock()
    service.projects.return_value = []
    internal = TestClient(create_app(True, settings, service))
    public = TestClient(create_app(False, settings, service))
    assert public.get("/projects").status_code == 401
    assert public.get("/projects", headers={"X-API-Key": "invalid"}).status_code == 401
    assert public.get("/projects", headers={"X-API-Key": settings.public_key}).status_code == 200
    assert internal.get("/internal/projects").status_code == 200
    assert internal.get("/internal/projects", headers={"X-API-Key": "invalid"}).status_code == 200
    assert public.post("/internal/projects", headers={"X-API-Key": settings.public_key},
                       json={"name": "秘密"}).status_code == 404
    assert public.post("/projects", headers={"X-API-Key": settings.public_key},
                       json={"name": "秘密"}).status_code == 405
    assert public.get("/openapi.json").status_code == 404
    assert {route.path for route in public.app.routes} == {
        "/projects", "/projects/{project_id}", "/projects/{project_id}/versions",
        "/projects/{project_id}/features", "/projects/{project_id}/download"}


def test_validation_and_confirmation(settings):
    service = Mock()
    client = TestClient(create_app(True, settings, service),
                        headers={"X-API-Key": "invalid"})
    assert client.post("/internal/projects", json={"name": "  "}).status_code == 422
    payload = {"drafts": [{"image_id": str(uuid.uuid4()), "draft_id": str(uuid.uuid4())}],
               "base_version": None, "idempotency_key": str(uuid.uuid4()), "confirmed": False}
    response = client.post("/internal/projects/{}/publish".format(uuid.uuid4()), json=payload)
    assert response.status_code == 422
    service.publish.assert_not_called()


def test_following_page_requires_version(settings):
    service = Service(settings)
    with pytest.raises(ServiceError) as error:
        service.features(uuid.uuid4(), None, 100, 100)
    assert error.value.status == 422


def test_query_limits(settings):
    client = TestClient(create_app(False, settings, Mock()),
                        headers={"X-API-Key": settings.public_key})
    path = "/projects/{}/features".format(uuid.uuid4())
    assert client.get(path + "?limit=1001").status_code == 422
    assert client.get(path + "?offset=-1").status_code == 422
    assert client.get(path + "?version_id=invalid").status_code == 422


def test_errors_do_not_expose_internal_paths(settings):
    service = Mock()
    service.projects.side_effect = RuntimeError("secret-path-password")
    client = TestClient(create_app(False, settings, service), raise_server_exceptions=False)
    response = client.get("/projects", headers={"X-API-Key": settings.public_key})
    assert response.status_code == 500
    assert "secret-path-password" not in response.text


def test_inputs_accept_arbitrary_directories_and_storage_stays_confined(settings, tmp_path):
    service = Service(settings)
    for directory in (tmp_path / "data", tmp_path / "another" / "nested"):
        directory.mkdir(parents=True)
        source = directory / "input.tif"
        source.write_bytes(b"data")
        assert service.input_path(str(source)) == str(source.resolve())
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    with pytest.raises(ServiceError):
        service.file_path("../private.txt")


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_input_rejects_unusable_file(settings, tmp_path, monkeypatch, kind):
    from pathlib import Path
    source = tmp_path / "input.tif"
    if kind == "directory":
        source.mkdir()
    elif kind == "unreadable":
        source.write_bytes(b"data")
        # 模拟服务账户没有读权限，不依赖测试进程的管理员身份。
        def denied(*args, **kwargs):
            raise PermissionError("拒绝访问")
        monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ServiceError) as error:
        Service(settings).input_path(str(source))
    assert error.value.status == 422



def test_upload_limit_and_cleanup(settings):
    from dataclasses import replace
    settings = replace(settings, max_upload_bytes=10)
    client = TestClient(create_app(True, settings),
                        headers={"X-API-Key": "invalid"})
    response = client.put("/internal/projects/{}/images/{}/draft".format(uuid.uuid4(), uuid.uuid4()),
                          content=b"x" * 11)
    assert response.status_code == 413
    assert list((settings.storage_root / "uploads").iterdir()) == []
