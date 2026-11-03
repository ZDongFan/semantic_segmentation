# -*- coding: utf-8 -*-
"""在独立 QGIS 中验证项目草稿恢复、故障保留和原有三种导出。"""

import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings, Qt, QTimer
from qgis.PyQt.QtGui import QCloseEvent, QKeyEvent
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvas, QgsMessageBar

from land_cover_classification.draft_session import DraftComposer, DraftSession, layer_uri
from land_cover_classification.export_service import ExportService
from land_cover_classification.land_cover_classification_dialog import LandCoverClassificationDialog
from land_cover_classification.project_client import ProjectClientError, snapshot_gpkg
from land_cover_classification.land_cover_classification import _ProjectDockWidget


class _Iface:
    """使用真实画布和消息栏，隔离用户正在使用的 QGIS 会话。"""

    def __init__(self):
        self.window = QtWidgets.QMainWindow()
        self.canvas = QgsMapCanvas()
        self.bar = QgsMessageBar()
        self.layer = None

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def setActiveLayer(self, layer):
        self.layer = layer

    def activeLayer(self):
        return self.layer

    def addRasterLayer(self, path, name):
        from qgis.core import QgsRasterLayer
        layer = QgsRasterLayer(path, name)
        QgsProject.instance().addMapLayer(layer)
        return layer


class _ServiceClient:
    """模拟跨进程服务的文件存储，保留真实 GeoPackage 网络边界。"""

    base_url = "http://test.invalid"

    def __init__(self, directory, image):
        self.directory = directory
        self.image = image
        self.saved = None
        self.failure = False
        self.requests = []

    def request_on_exit(self, path, upload):
        return self.request('PUT', path, upload=upload)

    def request(self, method, path, payload=None, upload=None, download=None):
        self.requests.append((method, path, payload))
        if self.failure:
            raise ProjectClientError("服务不可用")
        if method == "PUT":
            self.saved = os.path.join(self.directory, "saved.gpkg")
            snapshot_gpkg(upload, self.saved)
            self.image["draft_id"] = str(uuid.uuid4())
            return {"draft_id": self.image["draft_id"]}
        if download:
            shutil.copy2(self.saved, download)
            return {"X-Draft-ID": self.image["draft_id"]}
        if path.endswith("/publish"):
            return {"id": str(uuid.uuid4())}
        if path.endswith("/images"):
            if method == "POST":
                self.image["dem_path"] = payload["dem_path"]
                return self.image.copy()
            return [self.image.copy()]
        if path == "/internal/projects":
            return [self.project.copy()]
        return []


