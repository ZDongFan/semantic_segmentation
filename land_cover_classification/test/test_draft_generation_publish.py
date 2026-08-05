# -*- coding: utf-8 -*-
"""统一草稿 generation 发布与回滚测试。"""

import os
import tempfile
import unittest

from land_cover_classification.draft_session import (
    FIELD_CLASS_ID,
    FIELD_CLASS_NAME,
    FIELD_FEATURE_UUID,
    FIELD_ORIGIN,
    FIELD_RUN_ID,
    FIELD_SOURCE_ID,
    LANDSLIDE_CLASS_ID,
    LANDSLIDE_CLASS_NAME,
    ORIGIN_USER,
    DraftComposer,
    DraftSession,
    layer_uri,
)
from land_cover_classification.land_cover_classification_dialog import (
    DRAFT_LAYER_MARKER,
    DRAFT_LAYER_MARKER_VALUE,
    LandCoverClassificationDialog,
)


class _FakeLayer:
    """提供 generation 切换和插件来源识别所需的最小图层接口。"""

    def __init__(self, source, layer_id="draft-layer", fail_uri=None):
        self._source = source
        self._layer_id = layer_id
        self._fail_uri = fail_uri
        self._valid = True
        self._properties = {}
        self.reload_count = 0
        self.repaint_count = 0

    def id(self):
        return self._layer_id

    def name(self):
        return "地物分类草稿"

    def source(self):
        return self._source

    def providerType(self):
        return "ogr"

    def setDataSource(self, source, name, provider):
        del name, provider
        self._source = source
        self._valid = source != self._fail_uri
        return self._valid

    def isValid(self):
        return self._valid

    def reload(self):
        self.reload_count += 1

    def triggerRepaint(self):
        self.repaint_count += 1

    def setCustomProperty(self, key, value):
        self._properties[key] = value

    def customProperty(self, key, default=None):
        return self._properties.get(key, default)


class _FakeDraftSession:
    """记录统一发布入口提交的 generation 和可见层。"""

    def __init__(self):
        self.generation_path = None
        self.layer = None
        self.fail = False
        self.calls = []

    def activate_generation(self, generation, layer):
        if self.fail:
            raise IOError("模拟 generation 切换失败")
        layer.setDataSource(layer_uri(generation), layer.name(), "ogr")
        self.calls.append((generation, layer))
        self.generation_path = generation
        self.layer = layer


class _PublishHarness:
    """复用对话框发布方法，但不创建真实 QGIS 窗口。"""

    def __init__(self):
        self._draft_session = _FakeDraftSession()
        self._draft_layer = None
        self._draft_layer_id = None
        self._draft_path = None
        self._loaded_layer = _FakeLayer("initial|layername=draft")
        self.load_count = 0
        self.cleanup_count = 0

    def _validate_draft_generation(self, generation):
        return generation

    def _find_reusable_session_draft_layer(self):
        return self._draft_layer

    def _load_session_draft_layer(self, generation):
        self.load_count += 1
        self._loaded_layer._source = layer_uri(generation)
        return self._loaded_layer

    def _connect_session_draft_layer(self, layer):
        del layer

    def _mark_session_draft_layer(self, layer):
        layer.setCustomProperty(
            DRAFT_LAYER_MARKER, DRAFT_LAYER_MARKER_VALUE)

    def _apply_vector_style(self, layer, draft):
        del layer, draft

    def _place_layer_above_input(self, layer):
        del layer

    def _activate_layer(self, layer):
        del layer

    def _remove_stale_session_draft_layers(self):
        self.cleanup_count += 1


