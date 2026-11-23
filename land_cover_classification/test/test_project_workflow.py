"""独立 QGIS 中验证本地工作流，不访问用户会话或网络。"""

import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject
from qgis.gui import QgsMapCanvas, QgsMessageBar

from land_cover_classification.land_cover_classification_dialog import LandCoverClassificationDialog
from land_cover_classification.local_project import file_digest
from land_cover_classification.project_panel import reference_grid


class _Iface:
    """真实画布和消息栏，隔离当前 QGIS 编辑会话。"""

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


class ProjectWorkflowTest(unittest.TestCase):
    """使用真实 GeoPackage、图层提交及三类导出。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication.instance() or QgsApplication([], True)
        cls.app.initQgis()
        cls.app.setQuitOnLastWindowClosed(False)
        sys.path.insert(0, os.path.join(QgsApplication.pkgDataPath(), 'python', 'plugins'))
        from processing.core.Processing import Processing
        Processing.initialize()

    def setUp(self):
        from osgeo import gdal, osr
        self.directory = tempfile.TemporaryDirectory(prefix='lcc_local_qgis_')
        self.root = self.directory.name
        self.image_path = os.path.join(self.root, 'input.tif')
        raster = gdal.GetDriverByName('GTiff').Create(self.image_path, 100, 100, 3, gdal.GDT_Byte)
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
        self.panel.settings = QSettings(os.path.join(self.root, 'settings.ini'), QSettings.IniFormat)
        self.confirm = patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.Yes)
        self.confirm.start()
        self.addCleanup(self.confirm.stop)
        # 所有网络调用都必须为零，立即失败也不能被业务吞掉后视为通过。
        self.network = patch('socket.socket.connect', side_effect=AssertionError('本地工作流禁止网络'))
        self.connection = self.network.start()
        self.addCleanup(self.network.stop)
        project_dir = Path(self.root) / '本地 项目'
        project_dir.mkdir()
        self.panel.open_directory(str(project_dir), create=('测试项目', '张三'))
        self.project = self.panel.project
        self.image = self.panel.store.register_image(self.image_path, None, reference_grid(self.image_path))
        self.image_path = self.image['image_path']
        self.panel.refresh_images()
        self.panel.images.setCurrentIndex(1)
        self.panel.open_image()

    def tearDown(self):
        self.dialog.close()
        self.panel.release_project()
        QgsProject.instance().removeAllMapLayers()
        self.dialog.deleteLater()
        QgsApplication.processEvents()
        self.connection.assert_not_called()
        self.directory.cleanup()

    def add_feature(self):
        self.assertTrue(self.dialog._ensure_draft_session(self.image_path))
        if self.dialog._draft_layer is None:
            self.dialog._publish_draft_generation(self.dialog._draft_session.generation_path)
        layer = self.dialog._draft_layer
        self.assertTrue(layer.startEditing())
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromWkt('MULTIPOLYGON (((500010 3000010,500030 3000010,'
            '500030 3000030,500010 3000030,500010 3000010)))'))
        geometry = feature.geometry()
        geometry.translate(40 * layer.featureCount(), 0)
        feature.setGeometry(geometry)
        for name, value in dict(class_id=1, class_name='landslide', source_id=1,
                feature_uuid=str(uuid.uuid4()), origin='user', run_id=None).items():
            feature[name] = value
        self.assertTrue(layer.addFeature(feature))
        self.assertTrue(layer.commitChanges())

    def test_save_close_reopen_keeps_layer_and_identity(self):
        self.add_feature()
        self.panel.save()
        identifier = self.panel.image['id']
        path = self.dialog._draft_session.generation_path
        directory = self.project['directory']
        self.dialog.close()
        self.assertTrue(Path(path).is_file())
        self.assertIsNone(self.panel.store)
        self.panel.reset_close_state()
        self.panel.refresh_images()
        self.panel.images.setCurrentIndex(1)
        self.panel.open_image()
        self.assertEqual(self.panel.image['id'], identifier)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 1)
        self.assertEqual(self.project['directory'], directory)
        self.assertTrue(self.panel.is_saved)

    def test_edit_invalidates_save_and_automatic_recovers(self):
        self.add_feature()
        self.panel.save()
        old_layer = self.dialog._draft_layer
        old_id = old_layer.id()
        self.add_feature()
        self.assertEqual(self.dialog._draft_layer.id(), old_id)
        self.assertFalse(self.panel.is_saved)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 2)
        self.assertEqual(len(self.panel.store.history(self.image['id'])), 2)

    def test_failed_save_retains_generation_and_saved_snapshot(self):
        self.add_feature()
        self.panel.save()
        saved = self.panel.image['draft_id']
        self.add_feature()
        path = self.dialog._draft_session.generation_path
        with patch.object(self.panel.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.panel.save()
        self.assertTrue(Path(path).is_file())
        self.assertEqual(self.panel.image['draft_id'], saved)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 2)

    def test_registration_failure_is_visible_and_retryable(self):
        self.add_feature()
        with patch.object(self.panel.store, 'register_generation', side_effect=OSError('disk full')):
            self.panel.record_recovery()
        self.assertTrue(self.panel._save_error)
        self.dialog._workflow_ui.refresh()
        self.assertIn('保存失败', self.dialog._workflow_ui.summary.text())
        self.panel.save()
        self.assertTrue(self.panel.is_saved)

    def test_zero_features_save_preview_exports(self):
        self.dialog._ensure_draft_session(self.image_path)
        self.dialog._publish_draft_generation(self.dialog._draft_session.generation_path)
        self.panel.save()
        self.assertTrue(self.panel.is_saved)
        preview = self.panel._create_result_preview()
        table = preview.findChild(QtWidgets.QTableWidget, 'resultAttributesTable')
        self.assertEqual(table.rowCount(), 0)
        self.assertEqual(table.columnCount(), 24)
        preview.deleteLater()
        self._exports()

    def _exports(self):
        from land_cover_classification.export_service import ExportService
        from osgeo import gdal, ogr
        export = ExportService()
        session = self.dialog._draft_session
        context = self.panel.export_context()
        raster = os.path.join(self.root, 'result.tif')
        shape = os.path.join(self.root, 'result.shp')
        dxf = os.path.join(self.root, 'result.dxf')
        export.export_raster(session, raster)
        export.export_vector(session, shape, **context)
        export.export_dxf(session, dxf)
        self.assertTrue(gdal.Open(raster))
        vector = ogr.Open(shape)
        names = [field.GetName() for field in vector.GetLayer().schema]
        self.assertIn('mj', names)
        self.assertNotIn('feature_uuid', names)
        self.assertTrue(Path(dxf).is_file())
        self.assertIsNotNone(ogr.Open(dxf))

    def test_three_exports_and_standard_attributes(self):
        self.add_feature()
        self._exports()
        self.assertFalse(self.panel.is_saved)

    def test_upload_placeholder_does_not_write_or_save(self):
        self.add_feature()
        self.panel.save()
        self.add_feature()
        paths = sorted(Path(self.project['directory']).rglob('*'))
        before = {str(p): file_digest(p) for p in paths if p.is_file()}
        self.dialog._workflow_ui.refresh()
        self.assertTrue(self.panel.buttons['upload_results'].isEnabled())
        self.panel.buttons['upload_results'].click()
        after = {str(p): file_digest(p) for p in paths if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.panel.is_saved)
        self.assertIn('尚未接入', self.panel.label.text())

    def test_project_lock_and_bad_open_keep_current(self):
        other = LandCoverClassificationDialog(self.iface)
        other._project_panel._started = True
        try:
            with self.assertRaisesRegex(RuntimeError, '实例'):
                other._project_panel.open_directory(self.project['directory'])
            self.add_feature()
            previous = self.dialog._draft_session.generation_path
            bad = Path(self.root) / 'bad'
            bad.mkdir()
            with self.assertRaises(Exception):
                self.panel.open_directory(str(bad))
            self.assertEqual(self.dialog._draft_session.generation_path, previous)
        finally:
            other.close()
            other.deleteLater()

    def test_layer_input_mode_survives_registration(self):
        from qgis.core import QgsRasterLayer
        path = str(Path(self.root) / 'second.tif')
        shutil.copyfile(self.image_path, path)
        layer = QgsRasterLayer(path, '第二景')
        QgsProject.instance().addMapLayer(layer)
        d = self.dialog
        d.fileRadio.blockSignals(True)
        d.layerRadio.blockSignals(True)
        d.layerCombo.blockSignals(True)
        try:
            d.layerRadio.setChecked(True)
            d.layerCombo.setLayer(layer)
        finally:
            d.fileRadio.blockSignals(False)
            d.layerRadio.blockSignals(False)
            d.layerCombo.blockSignals(False)
        self.panel.bind_input(path)
        self.assertTrue(d.layerRadio.isChecked())
        self.assertEqual(os.path.normcase(d._input_path), os.path.normcase(path))

    def test_restore_preserves_before_and_failure_keeps_current(self):
        from land_cover_classification import recovery
        self.add_feature()
        self.panel.save()
        target = self.panel.image['draft_id']
        self.add_feature()
        current = self.dialog._draft_session.generation_path
        with patch.object(self.dialog, '_publish_draft_generation', side_effect=RuntimeError('fault')):
            with self.assertRaises(RuntimeError):
                recovery.restore_source(self.panel, target)
        self.assertEqual(self.dialog._draft_session.generation_path, current)
        recovery.restore_source(self.panel, target)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 1)
        self.assertFalse(self.panel.is_saved)
        self.assertIn('restore_backup', [r['save_kind'] for r in self.panel.store.history(self.image['id'])])

    def test_remove_rejoin_and_missing_input_management(self):
        self.add_feature()
        self.panel.change_image_membership(self.panel.image, True)
        self.assertIsNone(self.panel.image)
        row = self.panel.store.images(include_removed=True)[0]
        self.panel.change_image_membership(row, False)
        self.assertEqual(self.panel.store.images()[0]['id'], self.image['id'])
        from land_cover_classification.history_dialogs import ImageManager
        QgsProject.instance().removeAllMapLayers()
        self.iface.layer = None
        Path(self.image_path).rename(Path(self.root) / 'moved.tif')
        browser = ImageManager(self.panel)
        browser.load()
        self.assertEqual(len(browser.rows), 1)
        browser.close()
        browser.deleteLater()


if __name__ == '__main__':
    unittest.main()