class ProjectWorkflowTest(unittest.TestCase):
    """覆盖真实 QGIS 控件、草稿数据源与导出驱动。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication.instance() or QgsApplication([], True)
        cls.app.initQgis()
        cls.app.setQuitOnLastWindowClosed(False)
        sys.path.insert(0, os.path.join(QgsApplication.pkgDataPath(), "python", "plugins"))
        from processing.core.Processing import Processing
        Processing.initialize()

    def setUp(self):
        from osgeo import gdal, osr
        self.directory = tempfile.TemporaryDirectory(prefix="lcc_project_test_")
        self.root = self.directory.name
        self.image_path = os.path.join(self.root, "input.tif")
        raster = gdal.GetDriverByName("GTiff").Create(self.image_path, 100, 100, 3, gdal.GDT_Byte)
        raster.SetGeoTransform((500000, 1, 0, 3000100, 0, -1))
        crs = osr.SpatialReference()
        crs.ImportFromEPSG(32650)
        raster.SetProjection(crs.ExportToWkt())
        raster = None
        self.iface = _Iface()
        self.dialog = LandCoverClassificationDialog(self.iface)
        self.dialog._warn = Mock()
        self.panel = self.dialog._project_panel
        self.panel._started = True
        self.confirm = patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.Yes)
        self.confirm.start()
        self.addCleanup(self.confirm.stop)
        self.panel.settings = QSettings(os.path.join(self.root, "settings.ini"), QSettings.IniFormat)
        self.project = {"id": str(uuid.uuid4()), "name": "测试项目", "current_version": None}
        self.image = {"id": str(uuid.uuid4()), "name": "测试影像", "image_path": self.image_path,
                      "dem_path": None, "draft_id": None, "project_id": self.project["id"]}
        self.client = _ServiceClient(self.root, self.image)
        self.client.project = self.project
        self.panel.client = self.client
        self.panel.project = self.project
        self.panel.projects.blockSignals(True)
        self.panel.projects.clear()
        self.panel.projects.addItem(self.project["name"], self.project)
        self.panel.projects.blockSignals(False)
        self.panel.images.clear()
        self.panel.images.addItem(self.image["name"], self.image)
        self.panel.call = lambda fn: fn()
        self.panel.open_image()

    def tearDown(self):
        self.panel.project = self.panel.image = None
        self.dialog._discard_draft_session()
        self.dialog.close()
        QgsProject.instance().removeAllMapLayers()
        self.dialog.deleteLater()
        QgsApplication.processEvents()
        self.directory.cleanup()

    def add_feature(self):
        self.assertTrue(self.dialog._ensure_draft_session(self.image_path))
        self.dialog._publish_draft_generation(self.dialog._draft_session.generation_path)
        layer = self.dialog._draft_layer
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromWkt(
            "MULTIPOLYGON (((500010 3000010,500030 3000010,500030 3000030,"
            "500010 3000030,500010 3000010)))"))
        for name, value in {"class_id": 1, "class_name": "landslide", "source_id": 1,
                            "feature_uuid": str(uuid.uuid4()), "origin": "user",
                            "run_id": None}.items():
            feature[name] = value
        self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        layer.updateExtents()
        self.dialog._draft_session.exported_generation = None

    def test_preview_standard_fields_and_current_project(self):
        from land_cover_classification.result_attributes import RESULT_FIELD_NAMES
        self.add_feature()
        original_fields = self.dialog._draft_layer.fields().names()
        original_requests = list(self.client.requests)
        for cartographer in ("张三", None):
            self.panel.project["ztz"] = cartographer
            self.panel.image["id"] = str(uuid.uuid4())
            preview = self.panel._create_result_preview()
            try:
                table = preview.findChild(QtWidgets.QTableWidget, "resultAttributesTable")
                names = [table.horizontalHeaderItem(i).data(Qt.UserRole)
                         for i in range(table.columnCount())]
                self.assertEqual(names[1:18], list(RESULT_FIELD_NAMES))
                self.assertEqual(len(names), len(set(names)))
                self.assertNotIn("area_m2", names)
                values = {name: table.item(0, col).data(Qt.UserRole)
                          for col, name in enumerate(names)}
                self.assertEqual(values["tid"], uuid.UUID(self.panel.image["id"]).hex)
                self.assertEqual(values["ztz"], cartographer)
                self.assertAlmostEqual(values["mj"], 400.3201891841654, places=3)
                self.assertEqual(values["class_name"], "landslide")
                self.assertGreater(values["perimeter_m"], 0)
                for name in RESULT_FIELD_NAMES:
                    if name not in ("tid", "mj", "ztz"):
                        self.assertIsNone(values[name])
                        self.assertEqual(table.item(0, names.index(name)).text(), "")
                self.assertEqual(table.editTriggers(), QtWidgets.QAbstractItemView.NoEditTriggers)
            finally:
                preview.deleteLater()
        self.assertEqual(self.dialog._draft_layer.fields().names(), original_fields)
        self.assertEqual(self.client.requests, original_requests)

    def test_preview_empty_draft_keeps_all_headers(self):
        self.assertTrue(self.dialog._ensure_draft_session(self.image_path))
        self.dialog._publish_draft_generation(self.dialog._draft_session.generation_path)
        preview = self.panel._create_result_preview()
        try:
            table = preview.findChild(QtWidgets.QTableWidget, "resultAttributesTable")
            self.assertEqual(table.rowCount(), 0)
            self.assertEqual(table.columnCount(), 24)
        finally:
            preview.deleteLater()

    def test_preview_rejects_mismatched_project(self):
        self.add_feature()
        self.panel.image["project_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "未关联"):
            self.panel._create_result_preview()

    def test_save_and_restore_after_session_destroyed(self):
        self.add_feature()
        self.panel.save()
        old = self.dialog._draft_session.generation_path
        self.dialog._discard_draft_session()
        self.panel.image = None
        self.panel.images.clear()
        self.panel.images.addItem(self.image["name"], self.image)
        self.panel.open_image()
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.dialog._draft_layer.featureCount(), 1)
        self.assertTrue(self.dialog.inputGroup.isEnabled())
        self.assertEqual(self.dialog._draft_layer.getFeature(1)["origin"], "user")

    def test_close_failure_keeps_generation(self):
        self.add_feature()
        path = self.dialog._draft_session.generation_path
        self.client.failure = True
        event = QCloseEvent()
        QtWidgets.QApplication.sendEvent(self.dialog, event)
        self.assertTrue(event.isAccepted())
        self.assertFalse(self.dialog._draft_session.is_active)
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        self.assertTrue(os.path.isfile(path))

    def test_publish_retry_reuses_idempotency_key(self):
        self.add_feature()
        self.panel.save()
        original_request = self.client.request
        attempted = []
        def request(method, path, payload=None, **kwargs):
            if path.endswith("/publish"):
                attempted.append(payload.copy())
                if len(attempted) == 1:
                    raise ProjectClientError("响应丢失")
            return original_request(method, path, payload, **kwargs)
        self.client.request = request
        with self.assertRaises(ProjectClientError):
            self.panel.publish()
        # 模拟保存返回同一草稿标识，确保重试发布同一个快照。
        pending = next(k for k in self.panel.settings.allKeys() if "pending_project_publish" in k)
        import json
        draft_id = json.loads(self.panel.settings.value(pending))["drafts"][0]["draft_id"]
        original_save = self.panel.save
        self.panel.save = lambda: (self.panel.image.update(draft_id=draft_id), self.client.image.update(draft_id=draft_id))
        self.panel.publish()
        self.panel.save = original_save
        self.assertEqual(attempted[0]["idempotency_key"], attempted[1]["idempotency_key"])
        self.assertFalse(self.panel.settings.contains(pending))

    def test_original_exports(self):
        self.add_feature()
        service = ExportService()
        session = self.dialog._draft_session
        for kind, extension in (("raster", "tif"), ("vector", "shp"), ("dxf", "dxf")):
            path = os.path.join(self.root, "output." + extension)
            service.export(session, kind, path, image_id=self.image["id"], ztz="张" * 32)
            self.assertTrue(os.path.isfile(path))
        layer = QgsVectorLayer(os.path.join(self.root, "output.shp"), "export", "ogr")
        self.assertTrue(layer.isValid())
        from land_cover_classification.result_attributes import RESULT_FIELD_NAMES
        self.assertEqual([field.name() for field in layer.fields()],
                         ["class_id", "class_name", "source_id", *RESULT_FIELD_NAMES])
        feature = next(layer.getFeatures())
        self.assertEqual(feature["ztz"], "张" * 32)
        self.assertEqual(feature["tid"], uuid.UUID(self.image["id"]).hex)
        self.assertGreater(feature["mj"], 400)
        from qgis.core import QgsVariantUtils
        for name in RESULT_FIELD_NAMES:
            if name not in ("tid", "ztz", "mj"):
                self.assertTrue(QgsVariantUtils.isNull(feature[name]), name)
        self.assertEqual(layer.fields().field("mj").length(), 24)
        self.assertEqual(layer.fields().field("mj").precision(), 3)
        self.assertIn("UTF-8", Path(self.root, "output.cpg").read_text())
        layer = None
        with open(os.path.join(self.root, "output.tif"), "rb") as stream:
            self.assertEqual(stream.read(4), b"II+\x00")

    def test_dock_close_failure_allows_close(self):
        self.add_feature()
        self.client.failure = True
        dock = _ProjectDockWidget("测试")
        dock.setWidget(self.dialog)
        self.dialog.show()
        event = QCloseEvent()
        path = self.dialog._draft_session.directory
        QtWidgets.QApplication.sendEvent(dock, event)
        self.assertTrue(event.isAccepted())
        self.addCleanup(shutil.rmtree, path, True)
        self.dialog.setParent(None)
        dock.deleteLater()

    def test_escape_failure_allows_close(self):
        from qgis.PyQt.QtCore import QEvent
        self.add_feature()
        self.client.failure = True
        self.dialog.show()
        event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        path = self.dialog._draft_session.directory
        QtWidgets.QApplication.sendEvent(self.dialog, event)
        self.assertFalse(self.dialog._draft_session.is_active)
        self.assertFalse(self.dialog.isVisible())
        self.addCleanup(shutil.rmtree, path, True)

    def test_host_close_passes_through_and_saves_once(self):
        self.add_feature()
        host = self.iface.window
        host.show()
        with patch.object(self.panel, "exit_notice") as notice:
            event = QCloseEvent()
            QtWidgets.QApplication.sendEvent(host, event)
            self.assertTrue(event.isAccepted())
            self.dialog.close()
        self.assertEqual(sum(method == "PUT" for method, _, _ in self.client.requests), 1)
        notice.assert_called()

    def test_host_cancel_allows_later_save(self):
        self.add_feature()
        host = self.iface.window
        host.show()
        event = QCloseEvent()
        self.panel.eventFilter(host, event)
        event.ignore()
        self.panel._host_close_settled()
        self.panel.eventFilter(host, QCloseEvent())
        self.assertEqual(sum(method == "PUT" for method, _, _ in self.client.requests), 2)
        host.hide()

    def test_busy_close_keeps_generation_without_upload(self):
        self.add_feature()
        path = self.dialog._draft_session.generation_path
        self.panel.busy = True
        self.assertTrue(self.dialog.close())
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(any(method == "PUT" for method, _, _ in self.client.requests))
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)

    def test_close_commit_failure_does_not_confirm_success(self):
        self.add_feature()
        path = self.dialog._draft_session.generation_path
        with patch.object(self.dialog, "_commit_layer_if_needed", return_value=False), \
                patch.object(self.panel, "exit_notice") as notice:
            self.assertTrue(self.dialog.close())
        self.assertEqual(self.panel._close_outcome, "failed")
        self.assertFalse(any(call.kwargs.get("success") for call in notice.call_args_list))
        self.assertTrue(os.path.isfile(path))
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)

    def test_invalid_context_preserves_existing_export(self):
        self.add_feature()
        path = os.path.join(self.root, "existing.shp")
        Path(path).write_bytes(b"existing-output")
        self.panel.image["project_id"] = str(uuid.uuid4())
        with patch.object(self.dialog, "_selected_output_format", return_value="vector"), \
                patch.object(self.dialog, "_vector_output_path", return_value=path):
            self.dialog._on_export_raster()
        self.assertEqual(Path(path).read_bytes(), b"existing-output")

    def test_close_running_request_keeps_snapshot_until_finished(self):
        import threading
        import time
        from land_cover_classification.project_panel import ProjectPanel, _ClosingRequest
        self.add_feature()
        release = threading.Event()
        observed = []
        with self.panel.request_directory("lcc_request_test_") as directory:
            snapshot = os.path.join(directory, "draft.gpkg")
            snapshot_gpkg(self.dialog._draft_session.generation_path, snapshot)

            def operation():
                release.wait(2)
                observed.append(os.path.isfile(snapshot))

            QTimer.singleShot(20, self.dialog.close)
            path = self.dialog._draft_session.directory
            with self.assertRaises(_ClosingRequest):
                ProjectPanel.call(self.panel, operation)
        self.assertTrue(os.path.isfile(snapshot))
        release.set()
        deadline = time.monotonic() + 3
        while (not observed or os.path.exists(directory)) and time.monotonic() < deadline:
            QgsApplication.processEvents()
            time.sleep(0.01)
        self.assertEqual(observed, [True])
        self.assertFalse(os.path.exists(directory))
        self.addCleanup(shutil.rmtree, path, True)

    def test_network_worker_propagates_result_and_error(self):
        from land_cover_classification.project_panel import ProjectPanel
        self.assertEqual(ProjectPanel.call(self.panel, lambda: 42), 42)
        def fail():
            raise RuntimeError("test")
        with self.assertRaisesRegex(RuntimeError, "test"):
            ProjectPanel.call(self.panel, fail)
        self.assertFalse(self.panel.busy)

    def test_switch_input_failure_keeps_old_generation(self):
        self.add_feature()
        generation = self.dialog._draft_session.generation_path
        other = os.path.join(self.root, "other.tif")
        shutil.copy2(self.image_path, other)
        self.client.failure = True
        self.dialog.inputFileWidget.setFilePath(other)
        self.assertEqual(self.dialog._draft_session.generation_path, generation)
        self.assertEqual(self.dialog.inputFileWidget.filePath(), self.image_path)
        self.assertEqual(self.panel.image["id"], self.image["id"])
        self.dialog._warn.assert_called()

    def test_layer_input_mode_is_preserved(self):
        from qgis.core import QgsRasterLayer
        self.panel.image = None
        layer = QgsRasterLayer(self.image_path, "输入图层")
        QgsProject.instance().addMapLayer(layer)
        self.dialog.layerCombo.setLayer(layer)
        self.dialog.layerRadio.setChecked(True)
        self.assertTrue(self.dialog.layerRadio.isChecked())
        self.assertEqual(self.panel.image["image_path"], self.image_path)

    def test_dem_selection_updates_association_and_keeps_geometry(self):
        self.add_feature()
        dem = os.path.join(self.root, "dem.tif")
        shutil.copy2(self.image_path, dem)
        self.dialog.mDemFile.setFilePath(dem)
        self.assertEqual(self.panel.image["dem_path"], dem)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 1)
        self.assertTrue(any(method == "POST" and payload["dem_path"] == dem
                            for method, path, payload in self.client.requests if path.endswith("/images")))

    def test_export_does_not_publish_version(self):
        self.add_feature()
        with patch.object(self.dialog._export_service, "export"), \
                patch.object(self.dialog, "_selected_output_format", return_value="vector"), \
                patch.object(self.dialog, "_vector_output_path", return_value=os.path.join(self.root, "out.shp")):
            self.dialog._on_export_raster()
        self.assertFalse(any(path.endswith("/publish") for _, path, _ in self.client.requests))

    def test_project_switch_failure_keeps_old_project(self):
        self.add_feature()
        generation = self.dialog._draft_session.generation_path
        self.panel.projects.addItem("另一个项目", {"id": str(uuid.uuid4()), "name": "另一个项目", "current_version": None})
        self.client.failure = True
        self.panel.projects.setCurrentIndex(self.panel.projects.count() - 1)
        self.assertEqual(self.panel.project["id"], self.project["id"])
        self.assertEqual(self.panel.projects.currentData()["id"], self.project["id"])
        self.assertEqual(self.dialog._draft_session.generation_path, generation)

    def test_create_project_form_and_cancel(self):
        captured = []
        original = self.client.request
        def request(method, path, payload=None, **kwargs):
            if method == "POST" and path == "/internal/projects":
                captured.append(payload)
                return dict(self.project, **payload)
            return original(method, path, payload, **kwargs)
        self.client.request = request

        def fill():
            form = QtWidgets.QApplication.activeModalWidget()
            edits = form.findChildren(QtWidgets.QLineEdit)
            edits[0].setText("新项目")
            edits[1].setText(" 张三 ")
            form.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.Ok).click()

        QTimer.singleShot(20, fill)
        self.panel.create_project()
        self.assertEqual(captured, [{"name": "新项目", "ztz": "张三"}])

        def cancel():
            form = QtWidgets.QApplication.activeModalWidget()
            self.assertTrue(all(not edit.text() for edit in form.findChildren(QtWidgets.QLineEdit)))
            form.reject()

        QTimer.singleShot(20, cancel)
        self.panel.create_project()
        self.assertEqual(len(captured), 1)

    def test_unload_failure_still_removes_actions(self):
        from land_cover_classification.land_cover_classification import LandCoverClassification
        from types import SimpleNamespace
        self.add_feature()
        self.client.failure = True
        path = self.dialog._draft_session.directory
        dock = _ProjectDockWidget("测试")
        dock.setWidget(self.dialog)
        iface = Mock()
        harness = SimpleNamespace(dlg=self.dialog, dock=dock, actions=[object()],
                                  iface=iface, tr=lambda text: text)
        LandCoverClassification.unload(harness)
        iface.removePluginMenu.assert_called_once()
        iface.removeToolBarIcon.assert_called_once()
        iface.mainWindow().removeDockWidget.assert_called_once()
        self.assertIsNone(harness.dlg)
        self.dialog.setParent(None)
        self.addCleanup(shutil.rmtree, path, True)

    def test_reentrant_close_only_uploads_once(self):
        self.add_feature()
        original = self.client.request_on_exit
        def request(path, upload):
            QtWidgets.QApplication.sendEvent(self.iface.window, QCloseEvent())
            self.dialog.close()
            return original(path, upload)
        self.client.request_on_exit = request
        self.dialog.close()
        self.assertEqual(sum(method == "PUT" for method, _, _ in self.client.requests), 1)
        self.assertFalse(self.dialog._draft_session.is_active)

    def test_repeated_export_keeps_number(self):
        self.add_feature()
        path = os.path.join(self.root, "repeat.shp")
        for _ in range(2):
            ExportService().export(self.dialog._draft_session, "vector", path,
                                   image_id=self.image["id"])
            from osgeo import ogr
            source = ogr.Open(path)
            feature = source.GetLayer(0).GetNextFeature()
            self.assertEqual(feature.GetField("tid"), uuid.UUID(self.image["id"]).hex)
            feature = source = None

    def test_invalid_crs_or_id_does_not_overwrite(self):
        from types import SimpleNamespace
        path = os.path.join(self.root, "protected.shp")
        Path(path).write_bytes(b"original")
        self.add_feature()
        with self.assertRaises(ValueError):
            ExportService().export(self.dialog._draft_session, "vector", path)
        self.assertEqual(Path(path).read_bytes(), b"original")
        with patch.object(ExportService, "_dissolved_parts",
                          return_value="Polygon?field=id:integer"):
            with self.assertRaises((IOError, ValueError)):
                ExportService().export(self.dialog._draft_session, "vector", path,
                                       image_id=self.image["id"])
        self.assertEqual(Path(path).read_bytes(), b"original")

    def test_empty_export_has_complete_schema(self):
        from land_cover_classification.result_attributes import RESULT_FIELD_NAMES
        self.assertTrue(self.dialog._ensure_draft_session(self.image_path))
        path = os.path.join(self.root, "empty.shp")
        ExportService().export(self.dialog._draft_session, "vector", path, image_id=self.image["id"])
        layer = QgsVectorLayer(path, "empty", "ogr")
        self.assertEqual(layer.featureCount(), 0)
        self.assertEqual([field.name() for field in layer.fields()],
                         ["class_id", "class_name", "source_id", *RESULT_FIELD_NAMES])

    def test_area_holes_overlap_small_and_large(self):
        from osgeo import ogr, osr
        from types import SimpleNamespace
        from qgis.core import QgsDistanceArea
        cases = [
            ("small", 4326, ["POLYGON((0 0,0.000001 0,0.000001 0.000001,0 0.000001,0 0))"]),
            ("large", 4326, ["POLYGON((0 0,1 0,1 1,0 1,0 0))"]),
            ("overlap", 32650, [
                "POLYGON((500000 3000000,500100 3000000,500100 3000100,500000 3000100,500000 3000000))",
                "POLYGON((500050 3000000,500150 3000000,500150 3000100,500050 3000100,500050 3000000))"]),
            ("holes", 32650, [
                "MULTIPOLYGON(((500000 3000000,500100 3000000,500100 3000100,500000 3000100,500000 3000000),"
                "(500020 3000020,500080 3000020,500080 3000080,500020 3000080,500020 3000020)),"
                "((500200 3000000,500220 3000000,500220 3000020,500200 3000020,500200 3000000)))"]),
        ]
        for name, epsg, wkts in cases:
            with self.subTest(name=name):
                path = os.path.join(self.root, name + ".gpkg")
                source = ogr.GetDriverByName("GPKG").CreateDataSource(path)
                crs = osr.SpatialReference()
                crs.ImportFromEPSG(epsg)
                layer = source.CreateLayer("draft", crs, ogr.wkbMultiPolygon)
                for wkt in wkts:
                    feature = ogr.Feature(layer.GetLayerDefn())
                    feature.SetGeometry(ogr.ForceToMultiPolygon(ogr.CreateGeometryFromWkt(wkt)))
                    layer.CreateFeature(feature)
                feature = layer = source = None
                session = SimpleNamespace(is_active=True, directory=self.root, generation_path=path)
                output = os.path.join(self.root, name + ".shp")
                ExportService().export(session, "vector", output, image_id=self.image["id"])
                result = QgsVectorLayer(output, name, "ogr")
                measure = QgsDistanceArea()
                measure.setSourceCrs(result.crs(), QgsProject.instance().transformContext())
                measure.setEllipsoid("WGS84")
                for feature in result.getFeatures():
                    self.assertAlmostEqual(feature["mj"], measure.measureArea(feature.geometry()), delta=0.00051)
                    self.assertGreater(feature["mj"], 0)
                total = sum(feature["mj"] for feature in result.getFeatures())
                if name == "small":
                    self.assertAlmostEqual(total, 0.012, places=3)
                elif name == "large":
                    self.assertAlmostEqual(total, 12308778361.469452, delta=0.001)
                elif name == "overlap":
                    self.assertAlmostEqual(total, 15000, delta=20)
                else:
                    self.assertAlmostEqual(total, 6800, delta=10)

    def test_project_page_constructed(self):
        names = [self.dialog.mainTabWidget.tabText(i)
                 for i in range(self.dialog.mainTabWidget.count())]
        self.assertEqual(names[0], "项目")
        buttons = {b.text() for b in self.panel.findChildren(QtWidgets.QPushButton)}
        self.assertIn("发布项目版本", buttons)
        self.assertFalse(any("刷新" in b or "关联" in b or "本地" in b or "连接" in b for b in buttons))
        self.assertFalse(self.panel.findChildren(QtWidgets.QLineEdit))
        self.assertEqual(self.dialog.mDemFile.filePath(), "")


if __name__ == "__main__":
    unittest.main()
