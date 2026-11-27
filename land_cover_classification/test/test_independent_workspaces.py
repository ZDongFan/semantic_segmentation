# -*- coding: utf-8 -*-
"""验证非模态地图属性联动及本地草稿历史。"""

from unittest.mock import patch

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QPoint, QEvent
from qgis.PyQt.QtTest import QTest
from qgis.core import QgsApplication, QgsCoordinateReferenceSystem
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
        browser.selected.setChecked(True)
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
        inspector = self.dialog._workflow_ui.inspector
        QTest.mouseClick(self.panel.buttons['inspect_feature'], Qt.LeftButton)
        point = canvas.getCoordinateTransform().transform(500020, 3000020)
        QTest.mouseClick(canvas.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y())))
        QgsApplication.processEvents()
        self.assertEqual(self.dialog._draft_layer.selectedFeatureCount(), 1)
        self.assertIsNone(getattr(self.panel, '_attribute_browser', None))
        self.assertEqual(inspector.values['class_name'].text(), 'landslide')
        self.assertTrue(inspector.values['mj'].text().endswith(' m²'))
        self.assertFalse(inspector.isHidden())
        QTest.keyClick(canvas, Qt.Key_Escape)
        self.assertIs(canvas.mapTool(), pan)
        self.assertTrue(inspector.isHidden())
        self.assertEqual(inspector.values['class_name'].text(), 'landslide')
        self.assertFalse(self.panel.buttons['inspect_feature'].isChecked())

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

    def test_closed_list_does_not_stop_independent_pick(self):
        self.add_feature()
        browser = self.browser()
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        browser.close()
        browser.deleteLater()
        QgsApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertIs(self.iface.mapCanvas().mapTool(), inspector.tool)
        inspector.stop_pick()
        QgsApplication.processEvents()

    def test_inspector_button_position_and_minimal_details(self):
        self.add_feature()
        self.dialog._workflow_ui.refresh()
        layout = self.panel.layout()
        preview = layout.indexOf(self.panel.buttons['preview'])
        self.assertEqual(layout.indexOf(self.panel.buttons['inspect_feature']), preview + 1)
        inspector = self.dialog._workflow_ui.inspector
        self.assertTrue(inspector.isHidden())
        self.assertEqual(len(inspector.values), 7)
        self.assertFalse(inspector.findChildren(QtWidgets.QPushButton))
        self.assertFalse(inspector.findChildren(QtWidgets.QToolButton))
        self.assertNotIn('只读', ' '.join(label.text() for label in inspector.findChildren(QtWidgets.QLabel)))
        self.panel.inspect_feature()
        inspector.picked(next(self.dialog._draft_layer.getFeatures()))
        for index in range(self.dialog.mainTabWidget.count()):
            self.dialog.mainTabWidget.setCurrentIndex(index)
            self.assertEqual(inspector.values['class_name'].text(), 'landslide')
            self.assertIsNot(inspector.parentWidget(), self.dialog.mainTabWidget.widget(index))
        self.panel.inspect_feature()
        self.assertFalse(self.panel.buttons['inspect_feature'].isChecked())
        self.panel.inspect_feature()
        QTest.keyClick(self.dialog, Qt.Key_Escape)
        self.assertFalse(self.panel._closed)
        self.assertFalse(self.panel.buttons['inspect_feature'].isChecked())
        self.assertTrue(inspector.isHidden())

    def test_cross_crs_continuous_clicks_update_same_details(self):
        self.add_feature()
        self.add_feature()
        canvas = self.iface.mapCanvas()
        layer = self.dialog._draft_layer
        canvas.resize(500, 500)
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem(4326))
        canvas.setLayers([layer])
        from qgis.core import QgsCoordinateTransform, QgsProject
        transform = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
        extent = transform.transformBoundingBox(layer.extent())
        extent.scale(2)
        canvas.setExtent(extent)
        canvas.show()
        QgsApplication.processEvents()
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        identifiers = []
        for feature in layer.getFeatures():
            center = transform.transform(feature.geometry().centroid().asPoint())
            pixel = canvas.getCoordinateTransform().transform(center)
            QTest.mouseClick(canvas.viewport(), Qt.LeftButton, Qt.NoModifier,
                             QPoint(round(pixel.x()), round(pixel.y())))
            QgsApplication.processEvents()
            identifiers.append(inspector.feature_uuid)
            self.assertEqual(layer.selectedFeatureIds(), [feature.id()])
        self.assertEqual(len(set(identifiers)), 2)
        self.assertIs(self.dialog._workflow_ui.inspector, inspector)

    def test_details_float_without_changing_plugin_layout(self):
        self.add_feature()
        self.dialog.resize(471, 850)
        self.dialog.show()
        QgsApplication.processEvents()
        previous_height = self.dialog.mainTabWidget.height()
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        inspector.picked(next(self.dialog._draft_layer.getFeatures()))
        QgsApplication.processEvents()
        self.assertFalse(self.dialog.findChildren(QtWidgets.QSplitter))
        self.assertTrue(inspector.isWindow())
        self.assertEqual(inspector.windowModality(), Qt.NonModal)
        self.assertTrue(inspector.windowFlags() & Qt.WindowCloseButtonHint)
        self.assertEqual(self.dialog.rootLayout.indexOf(inspector), -1)
        self.assertLess(inspector.height(), 190)
        self.assertEqual(self.dialog.mainTabWidget.height(), previous_height)
        self.assertTrue(inspector.screen().availableGeometry().contains(inspector.frameGeometry()))
        for first, second in [('class_name', 'source_id'), ('mj', 'perimeter_m'),
                              ('center_lon', 'center_lat')]:
            left, right = inspector.values[first], inspector.values[second]
            self.assertEqual(left.y(), right.y())
            self.assertLess(left.geometry().right(), right.x())
        inspector.values['ztz'].setText('张' * 32)
        inspector.resize(340, inspector.height())
        QgsApplication.processEvents()
        self.assertLess(inspector.width(), 341)
        self.assertLess(inspector.values['ztz'].geometry().right(), inspector.body.width())

    def test_details_close_restores_tool_and_reopens_at_upper_right(self):
        self.add_feature()
        self.add_feature()
        canvas = self.iface.mapCanvas()
        canvas.resize(700, 500)
        canvas.show()
        self.dialog.show()
        QgsApplication.processEvents()
        pan = QgsMapToolPan(canvas)
        canvas.setMapTool(pan)
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        features = list(self.dialog._draft_layer.getFeatures())
        inspector.picked(features[0])
        QgsApplication.processEvents()
        initial = inspector.pos()
        inspector.move(initial.x() + 20, initial.y() - 20)
        moved = inspector.pos()
        inspector.picked(features[1])
        self.assertEqual(inspector.pos(), moved)
        inspector.close()
        self.assertTrue(inspector.isHidden())
        self.assertIs(canvas.mapTool(), pan)
        self.assertFalse(self.panel.buttons['inspect_feature'].isChecked())
        inspector.schedule()
        QTest.qWait(100)
        self.assertTrue(inspector.isHidden())
        self.panel.inspect_feature()
        QgsApplication.processEvents()
        self.assertFalse(inspector.isHidden())
        self.assertEqual(inspector.pos(), initial)
        QTest.keyClick(inspector, Qt.Key_Escape)
        self.assertIs(canvas.mapTool(), pan)
        self.assertTrue(inspector.isHidden())

    def test_default_position_tracks_qgis_upper_right_and_available_screen(self):
        self.add_feature()
        window = self.iface.mainWindow()
        window.resize(1200, 800)
        window.move(10, 10)
        window.show()
        canvas = self.iface.mapCanvas()
        canvas.resize(700, 500)
        canvas.move(100, 100)
        canvas.show()
        QgsApplication.processEvents()
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        anchor = window.mapToGlobal(QPoint(round(window.width() * 0.94), 0))
        anchor.setY(canvas.mapToGlobal(QPoint(0, round(canvas.height() * 0.12))).y())
        available = inspector.screen().availableGeometry()
        frame = inspector.frameGeometry()
        expected_x = max(available.left(), min(anchor.x() - frame.width() - 12,
                                              available.right() - frame.width() + 1))
        expected_y = max(available.top(), min(anchor.y(), available.bottom() - frame.height() + 1))
        self.assertEqual(frame.topLeft(), QPoint(expected_x, expected_y))
        self.assertTrue(available.contains(frame))

    def test_generation_change_clears_details_and_restores_tool(self):
        self.add_feature()
        canvas = self.iface.mapCanvas()
        pan = QgsMapToolPan(canvas)
        canvas.setMapTool(pan)
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        inspector.picked(next(self.dialog._draft_layer.getFeatures()))
        from land_cover_classification.local_project import snapshot_gpkg
        session = self.dialog._draft_session
        destination = session.next_generation_path('inspector_test')
        snapshot_gpkg(session.generation_path, destination)
        self.dialog._publish_draft_generation(destination)
        QgsApplication.processEvents()
        self.assertIsNone(inspector.fid)
        self.assertEqual(inspector.values['class_name'].text(), '—')
        self.assertTrue(inspector.isHidden())
        self.assertIs(canvas.mapTool(), pan)
        self.panel.inspect_feature()
        self.assertIs(canvas.mapTool(), inspector.tool)

    def test_inspector_busy_switch_and_layer_removal(self):
        self.add_feature()
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        feature = next(self.dialog._draft_layer.getFeatures())
        inspector.picked(feature)
        self.panel.busy = True
        self.dialog._workflow_ui.refresh()
        self.assertFalse(self.panel.buttons['inspect_feature'].isEnabled())
        self.assertIsNot(self.iface.mapCanvas().mapTool(), inspector.tool)
        self.panel.busy = False
        self.dialog._discard_draft_session()
        QgsApplication.processEvents()
        self.assertIsNone(inspector.identity)
        self.assertEqual(inspector.values['class_name'].text(), '—')
        inspector.picked(feature)
        self.assertIsNone(inspector.fid)

    def test_inspector_geometry_refresh_matches_list(self):
        self.add_feature()
        self.panel.inspect_feature()
        inspector = self.dialog._workflow_ui.inspector
        layer = self.dialog._draft_layer
        feature = next(layer.getFeatures())
        inspector.picked(feature)
        original = inspector.values['center_lon'].text()
        layer.startEditing()
        geometry = feature.geometry()
        geometry.translate(1, 1)
        layer.changeGeometry(feature.id(), geometry)
        inspector.refresh()
        browser = self.browser()
        names = [name for name, _ in browser.columns]
        longitude = browser.table.item(0, names.index('center_lon')).text()
        self.assertEqual(inspector.values['center_lon'].text(), longitude + '°')
        self.assertNotEqual(inspector.values['center_lon'].text(), original)
        layer.rollBack()

    def test_inspector_dialog_destroyed_releases_canvas_callbacks(self):
        self.add_feature()
        self.panel.inspect_feature()
        tool = self.dialog._workflow_ui.inspector.tool
        canvas = self.iface.mapCanvas()
        self.dialog.close()
        self.dialog._workflow_ui.inspector.deleteLater()
        QgsApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertIsNot(canvas.mapTool(), tool)
        canvas.mapToolSet.emit(None, None)
        QTest.keyClick(canvas, Qt.Key_Escape)
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
