# -*- coding: utf-8 -*-
"""使用真实 QGIS 控件验证工作区状态和交互边界。"""

import os
import shutil
import unittest
from unittest.mock import Mock, patch

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsApplication, QgsGeometry, QgsPointXY, QgsProject
from . import test_project_workflow as fixtures


class WorkflowUiTest(unittest.TestCase):
    """复用合成影像和真实本地项目，不启动推理或修改用户项目。"""

    setUpClass = classmethod(fixtures.ProjectWorkflowTest.setUpClass.__func__)
    setUp = fixtures.ProjectWorkflowTest.setUp
    tearDown = fixtures.ProjectWorkflowTest.tearDown
    add_feature = fixtures.ProjectWorkflowTest.add_feature

    def test_empty_generation_is_actionable(self):
        self.dialog._ensure_draft_session(self.image_path)
        self.dialog._publish_draft_generation(self.dialog._draft_session.generation_path)
        self.dialog._workflow_ui.refresh()
        self.assertEqual(self.dialog._draft_layer.featureCount(), 0)
        for name in ("save", "preview"):
            self.assertTrue(self.panel.buttons[name].isEnabled(), name)
        self.assertFalse(self.panel.buttons['upload_results'].isEnabled())
        self.panel.save()
        ui = self.dialog._workflow_ui
        ui.refresh()
        self.assertTrue(self.panel.buttons['upload_results'].isEnabled())
        self.assertTrue(self.dialog.exportRasterBtn.isEnabled())

    def test_missing_draft_disables_only_current_draft_actions(self):
        self.panel.image = None
        self.dialog._workflow_ui.refresh()
        self.assertFalse(self.panel.buttons["save"].isEnabled())
        self.assertFalse(self.panel.buttons["preview"].isEnabled())
        self.assertFalse(self.panel.buttons['upload_results'].isEnabled())
        self.assertFalse(self.dialog.aiStartBtn.isEnabled())

    def test_busy_blocks_mutations_and_recovers_from_real_conditions(self):
        self.add_feature()
        ui = self.dialog._workflow_ui
        self.panel.busy = True
        ui.refresh()
        self.assertFalse(self.panel.buttons["save"].isEnabled())
        self.assertFalse(self.dialog.exportRasterBtn.isEnabled())
        self.assertFalse(self.dialog.inputGroup.isEnabled())
        self.panel.busy = False
        self.panel.image = None
        ui.refresh()
        self.assertFalse(self.panel.buttons["save"].isEnabled())
        self.assertFalse(self.dialog.runBtn.isEnabled())

    def test_model_and_export_feedback_are_isolated(self):
        ui = self.dialog._workflow_ui
        ui.refresh()
        self.dialog.exportStatusLabel.setText("导出结果")
        self.dialog._refresh_models()
        self.assertEqual(self.dialog.exportStatusLabel.text(), "导出结果")
        self.dialog.statusLabel.setText("推理阶段")
        self.assertEqual(self.dialog.exportStatusLabel.text(), "导出结果")
        self.assertTrue(self.dialog.progressBar.isHidden())

    def test_project_failure_and_recovery_stay_in_project(self):
        ui = self.dialog._workflow_ui
        ui.refresh()
        self.dialog.exportStatusLabel.setText("导出结果")
        self.dialog.statusLabel.setText("推理结果")
        failure = Mock(side_effect=RuntimeError("连接失败"))
        self.panel.invoke(failure)
        self.assertIn("连接失败", self.panel.label.text())
        self.assertEqual(self.dialog.exportStatusLabel.text(), "导出结果")
        self.assertEqual(self.dialog.statusLabel.text(), "推理结果")
        self.panel.invoke(self.panel.refresh)
        self.assertNotIn("连接失败", self.panel.label.text())

    def test_save_marker_invalidates_after_edit(self):
        self.add_feature()
        self.panel.save()
        ui = self.dialog._workflow_ui
        ui.refresh()
        self.assertIn("已本地保存", ui.summary.text())
        layer = self.dialog._draft_layer
        layer.startEditing()
        feature = next(layer.getFeatures())
        layer.changeGeometry(feature.id(), QgsGeometry.fromWkt(
            "MULTIPOLYGON (((500011 3000010,500030 3000010,500030 3000030,500011 3000010)))"))
        ui.refresh()
        self.assertIn("尚未提交", ui.summary.text())
        layer.commitChanges()
        ui.refresh()
        self.assertIn("待保存", ui.summary.text())

    def test_single_run_dispatch_and_drawn_roi_guard(self):
        d = self.dialog
        ui = d._workflow_ui
        d.modelCombo.clear()
        d.modelCombo.addItem("测试模型", {"manifest": {"class_names": ["background", "landslide"]}})
        with patch("land_cover_classification.workflow_ui.pytorch_deps_check.venv_ready", return_value=True):
            with patch.object(d, "_on_run") as full, patch.object(d, "_on_run_canvas_extent") as canvas, patch.object(d, "_on_run_drawn_roi") as drawn:
                ui.scope.setCurrentIndex(0)
                ui.run()
                full.assert_called_once()
                ui.scope.setCurrentIndex(1)
                ui.run()
                canvas.assert_called_once()
                ui.scope.setCurrentIndex(2)
                ui.run()
                drawn.assert_not_called()
                d._drawn_roi_geometry = QgsGeometry.fromPolygonXY([[
                    QgsPointXY(0, 0), QgsPointXY(1, 0), QgsPointXY(0, 1), QgsPointXY(0, 0)]])
                d._drawn_roi_input_path = self.image_path
                ui.run()
                drawn.assert_called_once()
                d._drawn_roi_input_path = "another-image"
                ui.refresh()
                self.assertFalse(d.runBtn.isEnabled())

    def test_failed_or_cancelled_run_does_not_enable_undo(self):
        self.add_feature()
        d = self.dialog
        d._create_fusion_snapshot()
        d._workflow_ui.refresh()
        self.assertFalse(d.undoFusionBtn.isEnabled())
        d._fusion_undo_available = True
        d._workflow_ui.refresh()
        self.assertTrue(d.undoFusionBtn.isEnabled())
        d._invalidate_fusion_undo()
        d._workflow_ui.refresh()
        self.assertFalse(d.undoFusionBtn.isEnabled())

    def test_default_draft_display_survives_generation_publish(self):
        self.add_feature()
        d = self.dialog
        node = QgsProject.instance().layerTreeRoot().findLayer(d._draft_layer.id())
        self.assertTrue(node.itemVisibilityChecked())
        self.assertAlmostEqual(d._draft_layer.opacity(), 1.0)
        d._publish_draft_generation(d._draft_session.generation_path)
        self.assertTrue(node.itemVisibilityChecked())
        self.assertAlmostEqual(d._draft_layer.opacity(), 1.0)

    def test_input_and_model_details_stay_expanded_after_state_changes(self):
        d = self.dialog
        for busy in (False, True, False):
            self.panel.busy = busy
            d._workflow_ui.refresh()
            for widget in (d.inputGroup, d.modelRootLabel, d.modelRootEdit,
                           d.browseModelRootBtn, d.refreshModelsBtn, d.modelStatusLabel):
                self.assertFalse(widget.isHidden())
            self.assertEqual(d.inputGroup.isEnabled(), not busy)
            self.assertEqual(d.modelGroup.isEnabled(), not busy)

    def test_common_fields_keep_full_values_and_headers(self):
        self.add_feature()
        preview = self.panel._create_result_preview()
        try:
            table = preview.findChild(QtWidgets.QTableWidget, "resultAttributesTable")
            combo = preview.findChild(QtWidgets.QComboBox, "attributeFieldView")
            self.assertEqual(table.columnCount(), 24)
            visible = [table.horizontalHeaderItem(i).data(Qt.UserRole)
                       for i in range(table.columnCount()) if not table.isColumnHidden(i)]
            self.assertEqual(set(visible), {"class_name", "mj", "perimeter_m", "center_lon", "center_lat", "ztz", "source_id"})
            names = [table.horizontalHeaderItem(i).data(Qt.UserRole) for i in range(table.columnCount())]
            longitude = table.item(0, names.index("center_lon"))
            latitude = table.item(0, names.index("center_lat"))
            self.assertAlmostEqual(longitude.data(Qt.UserRole), 117.0002, places=3)
            self.assertAlmostEqual(latitude.data(Qt.UserRole), 27.1226, places=3)
            self.assertEqual(longitude.text(), '{:.7f}'.format(longitude.data(Qt.UserRole)))
            self.assertEqual(latitude.text(), '{:.7f}'.format(latitude.data(Qt.UserRole)))
            combo.setCurrentIndex(1)
            self.assertFalse(any(table.isColumnHidden(i) for i in range(24)))
        finally:
            preview.deleteLater()

    def test_selecting_image_keeps_current_tab(self):
        """通过真实文件输入信号切换影像，项目页和推理页均保持当前位置。"""
        d = self.dialog
        for index in range(2):
            if index == 0:
                d.mainTabWidget.setCurrentIndex(0)
            else:
                d.mainTabWidget.setCurrentWidget(d.inferenceTab.property('workflow_scroll') or d.inferenceTab)
            current = d.mainTabWidget.currentWidget()
            path = os.path.join(self.root, 'selected_{}.tif'.format(index))
            shutil.copyfile(self.image_path, path)
            d.inputFileWidget.setFilePath(path)
            QgsApplication.processEvents()
            self.assertEqual(os.path.normcase(d._input_path), os.path.normcase(path))
            self.assertIs(d.mainTabWidget.currentWidget(), current)

    def test_successful_fusion_keeps_current_tab(self):
        """融合完成仍更新进度和撤销状态，但不切换用户所在页签。"""
        self.add_feature()
        d = self.dialog
        d.mainTabWidget.setCurrentWidget(d.inferenceTab.property('workflow_scroll') or d.inferenceTab)
        current = d.mainTabWidget.currentWidget()
        d._dem_usage = {'mode': 'not_provided'}
        with patch.object(d, '_merge_inference_candidate_with_retry') as merge:
            d._finish_inference_fusion('candidate.gpkg', 'test-run')
        merge.assert_called_once_with('candidate.gpkg', 'test-run')
        self.assertIs(d.mainTabWidget.currentWidget(), current)
        self.assertEqual(d.progressBar.value(), 100)
        self.assertTrue(d._fusion_undo_available)
        self.assertIn('已融合', d.statusLabel.text())

    def test_tab_navigation_and_narrow_layout(self):
        self.add_feature()
        d = self.dialog
        d._workflow_ui.refresh()
        d.resize(400, 780)
        d.show()
        QgsApplication.processEvents()
        d._switch_to_draft_tab()
        self.assertIs(d.mainTabWidget.currentWidget().widget(), d.draftTab)
        d._switch_to_export_tab()
        self.assertIs(d.mainTabWidget.currentWidget().widget(), d.exportTab)
        self.assertGreater(d.maximumWidth(), 480)
        folder = os.environ.get("LCC_UI_SCREENSHOTS")
        if folder:
            os.makedirs(folder, exist_ok=True)
            for index in range(d.mainTabWidget.count()):
                d.mainTabWidget.setCurrentIndex(index)
                QgsApplication.processEvents()
                d.grab().save(os.path.join(folder, "tab_{}.png".format(index)))
            preview = self.panel._create_result_preview()
            preview.show()
            QgsApplication.processEvents()
            preview.grab().save(os.path.join(folder, "attributes.png"))
            preview.close()
            preview.deleteLater()


    def test_canvas_roi_transforms_crs_and_rejects_disjoint_extent(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsRectangle
        canvas = self.iface.mapCanvas()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem(4326))
        converter = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem(32650), QgsCoordinateReferenceSystem(4326),
            QgsProject.instance())
        canvas.setExtent(converter.transformBoundingBox(QgsRectangle(500020, 3000020, 500040, 3000040)))
        roi = self.dialog._canvas_intersection_roi(self.image_path)
        self.assertEqual(roi["mode"], "canvas_intersection")
        self.assertLess(roi["bounds"][0], roi["bounds"][2])
        canvas.setExtent(QgsRectangle(0, 0, 1, 1))
        self.assertIsNone(self.dialog._canvas_intersection_roi(self.image_path))

    def test_cancelled_process_does_not_fuse_or_report_failure(self):
        d = self.dialog
        d._process = Mock()
        d._cancel_requested = True
        with patch.object(d, "_on_process_output"), patch.object(d, "_consume_process_output"), \
                patch.object(d, "_on_process_completed") as completed, \
                patch.object(d, "_log_inference_error") as error, \
                patch.object(d, "_cleanup_process"):
            d._on_process_finished(1, 1)
            completed.assert_not_called()
            error.assert_not_called()
            self.assertIn("已取消", d.statusLabel.text())
        d._process = None


    def test_real_buttons_preserve_zero_argument_slot_signatures(self):
        """点击真实 QPushButton，捕获 Qt 槽异常而非直接调用业务方法。"""
        d = self.dialog
        errors = []
        with patch("sys.excepthook", side_effect=lambda *args: errors.append(args)):
            with patch.object(d._ai_controller, "startable", return_value="测试前置条件"):
                d.aiStartBtn.setEnabled(True)
                d.aiStartBtn.click()
                d._warn.assert_called_with("测试前置条件")
            d._warn.reset_mock()
            d.exportRasterBtn.setEnabled(True)
            d.exportRasterBtn.click()
            d._warn.assert_called_with("当前没有可导出的草稿。")
            d.refreshModelsBtn.click()
            d.aiAppendDraftBtn.setEnabled(True)
            d.aiAppendDraftBtn.click()
            self.assertIn("没有有效", d.aiStatusLabel.text())
            QgsApplication.processEvents()
        self.assertEqual(errors, [])


    def test_destroyed_panel_disconnects_external_canvas_signal(self):
        """窗口销毁后真实画布切换工具不得再访问旧定时器。"""
        from qgis.PyQt import sip
        from qgis.gui import QgsMapToolPan
        d = fixtures.LandCoverClassificationDialog(self.iface)
        d._project_panel._started = True
        ui = d._workflow_ui
        timer = ui.timer
        errors = []
        with patch("sys.excepthook", side_effect=lambda *args: errors.append(args)):
            sip.delete(d)
            self.assertTrue(sip.isdeleted(timer))
            pan = QgsMapToolPan(self.iface.canvas)
            self.iface.canvas.setMapTool(pan)
            QgsApplication.processEvents()
            ui.schedule()
            self.iface.canvas.unsetMapTool(pan)
        self.assertEqual(errors, [])


    def test_ai_append_button_reuses_binding_without_self_blocking(self):
        """真实按钮进入追加状态后仍可写入同一影像的下一代草稿。"""
        self.add_feature()
        d = self.dialog
        d._ai_preview_geometry = QgsGeometry.fromWkt(
            "MULTIPOLYGON (((500050 3000050,500060 3000050,500060 3000060,500050 3000060,500050 3000050)))")
        generation = d._draft_session.generation_path
        d.aiAppendDraftBtn.setEnabled(True)
        d.aiAppendDraftBtn.click()
        self.assertNotEqual(d._draft_session.generation_path, generation)
        self.assertIsNone(d._ai_preview_geometry)
        self.assertTrue(any(f["origin"] == "user" for f in d._draft_layer.getFeatures()))

    def test_busy_still_rejects_changing_image_binding(self):
        self.dialog._ai_appending = True
        try:
            with self.assertRaisesRegex(RuntimeError, "等待"):
                self.panel.bind_input(self.image_path + ".other")
        finally:
            self.dialog._ai_appending = False


    def test_placeholder_has_no_remote_actions(self):
        """项目页不再暴露远程发布、对比或请求线程入口。"""
        self.assertNotIn('publish', self.panel.buttons)
        self.assertNotIn('history', self.panel.buttons)
        self.assertFalse(hasattr(self.panel, '_client'))
        self.assertFalse(hasattr(self.panel, 'call'))


if __name__ == "__main__":
    unittest.main()
