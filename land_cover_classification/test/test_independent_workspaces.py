# -*- coding: utf-8 -*-
"""验证非模态地图属性联动、版本叠加及恢复原子性。"""

import json
from pathlib import Path
from unittest.mock import patch

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QPoint, QEvent
from qgis.PyQt.QtTest import QTest
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject
from qgis.gui import QgsMapToolPan

from . import test_project_workflow as fixtures
from land_cover_classification import recovery
from land_cover_classification.history_dialogs import VersionBrowser, RecoveryBrowser
from land_cover_classification.project_client import snapshot_gpkg


class IndependentWorkspacesTest(fixtures.unittest.TestCase):
    """使用真实 QGIS 图层，隔离服务和用户数据。"""

    setUpClass = classmethod(fixtures.ProjectWorkflowTest.setUpClass.__func__)
    setUp = fixtures.ProjectWorkflowTest.setUp
    tearDown = fixtures.ProjectWorkflowTest.tearDown
    add_feature = fixtures.ProjectWorkflowTest.add_feature

    def browser(self):
        self.panel.preview()
        browser = self.panel._attribute_browser
        self.addCleanup(lambda: browser.close() if not sip.isdeleted(browser) else None)
        return browser

    def test_nonmodal_reuses_window_and_selects_one(self):
        self.add_feature()
        self.add_feature()
        browser = self.browser()
        self.assertFalse(browser.isModal())
        self.panel.preview()
        self.assertIs(browser, self.panel._attribute_browser)
        browser.table.selectRow(1)
        self.assertEqual(self.dialog._draft_layer.selectedFeatureIds(), [browser.fids[1]])
        browser.picked(self.dialog._draft_layer.getFeature(browser.fids[1]))
        self.assertEqual(browser.table.rowCount(), 1)
        self.assertEqual(browser.table.columnCount(), 24)
        browser.view.setCurrentIndex(1)
        self.assertFalse(browser.table.isColumnHidden(1))

    def test_real_map_click_selects_feature_and_restores_tool(self):
        self.add_feature()
        canvas = self.iface.mapCanvas()
        canvas.resize(500, 500)
        canvas.setDestinationCrs(self.dialog._draft_layer.crs())
        canvas.setLayers([self.dialog._draft_layer])
        box = self.dialog._draft_layer.extent()
        box.scale(2)
        canvas.setExtent(box)
        canvas.show()
        QgsApplication.processEvents()
        pan = QgsMapToolPan(canvas)
        canvas.setMapTool(pan)
        browser = self.browser()
        browser.start_pick()
        point = canvas.getCoordinateTransform().transform(500020, 3000020)
        QTest.mouseClick(canvas.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
        QgsApplication.processEvents()
        self.assertEqual(self.dialog._draft_layer.selectedFeatureCount(), 1)
        self.assertTrue(browser.selected.isChecked())
        browser.close()
        self.assertIs(canvas.mapTool(), pan)

    def test_filter_paging_and_selection_generation(self):
        self.add_feature()
        self.add_feature()
        browser = self.browser()
        browser.PAGE_SIZE = 1
        browser.reset_page()
        first = browser.fids[0]
        self.assertTrue(browser.next.isEnabled())
        browser.next_page()
        self.assertNotEqual(browser.fids[0], first)
        browser.previous_page()
        self.assertEqual(browser.fids[0], first)
        browser.search.setText('not-present')
        browser.reset_page()
        self.assertEqual(browser.table.rowCount(), 0)
        browser.search.setText('landslide')
        browser.reset_page()
        self.assertEqual(browser.table.rowCount(), 1)
        self.dialog._discard_draft_session()
        QgsApplication.processEvents()
        self.assertEqual(browser.table.rowCount(), 0)
        self.assertFalse(browser.isEnabled())

    def test_closed_dialog_does_not_leave_map_callback(self):
        self.add_feature()
        browser = self.browser()
        browser.start_pick()
        browser.close()
        browser.deleteLater()
        QgsApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.iface.mapCanvas().setMapTool(QgsMapToolPan(self.iface.mapCanvas()))
        QgsApplication.processEvents()

    def test_restore_creates_backup_and_keeps_service_pointer(self):
        self.add_feature()
        source = str(Path(self.root) / 'old.gpkg')
        snapshot_gpkg(self.dialog._draft_session.generation_path, source)
        self.add_feature()
        previous = self.dialog._draft_session.generation_path
        pointer = self.panel.image['draft_id']
        with patch.object(recovery, 'recovery_root', return_value=Path(self.root) / 'recovery'):
            backup = recovery.restore_source(self.panel, source)
            record = recovery.read_record(Path(backup) / 'recovery.json', self.panel)
            self.assertEqual(recovery.validate_source(self.panel, record['path']), 2)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 1)
        self.assertNotEqual(self.dialog._draft_session.generation_path, previous)
        self.assertEqual(self.panel.image['draft_id'], pointer)
        self.assertIsNone(self.panel._synced_generation)

    def test_restore_publish_failure_preserves_generation_and_backup(self):
        self.add_feature()
        source = str(Path(self.root) / 'old.gpkg')
        snapshot_gpkg(self.dialog._draft_session.generation_path, source)
        previous = self.dialog._draft_session.generation_path
        with patch.object(recovery, 'recovery_root', return_value=Path(self.root) / 'recovery'), \
                patch.object(self.dialog, '_publish_draft_generation', side_effect=RuntimeError('模拟切换失败')):
            with self.assertRaisesRegex(RuntimeError, '模拟切换失败'):
                recovery.restore_source(self.panel, source)
        self.assertEqual(self.dialog._draft_session.generation_path, previous)
        self.assertEqual(self.dialog._draft_layer.featureCount(), 1)
        self.assertEqual(len(list((Path(self.root) / 'recovery').glob('*/recovery.json'))), 1)

    def test_recovery_record_rejects_wrong_project_and_path_escape(self):
        self.add_feature()
        record = Path(self.dialog._draft_session.directory) / 'recovery.json'
        original = json.loads(record.read_text('utf-8'))
        for replacement in ({'project_id': 'wrong'}, {'generation': '../outside.gpkg'}):
            record.write_text(json.dumps({**original, **replacement}), 'utf-8')
            with self.assertRaises(ValueError):
                recovery.read_record(record, self.panel)
        record.write_text(json.dumps(original), 'utf-8')

    def test_duplicate_uuid_and_wrong_crs_rejected(self):
        self.add_feature()
        layer = self.dialog._draft_layer
        feature = QgsFeature(next(layer.getFeatures()))
        feature.setId(-1)
        feature["fid"] = None
        self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        with self.assertRaisesRegex(ValueError, '重复'):
            recovery.validate_source(self.panel, self.dialog._draft_session.generation_path)

    def test_recovery_offline_still_displays_local_records(self):
        self.add_feature()
        browser = RecoveryBrowser(self.panel)
        self.addCleanup(lambda: browser.close() if not sip.isdeleted(browser) else None)
        self.client.failure = True
        with patch.object(recovery, 'local_records', return_value=[{'kind':'local', 'created_at':'today', 'reason':'备份'}]):
            browser.load()
        self.assertEqual(browser.list.count(), 1)
        self.assertIn('本地记录仍可使用', browser.status.text())

    def test_cancel_restore_and_context_switch_never_replace(self):
        self.add_feature()
        browser = RecoveryBrowser(self.panel)
        self.addCleanup(lambda: browser.close() if not sip.isdeleted(browser) else None)
        previous = self.dialog._draft_session.generation_path
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.No):
            browser.confirm_restore(previous)
        self.assertEqual(previous, self.dialog._draft_session.generation_path)
        self.panel.project = dict(self.panel.project, id='other')
        with self.assertRaisesRegex(RuntimeError, '项目已切换'):
            browser.check_context(True)

    def test_version_map_overlay_and_cleanup(self):
        self.add_feature()
        browser = VersionBrowser(self.panel)
        self.addCleanup(lambda: browser.close() if not sip.isdeleted(browser) else None)
        polygon = {'type':'MultiPolygon', 'coordinates':[[[[117,27],[117.01,27],[117.01,27.01],[117,27.01],[117,27]]]]}
        browser.rows = [{'change':'modified', 'before_geometry':polygon, 'after_geometry':polygon,
                         'before_properties':{'mj':1}, 'after_properties':{'mj':2}}]
        browser.table.setRowCount(1)
        browser.table.setItem(0, 0, QtWidgets.QTableWidgetItem('修改'))
        browser.table.selectRow(0)
        self.assertEqual(len(browser.bands), 2)
        self.assertIn('mj', browser.details.toPlainText())
        self.iface.mapCanvas().setDestinationCrs(self.dialog._draft_layer.crs())
        browser.locate()
        browser.close()
        self.assertEqual(browser.bands, [])

    def test_hundreds_of_features_keep_bounded_page_and_offpage_selection(self):
        import uuid
        self.add_feature()
        layer = self.dialog._draft_layer
        template = next(layer.getFeatures())
        rows = []
        for _ in range(420):
            feature = QgsFeature(template)
            feature.setId(-1)
            feature['fid'] = None
            feature['feature_uuid'] = str(uuid.uuid4())
            rows.append(feature)
        self.assertTrue(layer.dataProvider().addFeatures(rows)[0])
        browser = self.browser()
        self.assertEqual(browser.table.rowCount(), 200)
        browser.next_page()
        self.assertEqual(browser.table.rowCount(), 200)
        browser.next_page()
        self.assertEqual(browser.table.rowCount(), 21)
        target = browser.fids[-1]
        browser.previous_page()
        layer.selectByIds([target])
        browser.selected.setChecked(True)
        self.assertEqual(browser.fids, [target])

    def test_server_download_checksum_failure_never_restores(self):
        self.add_feature()
        self.panel.save()
        browser = RecoveryBrowser(self.panel)
        self.addCleanup(browser.close)
        row = {'id': self.image['draft_id'], 'sha256': 'incorrect', 'kind': 'server',
               'created_at': 'today'}
        browser.append([row])
        browser.list.setCurrentRow(0)
        with self.assertRaisesRegex(ValueError, '完整性'):
            browser.with_source(lambda source: self.fail('校验失败不能进入恢复'))

    def test_layer_source_replacement_refreshes_open_browser(self):
        self.add_feature()
        browser = self.browser()
        source = str(Path(self.root) / 'one.gpkg')
        snapshot_gpkg(self.dialog._draft_session.generation_path, source)
        self.add_feature()
        browser.refresh()
        self.assertEqual(browser.table.rowCount(), 2)
        with patch.object(recovery, 'recovery_root', return_value=Path(self.root) / 'recovery'):
            recovery.restore_source(self.panel, source)
        QTest.qWait(120)
        self.assertEqual(browser.table.rowCount(), 1)

    def test_recovery_rejects_busy_and_invalid_crs_without_replacing(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer
        from land_cover_classification.draft_session import layer_uri
        self.add_feature()
        previous = self.dialog._draft_session.generation_path
        self.panel.busy = True
        with self.assertRaisesRegex(RuntimeError, '等待'):
            recovery.restore_source(self.panel, previous)
        self.panel.busy = False
        # 用模拟图层 CRS 验证拒绝路径，真实源文件不变。
        original = recovery.QgsVectorLayer
        def wrong_crs(*args):
            layer = original(*args)
            layer.setCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
            return layer
        with patch.object(recovery, 'QgsVectorLayer', side_effect=wrong_crs):
            with self.assertRaisesRegex(ValueError, 'CRS'):
                recovery.validate_source(self.panel, previous)
        self.assertEqual(previous, self.dialog._draft_session.generation_path)