class DraftGenerationPublishTest(unittest.TestCase):
    """验证可见层复用和失败回滚边界。"""

    def test_publish_reuses_one_visible_layer(self):
        """连续发布多个 generation 时只加载一次可见层。"""
        harness = _PublishHarness()

        first = LandCoverClassificationDialog._publish_draft_generation(
            harness, "generation_0001.gpkg")
        second = LandCoverClassificationDialog._publish_draft_generation(
            harness, "generation_0002.gpkg")

        self.assertIs(first, second)
        self.assertEqual(harness.load_count, 1)
        self.assertEqual(harness.cleanup_count, 2)
        self.assertEqual(harness._draft_layer_id, "draft-layer")
        self.assertEqual(harness._draft_path, "generation_0002.gpkg")
        self.assertEqual(
            second.source(), layer_uri("generation_0002.gpkg"))
        self.assertIs(harness._draft_session.layer, first)

    def test_publish_failure_keeps_previous_visible_state(self):
        """复用层切换失败时不覆盖已发布的对话框状态。"""
        harness = _PublishHarness()
        LandCoverClassificationDialog._publish_draft_generation(
            harness, "generation_0001.gpkg")
        previous_layer = harness._draft_layer
        previous_path = harness._draft_path
        harness._draft_session.fail = True

        with self.assertRaises(IOError):
            LandCoverClassificationDialog._publish_draft_generation(
                harness, "generation_0002.gpkg")

        self.assertIs(harness._draft_layer, previous_layer)
        self.assertEqual(harness._draft_path, previous_path)
        self.assertEqual(harness.cleanup_count, 1)

    def test_activate_generation_restores_old_source_on_failure(self):
        """底层 setDataSource 失败时恢复旧 generation 和旧数据源。"""
        with tempfile.TemporaryDirectory() as directory:
            previous = os.path.join(directory, "generation_0001.gpkg")
            candidate = os.path.join(directory, "generation_0002.gpkg")
            for path in (previous, candidate):
                with open(path, "wb"):
                    pass

            layer = _FakeLayer(
                layer_uri(previous),
                fail_uri=layer_uri(candidate),
            )
            session = DraftSession()
            session.generation_path = previous
            session.layer = layer

            with self.assertRaises(IOError):
                session.activate_generation(candidate, layer)

            self.assertEqual(session.generation_path, previous)
            self.assertIs(session.layer, layer)
            self.assertEqual(layer.source(), layer_uri(previous))
            self.assertTrue(layer.isValid())
            self.assertFalse(session.is_replacing)

    def test_normalised_generation_has_unique_feature_uuids(self):
        """multipart 结果写入 generation 时必须消除重复 UUID。"""
        from osgeo import ogr

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.gpkg")
            destination = os.path.join(directory, "generation.gpkg")
            driver = ogr.GetDriverByName("GPKG")
            source_ds = driver.CreateDataSource(source)
            source_layer = source_ds.CreateLayer(
                "source", geom_type=ogr.wkbMultiPolygon)
            composer = DraftComposer()
            composer._create_fields(source_layer, ogr)
            for source_id, wkt in enumerate((
                    "POLYGON((0 0,1 0,1 1,0 1,0 0))",
                    "POLYGON((2 0,3 0,3 1,2 1,2 0))",
            ), start=1):
                feature = ogr.Feature(source_layer.GetLayerDefn())
                geometry = ogr.CreateGeometryFromWkt(wkt)
                feature.SetGeometry(ogr.ForceToMultiPolygon(geometry))
                feature.SetField(FIELD_CLASS_ID, LANDSLIDE_CLASS_ID)
                feature.SetField(FIELD_CLASS_NAME, LANDSLIDE_CLASS_NAME)
                feature.SetField(FIELD_SOURCE_ID, source_id)
                feature.SetField(FIELD_FEATURE_UUID, "duplicate-uuid")
                feature.SetField(FIELD_ORIGIN, ORIGIN_USER)
                feature.SetField(FIELD_RUN_ID, None)
                self.assertEqual(source_layer.CreateFeature(feature), 0)
            source_ds = None

            composer._normalise_to_generation(source, destination, None)

            destination_ds = ogr.Open(destination)
            destination_layer = destination_ds.GetLayer(0)
            feature_uuids = [
                feature.GetField(FIELD_FEATURE_UUID)
                for feature in destination_layer
            ]
            destination_ds = None
            self.assertEqual(len(feature_uuids), 2)
            self.assertEqual(len(set(feature_uuids)), 2)

    def test_user_named_gpkg_is_not_treated_as_plugin_draft(self):
        """用户目录中的同名前缀文件不能被旧层清理误识别。"""
        harness = type("LayerClassifier", (), {
            "_draft_layer_source_path":
                LandCoverClassificationDialog._draft_layer_source_path,
            "_is_plugin_session_draft_layer":
                LandCoverClassificationDialog._is_plugin_session_draft_layer,
        })()
        user_source = os.path.join(
            os.getcwd(), "user_data", "lcc_draft_user.gpkg")
        user_layer = _FakeLayer(user_source + "|layername=draft")
        plugin_source = os.path.join(
            tempfile.gettempdir(), "lcc_draft_plugin.gpkg")
        plugin_layer = _FakeLayer(plugin_source + "|layername=draft")

        self.assertFalse(
            harness._is_plugin_session_draft_layer(user_layer))
        self.assertTrue(
            harness._is_plugin_session_draft_layer(plugin_layer))


if __name__ == "__main__":
    unittest.main()
