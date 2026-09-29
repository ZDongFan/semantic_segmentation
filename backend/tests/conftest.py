"""后端合成数据与可选真实 PostGIS 测试环境。"""

import os
import uuid
from dataclasses import replace

import fiona
import pytest

from backend.config import Settings
from backend.service import Service


@pytest.fixture
def settings(tmp_path):
    return Settings("postgresql://unused", tmp_path / "storage", "p" * 32)


def make_draft(path, geometries=None, crs="EPSG:32650"):
    """生成含孔洞与多面的固定草稿契约。"""
    if geometries is None:
        geometries = [{"type": "MultiPolygon", "coordinates": [
            [[[500000, 3000000], [500100, 3000000], [500100, 3000100],
              [500000, 3000100], [500000, 3000000]],
             [[500020, 3000020], [500080, 3000020], [500080, 3000080],
              [500020, 3000080], [500020, 3000020]]],
            [[[500200, 3000000], [500220, 3000000], [500220, 3000020],
              [500200, 3000020], [500200, 3000000]]],
        ]}]
    schema = {"geometry": "MultiPolygon", "properties": {
        "feature_uuid": "str", "class_id": "int", "class_name": "str",
        "source_id": "int", "origin": "str", "run_id": "str"}}
    with fiona.open(path, "w", driver="GPKG", layer="draft", crs=crs, schema=schema) as dst:
        for index, geom in enumerate(geometries):
            dst.write({"geometry": geom, "properties": {
                "feature_uuid": str(uuid.uuid4()), "class_id": 1, "class_name": "landslide",
                "source_id": index + 1, "origin": "user", "run_id": None}})
    return path


@pytest.fixture
def draft(tmp_path):
    return make_draft(tmp_path / "draft.gpkg")


@pytest.fixture
def postgis(settings):
    """仅在显式测试数据库中创建随机 schema，不修改生产数据。"""
    url = os.environ.get("LCC_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未提供 LCC_TEST_DATABASE_URL，真实 PostGIS 事务测试未执行")
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg import sql
    schema = "lcc_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    service = Service(replace(settings, database_url=make_conninfo(
        url, options="-c search_path=" + schema + ",public")))
    try:
        service.initialize()
        yield service
    finally:
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
