# -*- coding: utf-8 -*-
"""会话草稿的终态导出服务。"""

from __future__ import annotations

import os
import tempfile

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .draft_session import (
    FIELD_CLASS_ID,
    FIELD_CLASS_NAME,
    FIELD_SOURCE_ID,
    LANDSLIDE_CLASS_ID,
    LANDSLIDE_CLASS_NAME,
    layer_uri,
)
from .pytorch_inference_core import GEOTIFF_CREATION_OPTIONS


OUTPUT_FORMAT_RASTER = "raster"
OUTPUT_FORMAT_VECTOR = "vector"
OUTPUT_FORMAT_DXF = "dxf"


class ExportService:
    """把当前 generation 导出为只含公开字段的终态成果。"""

    def export(self, session, output_format, path):
        """导出当前草稿，并在成功后由调用方标记会话已导出。"""
        if session is None or not session.is_active:
            raise IOError("当前没有可导出的草稿会话。")
        if output_format == OUTPUT_FORMAT_RASTER:
            self.export_raster(session, path)
        elif output_format == OUTPUT_FORMAT_VECTOR:
            self.export_vector(session, path)
        elif output_format == OUTPUT_FORMAT_DXF:
            self.export_dxf(session, path)
        else:
            raise ValueError("不支持的导出格式: {}".format(output_format))

    def export_vector(self, session, path):
        """dissolve 后拆分独立斑块，再导出公开属性字段。"""
        parts = self._dissolved_parts(session)
        layer = QgsVectorLayer(parts, "final_parts", "ogr")
        if not layer.isValid():
            raise IOError("无法读取草稿导出结果。")
        fields = self._public_fields()
        writer = self._writer(path, layer.crs(), QgsWkbTypes.MultiPolygon,
                              "ESRI Shapefile", fields)
        try:
            source_id = 1
            for feature in layer.getFeatures():
                geometry = feature.geometry()
                if geometry is None or geometry.isEmpty():
                    continue
                output = QgsFeature(fields)
                output.setGeometry(geometry.convertToType(
                    QgsWkbTypes.PolygonGeometry, True))
                output.setAttributes([
                    LANDSLIDE_CLASS_ID, LANDSLIDE_CLASS_NAME, source_id,
                ])
                if not writer.addFeature(output):
                    raise IOError("写入 Shapefile 要素失败。")
                source_id += 1
        finally:
            del writer

    def export_dxf(self, session, path):
        """按与 Shapefile 一致的连通斑块导出闭合外边界。"""
        parts = self._dissolved_parts(session)
        layer = QgsVectorLayer(parts, "dxf_parts", "ogr")
        if not layer.isValid():
            raise IOError("无法读取草稿 DXF 导出结果。")
        fields = QgsFields()
        fields.append(QgsField("Layer", QVariant.String, "", 80))
        writer = self._writer(path, layer.crs(), QgsWkbTypes.LineString, "DXF",
                              fields)
        count = 0
        try:
            for feature in layer.getFeatures():
                for polygon in self._polygon_parts(feature.geometry()):
                    if not polygon or not polygon[0]:
                        continue
                    line = self._closed_line(polygon[0])
                    if line is None or line.isEmpty():
                        continue
                    output = QgsFeature(fields)
                    output.setGeometry(line)
                    output.setAttribute("Layer", LANDSLIDE_CLASS_NAME)
                    if not writer.addFeature(output):
                        raise IOError("写入 DXF 要素失败。")
                    count += 1
        finally:
            del writer
        if count == 0:
            raise IOError("草稿层没有可导出的边界线。")
        self._fit_dxf_initial_view(path, layer.extent())

    def export_raster(self, session, path):
        """按照参考影像网格写出 0/1 二值 tiled BigTIFF。"""
        from osgeo import gdal

        template = gdal.Open(session.input_path)
        if template is None:
            raise IOError("无法打开工作影像: {}".format(session.input_path))
        driver = gdal.GetDriverByName("GTiff")
        destination = driver.Create(
            path, template.RasterXSize, template.RasterYSize, 1, gdal.GDT_Byte,
            list(GEOTIFF_CREATION_OPTIONS))
        if destination is None:
            template = None
            raise IOError("无法创建输出栅格: {}".format(path))
        destination.SetGeoTransform(template.GetGeoTransform())
        if template.GetProjection():
            destination.SetProjection(template.GetProjection())
        band = destination.GetRasterBand(1)
        band.Fill(0)
        source = gdal.OpenEx(session.generation_path, gdal.OF_VECTOR)
        if source is None:
            destination = None
            template = None
            raise IOError("无法打开草稿 generation。")
        result = gdal.RasterizeLayer(
            destination, [1], source.GetLayer(0), burn_values=[1])
        source = None
        destination.FlushCache()
        destination = None
        template = None
        if result != 0:
            raise IOError("草稿栅格化失败。")

    def _dissolved_parts(self, session):
        import processing

        workdir = session.directory
        dissolved = os.path.join(workdir, "export_dissolved.gpkg")
        parts = os.path.join(workdir, "export_parts.gpkg")
        processing.run("native:dissolve", {
            "INPUT": layer_uri(session.generation_path),
            "FIELD": [],
            "SEPARATE_DISJOINT": False,
            "OUTPUT": dissolved,
        })
        return processing.run("native:multiparttosingleparts", {
            "INPUT": dissolved,
            "OUTPUT": parts,
        })["OUTPUT"]

    def _public_fields(self):
        """返回最终成果允许写出的公开字段。"""
        fields = QgsFields()
        fields.append(QgsField(FIELD_CLASS_ID, QVariant.Int))
        fields.append(QgsField(FIELD_CLASS_NAME, QVariant.String, "", 80))
        fields.append(QgsField(FIELD_SOURCE_ID, QVariant.Int))
        return fields

    def _writer(self, path, crs, wkb_type, driver_name, fields=None):
        out_dir = os.path.dirname(path) or "."
        os.makedirs(out_dir, exist_ok=True)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver_name
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        public_fields = fields or self._public_fields()
        writer = QgsVectorFileWriter.create(
            path, public_fields, wkb_type, crs,
            QgsProject.instance().transformContext(), options)
        if writer.hasError() != QgsVectorFileWriter.NoError:
            message = writer.errorMessage()
            del writer
            raise IOError("创建 {} 失败: {}".format(driver_name, message))
        return writer

    def _polygon_parts(self, geometry):
        if geometry is None or geometry.isEmpty():
            return []
        if QgsWkbTypes.isMultiType(geometry.wkbType()):
            return geometry.asMultiPolygon()
        polygon = geometry.asPolygon()
        return [polygon] if polygon else []

    def _closed_line(self, ring):
        points = list(ring)
        if len(points) < 3:
            return QgsGeometry()
        if points[0] != points[-1]:
            points.append(points[0])
        return QgsGeometry.fromPolylineXY(points)

    def _fit_dxf_initial_view(self, path, extent):
        if extent is None or extent.isEmpty():
            return
        xmin, ymin = extent.xMinimum(), extent.yMinimum()
        xmax, ymax = extent.xMaximum(), extent.yMaximum()
        width, height = max(xmax - xmin, 1.0), max(ymax - ymin, 1.0)
        padding = max(width, height) * 0.02
        headers = {
            "$EXTMIN": {"10": xmin, "20": ymin},
            "$EXTMAX": {"10": xmax, "20": ymax},
            "$LIMMIN": {"10": xmin - padding, "20": ymin - padding},
            "$LIMMAX": {"10": xmax + padding, "20": ymax + padding},
        }
        vport = {
            "12": (xmin + xmax) / 2.0,
            "22": (ymin + ymax) / 2.0,
            "40": height + padding * 2,
            "41": max((width + padding * 2) / (height + padding * 2), 0.001),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix="lcc_dxf_", suffix=".tmp", dir=os.path.dirname(path) or ".")
        os.close(descriptor)
        try:
            self._rewrite_dxf_view_streaming(path, temporary, headers, vport)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def _rewrite_dxf_view_streaming(self, source_path, output_path, headers,
                                    active_vport_values):
        current_header = None
        entity = []
        buffering_vport = False
        with open(source_path, "r", encoding="utf-8", errors="replace") as source, \
                open(output_path, "w", encoding="utf-8", newline="\n") as output:
            while True:
                code_line = source.readline()
                if not code_line:
                    break
                value_line = source.readline()
                if not value_line:
                    output.write(code_line.rstrip("\r\n") + "\n")
                    break
                pair = [code_line.rstrip("\r\n"), value_line.rstrip("\r\n")]
                code, value = pair[0].strip(), pair[1].strip()
                if buffering_vport and code == "0":
                    self._write_dxf_entity(output, entity, active_vport_values)
                    entity, buffering_vport = [], False
                if not buffering_vport and code == "0" and value == "VPORT":
                    buffering_vport, entity = True, pair
                    continue
                if buffering_vport:
                    entity.extend(pair)
                    continue
                if code == "9":
                    current_header = value if value in headers else None
                elif current_header and code in headers[current_header]:
                    pair[1] = self._format_float(headers[current_header][code])
                output.write(pair[0] + "\n" + pair[1] + "\n")
            if buffering_vport:
                self._write_dxf_entity(output, entity, active_vport_values)

    def _write_dxf_entity(self, output, values, replacement):
        active = any(
            values[index].strip() == "2" and values[index + 1].strip() == "*Active"
            for index in range(0, len(values) - 1, 2))
        if active:
            for code, value in replacement.items():
                for index in range(0, len(values) - 1, 2):
                    if values[index].strip() == code:
                        values[index + 1] = self._format_float(value)
                        break
        output.write("\n".join(values) + "\n")

    def _format_float(self, value):
        return "{:.12g}".format(float(value))
