"""验证最终几何的测地线属性和完整 GeoPackage。"""

import uuid

import fiona
import pytest
from shapely.geometry import MultiPolygon, Polygon, mapping

from backend.geometry import final_geometry, validate_draft, write_scene
from .conftest import make_draft


def test_holes_and_multipart_metrics(draft):
    with fiona.open(draft, layer="draft") as src:
        geometry = next(iter(src)).geometry
        polygon, _, metrics = final_geometry(geometry, src.crs_wkt)
    assert len(polygon.geoms) == 2
    assert len(polygon.geoms[0].interiors) == 1
    assert metrics["area_m2"] == pytest.approx(6800, rel=0.002)
    assert metrics["perimeter_m"] == pytest.approx(720, rel=0.002)
    assert 116 < metrics["center_lon"] < 118
    assert 26 < metrics["center_lat"] < 28


def test_reject_invalid_geometry():
    polygon = Polygon([(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)])
    with pytest.raises(ValueError, match="有效"):
        final_geometry(mapping(polygon), "EPSG:4326")


def test_reject_out_of_bounds():
    polygon = Polygon([(200, 0), (201, 0), (201, 1), (200, 1), (200, 0)])
    with pytest.raises(ValueError, match="范围"):
        final_geometry(mapping(polygon), "EPSG:4326")


def test_geopackage_has_independent_scene_layers(tmp_path, draft):
    output = tmp_path / "project.gpkg"
    project, version = uuid.uuid4(), uuid.uuid4()
    images = [uuid.uuid4(), uuid.uuid4()]
    for image in images:
        rows = list(write_scene(output, draft, project, image, version, "2026-09-22T00:00:00Z"))
        assert rows[0][1]["run_id"] is None
        assert rows[0][1]["version_id"] == str(version)
    assert set(fiona.listlayers(output)) == {"image_" + image.hex for image in images}
    for image in images:
        with fiona.open(output, layer="image_" + image.hex) as src:
            assert src.crs.to_epsg() == 32650
            feature = next(iter(src))
            assert feature.properties["image_id"] == str(image)
            assert "origin" not in src.schema["properties"]
            assert len(feature.geometry.coordinates) == 2


def test_empty_draft_is_valid(tmp_path):
    path = make_draft(tmp_path / "empty.gpkg", [])
    assert validate_draft(path) == 0
    output = tmp_path / "empty_result.gpkg"
    assert list(write_scene(output, path, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "now")) == []
    assert len(fiona.listlayers(output)) == 1


def test_final_edit_changes_metrics():
    small = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    large = Polygon([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])
    a = final_geometry(mapping(small), "EPSG:4326")[2]
    b = final_geometry(mapping(large), "EPSG:4326")[2]
    assert b["area_m2"] > 3.9 * a["area_m2"]
    assert b["center_lon"] != a["center_lon"]
