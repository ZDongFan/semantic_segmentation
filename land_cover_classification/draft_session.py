# -*- coding: utf-8 -*-
"""以磁盘 generation 管理当前工作草稿与融合。"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid

from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsVectorLayer, QgsWkbTypes

FIELD_CLASS_ID = "class_id"
FIELD_CLASS_NAME = "class_name"
FIELD_SOURCE_ID = "source_id"
FIELD_FEATURE_UUID = "feature_uuid"
FIELD_ORIGIN = "origin"
FIELD_RUN_ID = "run_id"
ORIGIN_USER = "user"
ORIGIN_INFERENCE = "inference"
LANDSLIDE_CLASS_ID = 1
LANDSLIDE_CLASS_NAME = "landslide"
LAYER_NAME = "draft"

def layer_uri(path):
    """返回统一的 GeoPackage 图层 URI。"""
    return "{}|layername={}".format(path, LAYER_NAME)

class DraftSession:
    """仅在当前插件会话有效的草稿 generation。"""

    def __init__(self):
        self.directory = None
        self.input_path = None
        self.generation_path = None
        self.generation_index = 0
        self.layer = None
        self.exported_generation = None
        self.fusion_snapshot_path = None
        self.edit_snapshot_path = None
        self.is_replacing = False
        self.last_postprocess_path = None

    @property
    def is_active(self):
        return bool(self.generation_path and os.path.isfile(self.generation_path))

    @property
    def is_dirty(self):
        return self.is_active and self.generation_path != self.exported_generation

    def begin(self, input_path, create_empty):
        """为工作影像创建新的空草稿会话。"""
        self.close()
        self.directory = tempfile.mkdtemp(prefix="lcc_draft_session_")
        self.input_path = os.path.abspath(input_path)
        self.generation_index = 0
        path = self._generation_path(self.generation_index)
        create_empty(path, self.input_path)
        self.generation_path = path
        self.exported_generation = path

        return path

    def next_generation_path(self, prefix="generation"):
        """分配尚未写入的下一代草稿文件名。"""
        self.generation_index += 1
        return self._generation_path(self.generation_index, prefix)

    def activate_generation(self, path, layer=None):
        """在校验完成后原子地把可见图层切换到新 generation。"""
        if not os.path.isfile(path):
            raise IOError("草稿 generation 不存在: {}".format(path))
        previous_path = self.generation_path
        previous_layer = self.layer
        target_layer = layer if layer is not None else self.layer
        if target_layer is None:
            self.generation_path = path
            self.layer = layer
            return

        previous_source = None
        previous_name = None
        previous_provider = "ogr"
        try:
            previous_source = target_layer.source()
            previous_name = target_layer.name()
            previous_provider = target_layer.providerType() or "ogr"
        except Exception:
            pass

        self.is_replacing = True
        try:
            result = target_layer.setDataSource(
                layer_uri(path), target_layer.name(), "ogr")
            if result is False or not target_layer.isValid():
                raise IOError("无法切换草稿 generation: {}".format(path))
            target_layer.reload()
            target_layer.triggerRepaint()
        except Exception:
            # setDataSource 可能已经修改了 QgsVectorLayer，失败时尽量恢复旧数据源。
            if previous_source:
                try:
                    target_layer.setDataSource(
                        previous_source,
                        previous_name or target_layer.name(),
                        previous_provider,
                    )
                    if target_layer.isValid():
                        target_layer.reload()
                        target_layer.triggerRepaint()
                except Exception:
                    pass
            self.generation_path = previous_path
            self.layer = previous_layer
            raise
        finally:
            self.is_replacing = False

        self.generation_path = path
        self.layer = target_layer

    def mark_exported(self):
        """记录成功导出的 generation。"""
        self.exported_generation = self.generation_path

    def begin_edit_snapshot(self):
        """编辑前保存可回退的 generation 快照。"""
        if not self.is_active:
            return None
        path = os.path.join(self.directory, "edit_snapshot_{}.gpkg".format(
            uuid.uuid4().hex))
        shutil.copy2(self.generation_path, path)
        self.edit_snapshot_path = path
        return path

    def clear_edit_snapshot(self):
        """删除已使用的编辑快照。"""
        path = self.edit_snapshot_path
        self.edit_snapshot_path = None
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def close(self):
        """销毁会话临时文件；关闭插件后草稿不恢复。"""
        self.layer = None
        self.generation_path = None
        self.input_path = None
        self.exported_generation = None
        self.edit_snapshot_path = None
        self.fusion_snapshot_path = None

        directory = self.directory
        if directory and os.path.isdir(directory):
            shutil.rmtree(directory, ignore_errors=True)
        self.directory = None
        return directory if directory and os.path.isdir(directory) else None

    def _generation_path(self, index, prefix="generation"):
        return os.path.join(self.directory, "{}_{:04d}.gpkg".format(prefix, index))

class DraftComposer:
    """使用磁盘型 QGIS Processing 管线合成用户与推理草稿。"""

    def create_empty(self, path, reference_path=None):
        """创建固定字段和 MultiPolygon 类型的空 GeoPackage。"""
        from osgeo import ogr, osr

        driver = ogr.GetDriverByName("GPKG")
        if os.path.exists(path):
            driver.DeleteDataSource(path)
        spatial_ref = self._reference_spatial_ref(reference_path, osr)
        dataset = driver.CreateDataSource(path)
        if dataset is None:
            raise IOError("无法创建草稿 GeoPackage: {}".format(path))
        layer = dataset.CreateLayer(LAYER_NAME, srs=spatial_ref,
                                    geom_type=ogr.wkbMultiPolygon)
        if layer is None:
            dataset = None
            raise IOError("无法创建草稿图层: {}".format(path))
        self._create_fields(layer, ogr)
        dataset = None

    def create_inference_candidate(self, label_path, output_path,
                                   label_class_id, run_id):
        """使用 Processing 从类别 GeoTIFF 提取 landslide 候选。"""
        import processing

        result = processing.run("gdal:polygonize", {
            "INPUT": label_path,
            "BAND": 1,
            "FIELD": "label_value",
            "EIGHT_CONNECTEDNESS": False,
            "EXTRA": "",
            "OUTPUT": output_path,
        })
        candidate_path = result.get("OUTPUT")
        if not candidate_path or not os.path.isfile(candidate_path):
            raise IOError("推理候选矢量化失败。")
        layer = QgsVectorLayer(candidate_path, "inference_candidate", "ogr")
        if not layer.isValid():
            raise IOError("无法加载推理候选矢量。")
        required_fields = [
            QgsField(FIELD_CLASS_ID, QVariant.Int),
            QgsField(FIELD_CLASS_NAME, QVariant.String),
            QgsField(FIELD_SOURCE_ID, QVariant.Int),
            QgsField(FIELD_FEATURE_UUID, QVariant.String),
            QgsField(FIELD_ORIGIN, QVariant.String),
            QgsField(FIELD_RUN_ID, QVariant.String),
        ]
        provider = layer.dataProvider()
        existing = {field.name() for field in layer.fields()}
        missing = [field for field in required_fields
                   if field.name() not in existing]
        if missing and not provider.addAttributes(missing):
            raise IOError("无法创建推理候选字段。")
        layer.updateFields()
        fields = {field.name(): index for index, field in
                  enumerate(layer.fields())}
        if "label_value" not in fields or not layer.startEditing():
            raise IOError("无法编辑推理候选矢量。")
        source_id = 1
        for feature in layer.getFeatures():
            if feature["label_value"] != int(label_class_id):
                layer.deleteFeature(feature.id())
                continue
            layer.changeAttributeValue(feature.id(), fields[FIELD_CLASS_ID],
                                       LANDSLIDE_CLASS_ID)
            layer.changeAttributeValue(feature.id(), fields[FIELD_CLASS_NAME],
                                       LANDSLIDE_CLASS_NAME)
            layer.changeAttributeValue(feature.id(), fields[FIELD_SOURCE_ID],
                                       source_id)
            layer.changeAttributeValue(feature.id(), fields[FIELD_FEATURE_UUID],
                                       uuid.uuid4().hex)
            layer.changeAttributeValue(feature.id(), fields[FIELD_ORIGIN],
                                       ORIGIN_INFERENCE)
            layer.changeAttributeValue(feature.id(), fields[FIELD_RUN_ID],
                                       str(run_id))
            source_id += 1
        if not layer.commitChanges():
            errors = "; ".join(layer.commitErrors())
            layer.rollBack()
            raise IOError("保存推理候选失败: {}".format(errors))

    def append_user_geometry(self, session, geometry):
        """将 AI mask 作为用户区域追加，并裁掉重叠的推理区域。"""
        candidate = session.next_generation_path("ai_candidate")
        self.create_empty(candidate, session.input_path)
        layer = QgsVectorLayer(layer_uri(candidate), "ai_candidate", "ogr")
        if not layer.isValid() or not layer.startEditing():
            raise IOError("无法创建 AI 草稿候选图层。")
        feature = QgsFeature(layer.fields())
        feature.setGeometry(self._as_multipolygon(geometry))
        feature.setAttribute(FIELD_CLASS_ID, LANDSLIDE_CLASS_ID)
        feature.setAttribute(FIELD_CLASS_NAME, LANDSLIDE_CLASS_NAME)
        feature.setAttribute(FIELD_SOURCE_ID, 1)
        feature.setAttribute(FIELD_FEATURE_UUID, uuid.uuid4().hex)
        feature.setAttribute(FIELD_ORIGIN, ORIGIN_USER)
        feature.setAttribute(FIELD_RUN_ID, None)
        if not layer.addFeature(feature) or not layer.commitChanges():
            layer.rollBack()
            raise IOError("无法写入 AI 草稿候选。")
        return self.compose(session, candidate, replace_inference=False)

    def build_inference_candidate(self, session, label_path, label_class_id,
                                  run_id):
        """矢量化最新推理栅格，供融合失败后原样重试。"""
        candidate = session.next_generation_path("inference_candidate")
        self.create_inference_candidate(label_path, candidate, label_class_id,
                                        run_id)
        return candidate

    def merge_inference_candidate(self, session, candidate_path, run_id):
        """把已保存的推理候选合成到当前草稿。"""
        if not candidate_path or not os.path.isfile(candidate_path):
            raise IOError("推理候选不存在: {}".format(candidate_path))
        return self.compose(session, candidate_path, replace_inference=True,
                            run_id=run_id)

    def merge_inference(self, session, label_path, label_class_id, run_id):
        """用最新推理替换旧 inference，同时保留 user 区域。"""
        candidate = self.build_inference_candidate(
            session, label_path, label_class_id, run_id)
        return self.merge_inference_candidate(session, candidate, run_id)

    def compose_current(self, session):
        """将 QGIS 原生编辑后的当前文件重新规范化为新 generation。"""
        return self.compose(session, None, replace_inference=False)

    def compose(self, session, candidate_path, replace_inference, run_id=None):
        """按“用户区域 ∪ (推理区域 - 用户区域)”生成并验证新 generation。"""
        if not session.is_active:
            raise RuntimeError("当前没有可合成的草稿会话。")
        workspace = session.directory
        user_current = self._extract_origin(session.generation_path, ORIGIN_USER,
                                            workspace, "users_current")
        inference_current = self._extract_origin(
            session.generation_path, ORIGIN_INFERENCE, workspace,
            "inference_current")
        if candidate_path and replace_inference:
            inference_source = candidate_path
        else:
            inference_source = inference_current
        if candidate_path and not replace_inference:
            user_source = self._merge_sources(
                [path for path in (user_current, candidate_path) if path],
                workspace, "users_merged")
        else:
            user_source = user_current

        users = self._prepare_source(user_source, workspace, "users",
                                     ORIGIN_USER, None)
        inference = self._prepare_source(inference_source, workspace,
                                         "inference", ORIGIN_INFERENCE,
                                         run_id)
        if users and inference:
            inference = self._run("native:difference", {
                "INPUT": inference,
                "OVERLAY": users,
            }, workspace, "inference_without_users")
            inference = self._apply_metadata(
                inference, ORIGIN_INFERENCE, run_id)
        result = self._merge_sources(
            [path for path in (users, inference) if path], workspace,
            "composed")
        destination = session.next_generation_path()
        if result:
            self._normalise_to_generation(result, destination, run_id)
        else:
            self.create_empty(destination, session.input_path)
        self._assert_generation(destination)
        return destination

    def mark_manual_changes(self, layer, snapshot_path):
        """提交前把新增或改动的要素标记为 user。"""
        if not snapshot_path or not os.path.isfile(snapshot_path):
            return
        snapshot = QgsVectorLayer(layer_uri(snapshot_path), "snapshot", "ogr")
        originals = {}
        if snapshot.isValid():
            for feature in snapshot.getFeatures():
                feature_uuid = str(feature[FIELD_FEATURE_UUID] or "")
                if feature_uuid:
                    originals[feature_uuid] = bytes(feature.geometry().asWkb())
        field_map = {field.name(): index for index, field in
                     enumerate(layer.fields())}
        for feature in layer.getFeatures():
            feature_uuid = str(feature[FIELD_FEATURE_UUID] or "")
            changed = (not feature_uuid or
                       originals.get(feature_uuid) != bytes(feature.geometry().asWkb()))
            if not feature_uuid:
                layer.changeAttributeValue(feature.id(),
                                           field_map[FIELD_FEATURE_UUID],
                                           uuid.uuid4().hex)
            if changed:
                layer.changeAttributeValue(feature.id(), field_map[FIELD_ORIGIN],
                                           ORIGIN_USER)
                layer.changeAttributeValue(feature.id(), field_map[FIELD_RUN_ID],
                                           None)
            layer.changeAttributeValue(feature.id(), field_map[FIELD_CLASS_ID],
                                       LANDSLIDE_CLASS_ID)
            layer.changeAttributeValue(feature.id(), field_map[FIELD_CLASS_NAME],
                                       LANDSLIDE_CLASS_NAME)

    def _prepare_source(self, path, workspace, name, origin, run_id):
        if not path or self._feature_count(path) == 0:
            return None
        fixed = self._run("native:fixgeometries", {"INPUT": path}, workspace,
                          "{}_fixed".format(name))
        dissolved = self._run("native:dissolve", {
            "INPUT": fixed,
            "FIELD": [],
            "SEPARATE_DISJOINT": False,
        }, workspace, "{}_dissolved".format(name))
        parts = self._run("native:multiparttosingleparts", {"INPUT": dissolved},
                          workspace, "{}_parts".format(name))
        return self._apply_metadata(parts, origin, run_id)

    def _apply_metadata(self, path, origin, run_id):
        """在每个 Processing 阶段后恢复草稿来源元数据。"""
        layer = QgsVectorLayer(path, "metadata", "ogr")
        if not layer.isValid() or not layer.startEditing():
            raise IOError("无法更新草稿合成元数据。")
        fields = {field.name(): index for index, field in enumerate(layer.fields())}
        for required in (FIELD_CLASS_ID, FIELD_CLASS_NAME, FIELD_SOURCE_ID,
                         FIELD_FEATURE_UUID, FIELD_ORIGIN, FIELD_RUN_ID):
            if required not in fields:
                raise IOError("草稿合成结果缺少字段: {}".format(required))
        for feature in layer.getFeatures():
            layer.changeAttributeValue(feature.id(), fields[FIELD_CLASS_ID],
                                       LANDSLIDE_CLASS_ID)
            layer.changeAttributeValue(feature.id(), fields[FIELD_CLASS_NAME],
                                       LANDSLIDE_CLASS_NAME)
            layer.changeAttributeValue(feature.id(), fields[FIELD_ORIGIN], origin)
            if origin == ORIGIN_USER:
                layer.changeAttributeValue(feature.id(), fields[FIELD_RUN_ID], None)
            elif run_id is not None:
                layer.changeAttributeValue(feature.id(), fields[FIELD_RUN_ID],
                                           str(run_id))
        if not layer.commitChanges():
            errors = "; ".join(layer.commitErrors())
            layer.rollBack()
            raise IOError("更新草稿合成元数据失败: {}".format(errors))
        return path
    def _extract_origin(self, path, origin, workspace, name):
        if not path or not os.path.isfile(path):
            return None
        result = self._run("native:extractbyexpression", {
            "INPUT": layer_uri(path),
            "EXPRESSION": "\"{}\" = '{}'".format(FIELD_ORIGIN, origin),
        }, workspace, name)
        return result if self._feature_count(result) else None

    def _merge_sources(self, sources, workspace, name):
        sources = [source for source in sources
                   if source and self._feature_count(source)]
        if not sources:
            return None
        if len(sources) == 1:
            return sources[0]
        return self._run("native:mergevectorlayers", {
            "LAYERS": sources,
            "CRS": None,
        }, workspace, name)

    def _run(self, algorithm, parameters, workspace, name):
        import processing

        output = os.path.join(workspace, "{}_{}.gpkg".format(
            name, uuid.uuid4().hex))
        parameters = dict(parameters)
        parameters["OUTPUT"] = output
        result = processing.run(algorithm, parameters)
        return result["OUTPUT"]

    def _normalise_to_generation(self, source_path, destination, default_run_id):
        from osgeo import ogr

        source_ds = ogr.Open(source_path)
        if source_ds is None:
            raise IOError("无法读取草稿合成结果: {}".format(source_path))
        source_layer = source_ds.GetLayer(0)
        driver = ogr.GetDriverByName("GPKG")
        if os.path.exists(destination):
            driver.DeleteDataSource(destination)
        destination_ds = driver.CreateDataSource(destination)
        destination_layer = destination_ds.CreateLayer(
            LAYER_NAME, srs=source_layer.GetSpatialRef(),
            geom_type=ogr.wkbMultiPolygon)
        self._create_fields(destination_layer, ogr)
        source_id = 1
        seen_feature_uuids = set()
        source_layer.ResetReading()
        for source_feature in source_layer:
            geometry = source_feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            feature = ogr.Feature(destination_layer.GetLayerDefn())
            feature.SetGeometry(ogr.ForceToMultiPolygon(geometry.Clone()))
            origin = source_feature.GetField(FIELD_ORIGIN) or ORIGIN_USER
            origin = origin if origin in (ORIGIN_USER, ORIGIN_INFERENCE) else ORIGIN_USER
            feature.SetField(FIELD_CLASS_ID, LANDSLIDE_CLASS_ID)
            feature.SetField(FIELD_CLASS_NAME, LANDSLIDE_CLASS_NAME)
            feature.SetField(FIELD_SOURCE_ID, source_id)
            feature_uuid = str(
                source_feature.GetField(FIELD_FEATURE_UUID) or "").strip()
            if not feature_uuid or feature_uuid in seen_feature_uuids:
                feature_uuid = uuid.uuid4().hex
            seen_feature_uuids.add(feature_uuid)
            feature.SetField(FIELD_FEATURE_UUID, feature_uuid)
            feature.SetField(FIELD_ORIGIN, origin)
            source_run_id = source_feature.GetField(FIELD_RUN_ID)
            feature.SetField(
                FIELD_RUN_ID,
                (source_run_id or default_run_id)
                if origin == ORIGIN_INFERENCE else None)
            if destination_layer.CreateFeature(feature) != 0:
                raise IOError("无法写入规范化草稿要素。")
            source_id += 1
        destination_ds = None
        source_ds = None

    def _assert_generation(self, path):
        layer = QgsVectorLayer(layer_uri(path), "draft_check", "ogr")
        required = {FIELD_CLASS_ID, FIELD_CLASS_NAME, FIELD_SOURCE_ID,
                    FIELD_FEATURE_UUID, FIELD_ORIGIN, FIELD_RUN_ID}
        if not layer.isValid() or not required.issubset(
                {field.name() for field in layer.fields()}):
            raise IOError("草稿 generation 校验失败: {}".format(path))

    def _feature_count(self, path):
        layer = QgsVectorLayer(path, "count", "ogr")
        return layer.featureCount() if layer.isValid() else 0

    def _reference_spatial_ref(self, reference_path, osr):
        if not reference_path:
            return None
        from osgeo import gdal

        dataset = gdal.Open(reference_path)
        if dataset is None or not dataset.GetProjection():
            return None
        spatial_ref = osr.SpatialReference()
        spatial_ref.ImportFromWkt(dataset.GetProjection())
        dataset = None
        return spatial_ref

    def _create_fields(self, layer, ogr):
        fields = [
            (FIELD_CLASS_ID, ogr.OFTInteger),
            (FIELD_CLASS_NAME, ogr.OFTString),
            (FIELD_SOURCE_ID, ogr.OFTInteger),
            (FIELD_FEATURE_UUID, ogr.OFTString),
            (FIELD_ORIGIN, ogr.OFTString),
            (FIELD_RUN_ID, ogr.OFTString),
        ]
        for name, field_type in fields:
            if layer.GetLayerDefn().GetFieldIndex(name) < 0:
                layer.CreateField(ogr.FieldDefn(name, field_type))

    def _as_multipolygon(self, geometry):
        if geometry is None or geometry.isEmpty():
            raise ValueError("AI 预览没有可写入的多边形。")
        converted = QgsGeometry(geometry)
        if QgsWkbTypes.geometryType(converted.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise ValueError("AI 预览必须是多边形。")
        return converted.convertToType(QgsWkbTypes.PolygonGeometry, True)
