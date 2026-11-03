# -*- coding: utf-8 -*-
"""工作区展示与按钮条件；不改变推理、草稿和服务的数据契约。"""

import os
from functools import wraps
from inspect import signature

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import QObject, QTimer, Qt, QUrl, pyqtSlot
from qgis.PyQt.QtGui import QDesktopServices
from qgis.core import QgsProject

from . import pytorch_deps_check
from .inference_controller import BundleContractError, landslide_class_id


def refresh_workflow(method):
    """在业务状态转换前后合并刷新，兼容不创建界面的回归桩。"""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        ui = getattr(self, "_workflow_ui", None)
        if ui:
            ui.schedule()
        try:
            return method(self, *args, **kwargs)
        finally:
            if ui and not sip.isdeleted(ui.dialog):
                ui.schedule()
    # PyQt 按真实可调用参数数量裁剪 clicked(bool)，不能把无参槽变成变参槽。
    if len(signature(method).parameters) == 1:
        @wraps(method)
        def without_arguments(self):
            return wrapped(self)
        return without_arguments
    return wrapped


class WorkflowUi(QObject):
    """由现有会话状态推导展示；刷新不发起网络或重依赖探测。"""

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = d = dialog
        self.timer = QTimer(d)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.image_key = None
        self.last_export = None
        self.layer_id = None
        self.base_renderer = None
        self._build()
        for signal in (d.mainTabWidget.currentChanged,
                       d.modelCombo.currentIndexChanged,
                       d.thresholdSlider.valueChanged,
                       d.minAreaSpinBox.valueChanged,
                       d.inputFileWidget.fileChanged, d.layerCombo.layerChanged,
                       d.mDemFile.fileChanged, d.outputDirWidget.fileChanged):
            signal.connect(self.schedule)
        canvas = d.iface.mapCanvas()
        if canvas:
            canvas.mapToolSet.connect(self.schedule)
        self.refresh()

    def _label(self, text=""):
        label = QtWidgets.QLabel(text, self.dialog)
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        label.setMinimumWidth(0)
        label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        return label

    def _build(self):
        d = self.dialog
        summary = QtWidgets.QGroupBox("当前工作", d)
        layout = QtWidgets.QVBoxLayout(summary)
        self.summary = self._label()
        layout.addWidget(self.summary)
        self.input_toggle = QtWidgets.QPushButton("更换影像／查看详情", summary)
        self.input_toggle.setCheckable(True)
        self.input_toggle.toggled.connect(d.inputGroup.setVisible)
        layout.addWidget(self.input_toggle)
        d.rootLayout.insertWidget(0, summary)

        self.scope = QtWidgets.QComboBox(d)
        for text, value in (("全图", "full"), ("当前画布", "canvas"), ("绘制区域", "drawn")):
            self.scope.addItem(text, value)
        self.scope.currentIndexChanged.connect(self.schedule)
        row = QtWidgets.QFormLayout()
        row.addRow("推理范围", self.scope)
        d.inferenceLayout.insertLayout(d.inferenceLayout.indexOf(d.roiButtonsLayout), row)
        self.scope_hint = self._label()
        d.inferenceLayout.insertWidget(d.inferenceLayout.indexOf(d.roiButtonsLayout), self.scope_hint)
        d.runBtn.setText("开始推理")
        d.runBtn.clicked.disconnect()
        d.runBtn.clicked.connect(self.run)
        d.canvasInferenceBtn.hide()
        d.runDrawnRoiBtn.hide()
        d.closeBtn.hide()
        d.exportCloseBtn.hide()
        d.inferenceButtonsLayout.removeWidget(d.undoFusionBtn)
        d.progressLayout.addWidget(d.undoFusionBtn)
        d.inferenceLayout.removeWidget(d.progressGroup)
        d.inferenceLayout.insertWidget(
            d.inferenceLayout.indexOf(d.inferenceControlButtonsLayout) + 1, d.progressGroup)
        self.run_hint = self._label()
        d.inferenceLayout.insertWidget(d.inferenceLayout.indexOf(d.inferenceButtonsLayout), self.run_hint)

        self.parameter_hint = self._label()
        d.modelLayout.addWidget(self.parameter_hint)
        self.reset_parameters = QtWidgets.QPushButton("恢复当前模型默认参数", d)
        self.reset_parameters.clicked.connect(d._on_model_changed)
        d.modelLayout.addWidget(self.reset_parameters)
        self.model_details = QtWidgets.QPushButton("模型管理与详情", d)
        self.model_details.setCheckable(True)
        d.modelLayout.addWidget(self.model_details)
        self.model_details.toggled.connect(self._show_model_details)
        self.model_details.setToolTip("展开模型目录、刷新入口与模型扫描结果")
        self._show_model_details(False)

        draft = QtWidgets.QGroupBox("当前草稿", d.draftTab)
        grid = QtWidgets.QGridLayout(draft)
        self.draft_summary = self._label()
        grid.addWidget(self.draft_summary, 0, 0, 1, 2)
        self.attributes = QtWidgets.QPushButton("查看属性", draft)
        self.attributes.clicked.connect(lambda: d._project_panel.invoke(d._project_panel.preview))
        self.locate = QtWidgets.QPushButton("定位选中对象", draft)
        self.locate.clicked.connect(self._locate)
        grid.addWidget(self.attributes, 1, 0)
        grid.addWidget(self.locate, 1, 1)
        self.display = QtWidgets.QComboBox(draft)
        self.display.addItems(["填充＋轮廓", "仅轮廓", "隐藏草稿"])
        self.display.currentIndexChanged.connect(self._apply_display)
        grid.addWidget(self.display, 2, 0, 1, 2)
        self.opacity = QtWidgets.QSlider(Qt.Horizontal, draft)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(100)
        self.opacity.valueChanged.connect(self._apply_display)
        self.opacity_label = QtWidgets.QLabel("不透明度 100%", draft)
        grid.addWidget(self.opacity_label, 3, 0)
        grid.addWidget(self.opacity, 3, 1)
        self.edit_hint = self._label("使用 QGIS 原生编辑工具修改草稿边界；提交后写入本地草稿。")
        grid.addWidget(self.edit_hint, 4, 0, 1, 2)
        d.draftLayout.insertWidget(0, draft)
        self.ai_hint = self._label()
        # 窄侧栏按两列布局，长中文按钮不再挤在同一行。
        positions = (
            (d.aiHintLabel, 0, 0, 1, 2),
            (d.aiStartBtn, 1, 0, 1, 1), (d.aiStopBtn, 1, 1, 1, 1),
            (d.aiUndoPointBtn, 2, 0, 1, 1), (d.aiClearPointsBtn, 2, 1, 1, 1),
            (d.aiAppendDraftBtn, 3, 0, 1, 2),
            (d.aiReactivateBtn, 4, 0, 1, 2),
            (d.aiStatusLabel, 5, 0, 1, 2), (self.ai_hint, 6, 0, 1, 2),
        )
        for widget, row, col, rows, cols in positions:
            d.aiEditLayout.removeWidget(widget)
            d.aiEditLayout.addWidget(widget, row, col, rows, cols)

        self.export_summary = self._label()
        d.exportLayout.insertWidget(0, self.export_summary)
        self.open_export = QtWidgets.QPushButton("打开导出文件夹", d)
        self.open_export.clicked.connect(self._open_export)
        d.exportProgressLayout.addWidget(self.open_export)
        d.exportProgressBar.hide()
        d.progressBar.hide()
        d.cancelBtn.hide()
        d.statusLabel.setText("尚未开始推理。")
        d.exportStatusLabel.setText("从当前影像草稿导出文件；项目版本请到项目页发布。")

        for button in d.findChildren(QtWidgets.QPushButton):
            button.setMinimumHeight(max(28, button.sizeHint().height()))
        for tab in (d.inferenceTab, d.draftTab, d.exportTab):
            index = d.mainTabWidget.indexOf(tab)
            title = d.mainTabWidget.tabText(index)
            d.mainTabWidget.removeTab(index)
            scroll = QtWidgets.QScrollArea(d.mainTabWidget)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setWidget(tab)
            d.mainTabWidget.insertTab(index, scroll, title)
            tab.setProperty("workflow_scroll", scroll)

    def _show_model_details(self, visible):
        d = self.dialog
        for widget in (d.modelRootLabel, d.modelRootEdit, d.browseModelRootBtn,
                       d.refreshModelsBtn, d.modelStatusLabel):
            widget.setVisible(visible)

    @pyqtSlot()
    def schedule(self, *args):
        """由 Qt 管理外部信号的接收者寿命，销毁后不再访问定时器。"""
        if sip.isdeleted(self) or sip.isdeleted(self.dialog) or sip.isdeleted(self.timer):
            return
        if not self.timer.isActive():
            self.timer.start(0)

    def busy(self):
        d = self.dialog
        p = d._project_panel
        return bool(p.busy or p.switching or d._process is not None
                    or getattr(d, "_adapter_busy", False)
                    or d._ai_predicting or d._ai_appending
                    or getattr(d, "_export_busy", False)
                    or (d._ai_worker is not None and d._ai_worker.state() != 0
                        and not d._ai_worker_ready))

    def refresh(self):
        d = self.dialog
        p = d._project_panel
        if p._closed or p._host_closing:
            return
        busy = self.busy()
        path = d._input_path
        image_ready = bool(p.active and path and os.path.isfile(path)
                           and os.path.normcase(path) == os.path.normcase(p.image["image_path"]))
        layer = d._draft_layer
        draft = bool(d._draft_session.is_active and d._layer_is_usable(layer))
        associated = bool(image_ready and draft
                          and p.image.get("project_id") == p.project["id"]
                          and os.path.normcase(d._draft_session.input_path) == os.path.normcase(path))
        count = layer.featureCount() if draft else 0
        project_name = (p.project or {}).get("name", "未选择项目")
        image_name = os.path.basename(path) if path else "未选择影像"
        dem = os.path.basename(d.mDemFile.filePath()) or "未选择 DEM"
        state = "无草稿"
        if draft:
            modified = layer.isModified()
            synced = p._synced_generation == (p.image["id"], d._draft_session.generation_path) if p.image else False
            state = ("编辑尚未提交" if modified else
                     "当前草稿已同步服务端" if synced else "本地草稿待同步")
        self.summary.setText("项目：{}\n影像：{}\n{} · {}".format(project_name, image_name, dem, state))
        self.summary.setToolTip(path or "")
        image_key = ((p.project or {}).get("id"), (p.image or {}).get("id"), path)
        if image_key != self.image_key:
            self.image_key = image_key
            self.input_toggle.setChecked(not image_ready)
            d.inputGroup.setVisible(not image_ready)
            d.statusLabel.setText("尚未开始推理。")
            d.exportStatusLabel.setText("从当前影像草稿导出成果。")
            self.last_export = None
        if not image_ready:
            d.inputGroup.show()
        self.input_toggle.setEnabled(bool(p.project) and not busy)
        d.inputGroup.setEnabled(bool(p.project) and not busy)
        self.input_toggle.setText("收起输入详情" if self.input_toggle.isChecked() else "更换影像／查看详情")
        self.draft_summary.setText("当前草稿：{} 个要素 · {}".format(count, state) if draft else "当前没有草稿，可直接启动 AI 编辑或模型推理。")
        self.export_summary.setText("导出对象：{}\n{}".format(image_name, self.draft_summary.text()))

        model_ready = bool(d.modelCombo.currentData())
        if model_ready:
            try:
                landslide_class_id(d.modelCombo.currentData().get("manifest", {}))
            except (BundleContractError, AttributeError):
                model_ready = False
        runtime = pytorch_deps_check.venv_ready()
        reason = ("请先选择项目。" if not p.project else
                  "请先打开有效工作影像。" if not image_ready else
                  "请等待当前操作完成。" if busy else
                  "缺少运行环境，请重新打开插件完成安装。" if not runtime else
                  "请选择满足滑坡类别契约的模型。" if not model_ready else "")
        drawn = self.scope.currentData() == "drawn"
        has_roi = d._drawn_roi_geometry is not None and d._drawn_roi_input_path == path
        if not reason and drawn and not has_roi:
            reason = "请先绘制有效推理范围。"
        self.run_hint.setText(reason or "准备就绪，运行后将融合到当前草稿。")
        d.runBtn.setEnabled(not reason)
        d.modelGroup.setEnabled(not busy)
        self.scope.setEnabled(not busy)
        self.scope_hint.setText({
            "full": "范围：整幅影像；保留用户编辑区域。",
            "canvas": "范围：点击开始时的画布与影像交集；无交集时停止。",
            "drawn": "范围：已绘制的有效多边形。" if has_roi else "请在地图绘制范围；Esc 取消重画并保留原范围。",
        }[self.scope.currentData()])
        for button in (d.drawRoiBtn, d.clearRoiBtn):
            button.setVisible(drawn)
            button.setEnabled(image_ready and not busy and (button is d.drawRoiBtn or has_roi))
        running = d._process is not None or getattr(d, "_adapter_busy", False)
        d.progressBar.setVisible(running)
        d.cancelBtn.setVisible(running)
        d.cancelBtn.setEnabled(running)
        undo = bool(d._draft_session.fusion_snapshot_path and d._fusion_undo_available)
        d.undoFusionBtn.setEnabled(undo and not busy
                                   and not (draft and layer.isModified()))
        d.undoFusionBtn.setVisible(undo)
        d.exportRasterBtn.setEnabled(associated and not busy)
        d.workflowGroup.setEnabled(not busy)
        self.open_export.setVisible(bool(self.last_export))

        ai_active = d._ai_tool is not None
        d.aiStartBtn.setEnabled(image_ready and runtime and not busy and not ai_active)
        d.aiStopBtn.setEnabled(ai_active or d._ai_worker is not None)
        d.aiReactivateBtn.setEnabled(ai_active and not busy)
        d.aiUndoPointBtn.setEnabled(ai_active and not busy)
        d.aiClearPointsBtn.setEnabled(ai_active and not busy)
        d.aiAppendDraftBtn.setEnabled(ai_active and not busy and d._ai_preview_geometry is not None)
        in_tool = ai_active and d.iface.mapCanvas().mapTool() is d._ai_tool
        self.ai_hint.setText(
            "正在生成预览，请等待；有效预览生成后才可添加。" if d._ai_predicting else
            "当前不在 AI 点选模式，点击“回到 AI 点选模式”继续。" if ai_active and not in_tool else
            "左键标记目标内点 · 右键标记排除点 · 地图获得焦点时 E 添加到草稿" if in_tool else
            "请先选择项目和工作影像。" if not image_ready else
            "缺少运行环境，请重新打开插件完成安装。" if not runtime else
            "可直接启动 AI 编辑，无需先运行模型推理。")
        self.attributes.setEnabled(associated and not busy)
        self.locate.setEnabled(draft and layer.selectedFeatureCount() > 0)
        self.display.setEnabled(draft)
        self.opacity.setEnabled(draft)
        if draft:
            self.edit_hint.setText("原生编辑模式：{}。使用 QGIS 工具修改边界，提交后写入本地草稿。".format(
                "已开启" if layer.isEditable() else "未开启"))
        p.update_actions(associated, busy)
        defaults = getattr(d, "_parameter_defaults", None)
        custom = bool(defaults and defaults != (d.thresholdSlider.value(), d.minAreaSpinBox.value()))
        self.parameter_hint.setText("本次使用自定义参数；不会修改模型文件。" if custom else "使用当前模型默认参数。")
        self.reset_parameters.setEnabled(custom and not busy)

    def run(self):
        """沿用各入口的校验及 ROI 构建，运行时固定本次范围。"""
        self.refresh()
        if not self.dialog.runBtn.isEnabled():
            return
        d = self.dialog
        {"full": d._on_run, "canvas": d._on_run_canvas_extent,
         "drawn": d._on_run_drawn_roi}[self.scope.currentData()]()
        self.schedule()

    def bind_layer(self, layer):
        """换代后重新记录样式；选择和编辑变化驱动摘要刷新。"""
        if self.layer_id != layer.id():
            self.layer_id = layer.id()
            layer.selectionChanged.connect(self.schedule)
            layer.layerModified.connect(self.schedule)
            layer.editingStarted.connect(self.schedule)
            layer.editingStopped.connect(self.schedule)
        self.base_renderer = layer.renderer().clone() if layer.renderer() else None
        self._apply_display()

    def _apply_display(self, *args):
        d = self.dialog
        layer = d._draft_layer
        if not d._layer_is_usable(layer):
            return
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node:
            node.setItemVisibilityChecked(self.display.currentIndex() != 2)
        if self.base_renderer:
            renderer = self.base_renderer.clone()
            if self.display.currentIndex() == 1:
                from qgis.core import QgsRenderContext
                for symbol in renderer.symbols(QgsRenderContext()):
                    for part in symbol.symbolLayers():
                        if hasattr(part, "setBrushStyle"):
                            part.setBrushStyle(Qt.NoBrush)
            layer.setRenderer(renderer)
        layer.setOpacity(self.opacity.value() / 100.0)
        self.opacity_label.setText("不透明度 {}%".format(self.opacity.value()))
        layer.triggerRepaint()

    def _locate(self):
        layer = self.dialog._draft_layer
        if self.dialog._layer_is_usable(layer) and layer.selectedFeatureCount():
            self.dialog.iface.mapCanvas().zoomToSelected(layer)

    def _open_export(self):
        if self.last_export:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.last_export)))
