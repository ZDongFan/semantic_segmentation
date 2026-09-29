"""逐要素校验最终几何，生成测地线属性和标准成果。"""

import math
import uuid

import fiona
from pyproj import CRS, Geod, Transformer
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import transform

GEOD = Geod(ellps="WGS84")
DRAFT_FIELDS = {"feature_uuid", "class_id", "class_name", "source_id", "origin", "run_id"}
OUTPUT_SCHEMA = {
    "geometry": "MultiPolygon",
    "properties": {
        "feature_id": "str", "class_id": "int", "class_name": "str",
        "source_id": "int", "project_id": "str", "image_id": "str",
        "version_id": "str", "run_id": "str", "created_at": "str",
        "area_m2": "float", "perimeter_m": "float",
        "center_lon": "float", "center_lat": "float",
    },
}


def final_geometry(geometry, crs):
    """保留孔洞和多面；周长包含外环和内环。"""
    polygon = shape(geometry)
    if polygon.geom_type == "Polygon":
        polygon = MultiPolygon([polygon])
    if polygon.geom_type != "MultiPolygon" or polygon.is_empty or not polygon.is_valid:
        raise ValueError("成果必须为有效且非空的 Polygon / MultiPolygon。")
    if polygon.has_z:
        raise ValueError("首期成果仅接受二维几何。")
    source_crs = CRS.from_user_input(crs)
    if not (source_crs.is_projected or source_crs.is_geographic):
        raise ValueError("成果 CRS 必须为投影或地理坐标系。")
    converter = Transformer.from_crs(source_crs, 4326, always_xy=True)
    geographic = transform(converter.transform, polygon)
    xmin, ymin, xmax, ymax = geographic.bounds
    if (not all(math.isfinite(v) for v in geographic.bounds)
            or xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90
            or xmax - xmin > 180 or not geographic.is_valid):
        raise ValueError("几何超出有效经纬度范围，或跨越日期变更线，需先分割处理。")
    area = perimeter = 0.0
    for part in geographic.geoms:
        part = orient(part, sign=1.0)
        part_area, _ = GEOD.geometry_area_perimeter(part)
        area += abs(part_area)
        perimeter += GEOD.geometry_length(part.exterior)
        perimeter += sum(GEOD.geometry_length(ring) for ring in part.interiors)
    center = transform(converter.transform, polygon.centroid)
    if area <= 0 or not all(math.isfinite(v) for v in (area, perimeter, center.x, center.y)):
        raise ValueError("无法计算有效的测地线属性。")
    return polygon, geographic, {
        "area_m2": area, "perimeter_m": perimeter,
        "center_lon": center.x, "center_lat": center.y,
    }


def draft_features(path):
    """仅接受固定 draft 图层，禁止驱动自动识别其他数据源。"""
    with fiona.open(path, layer="draft", enabled_drivers=["GPKG"]) as source:
        if not source.crs_wkt or not DRAFT_FIELDS.issubset(source.schema["properties"]):
            raise ValueError("草稿缺少 CRS 或固定字段。")
        seen = set()
        for feature in source:
            props = dict(feature.properties)
            feature_id = str(uuid.UUID(str(props["feature_uuid"])))
            if feature_id in seen:
                raise ValueError("草稿 feature_uuid 重复。")
            seen.add(feature_id)
            if (props["class_id"] != 1 or props["class_name"] != "landslide"
                    or props["origin"] not in ("user", "inference")):
                raise ValueError("草稿分类或来源不符合契约。")
            polygon, geographic, metrics = final_geometry(feature.geometry, source.crs_wkt)
            yield source.crs_wkt, feature_id, props, polygon, geographic, metrics


def validate_draft(path):
    """完整遍历并验证，空草稿允许持久化和发布。"""
    return sum(1 for _ in draft_features(path))


def write_scene(output, draft, project_id, image_id, version_id, created_at):
    """按影像建立独立图层，逐条返回与文件完全一致的数据库记录。"""
    with fiona.open(draft, layer="draft", enabled_drivers=["GPKG"]) as source:
        crs_wkt = source.crs_wkt
    with fiona.open(output, "w", driver="GPKG", layer="image_" + image_id.hex,
                    crs_wkt=crs_wkt, schema=OUTPUT_SCHEMA) as destination:
        for _, feature_id, props, polygon, geographic, metrics in draft_features(draft):
            attributes = {
                "feature_id": feature_id, "class_id": 1, "class_name": "landslide",
                "source_id": props["source_id"], "project_id": str(project_id),
                "image_id": str(image_id), "version_id": str(version_id),
                "run_id": props["run_id"] or None, "created_at": created_at,
                **metrics,
            }
            destination.write({"geometry": mapping(polygon), "properties": attributes})
            yield feature_id, attributes, geographic.wkb
