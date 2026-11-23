# -*- coding: utf-8 -*-
"""验证非模态地图属性联动及本地草稿历史。"""

from unittest.mock import patch

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QPoint, QEvent
from qgis.PyQt.QtTest import QTest
from qgis.core import QgsApplication
from qgis.gui import QgsMapToolPan

from . import test_project_workflow as fixtures
from land_cover_classification.history_dialogs import RecoveryBrowser


class IndependentWorkspacesTest(fixtures.unittest.TestCase):
    """使用真实 QGIS 图层与本地项目，隔离用户数据。"""

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

    def test_local_history_restore_cancel_and_context(self):
        self.add_feature()
        self.panel.save()
        browser = RecoveryBrowser(self.panel)
        browser.load()
        self.assertEqual(len(browser.rows), 1)
        browser.list.setCurrentRow(0)
        before = self.dialog._draft_session.generation_path
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.No):
            browser.restore()
        self.assertEqual(before, self.dialog._draft_session.generation_path)
        self.panel.image = None
        with self.assertRaises(RuntimeError):
            browser.source()
        browser.close()
        browser.deleteLater()

    def test_local_history_checks_sha_and_identity(self):
        self.add_feature()
        self.panel.save()
        browser = RecoveryBrowser(self.panel)
        browser.load()
        browser.list.setCurrentRow(0)
        browser.inspect()
        self.assertIn('检查通过', browser.status.text())
        with self.assertRaises(ValueError):
            self.panel.store.snapshot('wrong-image', browser.rows[0]['id'])
        browser.close()
        browser.deleteLater()


if __name__ == '__main__':
    fixtures.unittest.main()
