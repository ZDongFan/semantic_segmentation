# -*- coding: utf-8 -*-
"""列表与单个滑坡详情共用的属性计算和显示精度。"""

from qgis.core import (NULL, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsDistanceArea, QgsProject)

from .result_attributes import build_result_attributes


COMMON_FIELDS = (("class_name", "类别"), ("mj", "面积"), ("perimeter_m", "周长"),
                 ("center_lon", "中心经度"), ("center_lat", "中心纬度"),
                 ("ztz", "制图者"), ("source_id", "来源编号"))


class FeatureAttributes:
    """在当前图层 CRS 下复用椭球测量与坐标转换器。"""

    def __init__(self, layer, context):
        if not layer.crs().isValid():
            raise ValueError("草稿缺少有效 CRS，无法计算成果面积。")
        self.context = context
        self.distance = QgsDistanceArea()
        self.distance.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
        self.distance.setEllipsoid('WGS84')
        self.converter = QgsCoordinateTransform(
            layer.crs(), QgsCoordinateReferenceSystem(4326), QgsProject.instance())

    def values(self, feature):
        """按当前几何计算公开属性，不写回草稿。"""
        geometry = feature.geometry()
        if geometry.isEmpty():
            raise ValueError("滑坡几何为空。")
        center = self.converter.transform(geometry.centroid().asPoint())
        values = build_result_attributes(**self.context, mj=self.distance.measureArea(geometry))
        values.update(feature_id=str(feature['feature_uuid']),
                      **{name: feature[name] for name in ('class_id', 'class_name', 'source_id')},
                      perimeter_m=self.distance.measurePerimeter(geometry),
                      center_lon=center.x(), center_lat=center.y())
        return values


def format_attribute(name, value, *, detail=False):
    """保留底层值，仅调整显示精度、单位与空值提示。"""
    if value is None or value == NULL:
        return '未填写' if detail else ''
    if name in ('mj', 'perimeter_m'):
        text = '{:,.3f}'.format(value) if detail else '{:.3f}'.format(value)
        return text + (' m²' if name == 'mj' else ' m') if detail else text
    if name in ('center_lon', 'center_lat'):
        return '{:.7f}'.format(value) + ('°' if detail else '')
    return str(value)
