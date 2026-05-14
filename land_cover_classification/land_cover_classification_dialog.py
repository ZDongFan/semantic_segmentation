# -*- coding: utf-8 -*-
"""LandCoverClassification 对话框。

UI 在 Qt Designer 中绘制,保存为 `land_cover_classification_dialog_base.ui`。
本类负责连接控件信号到校验、模型扫描以及后台推理任务 SegmenterTask。
"""

import os

from qgis.PyQt import uic
from qgis.PyQt import QtCore, QtWidgets
from qgis.PyQt.QtCore import QSettings, QUrl
from qgis.PyQt.QtGui import QDesktopServices

from qgis.core import (QgsApplication, QgsMapLayerProxyModel, Qgis)
from qgis.gui import QgsFileWidget

from .inference import SegmenterTask, is_georeferenced
from .model_scan import scan as scan_models


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__),
                 'land_cover_classification_dialog_base.ui'))

SETTINGS_GROUP = "LandCoverClassification"


def _default_model_root():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "models", "semantic_segmentation")


class LandCoverClassificationDialog(QtWidgets.QDialog, FORM_CLASS):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self._task = None

        self._init_defaults()
        self._wire_signals()
        self._refresh_models()

    # --------------------------------------------------------------- 初始化
    def _init_defaults(self):
        # 模型根目录
        settings = QSettings()
        default_root = _default_model_root()
        saved_root = settings.value(
            "{}/model_root".format(SETTINGS_GROUP), default_root)
        self.modelRootEdit.setText(saved_root)

        # 图层下拉框:仅筛选栅格图层
        self.layerCombo.setFilters(QgsMapLayerProxyModel.RasterLayer)

        # 文件控件
        self.inputFileWidget.setStorageMode(QgsFileWidget.GetFile)
        self.inputFileWidget.setFilter(
            "影像文件 (*.tif *.tiff *.png *.jpg *.jpeg)")
        self.outputFileWidget.setStorageMode(QgsFileWidget.SaveFile)
        self.outputFileWidget.setFilter(
            "GeoTIFF (*.tif);;PNG (*.png);;JPEG (*.jpg)")

        # 默认使用图层输入
        self.layerRadio.setChecked(True)
        self._on_input_source_changed()

    def _wire_signals(self):
        self.browseModelRootBtn.clicked.connect(self._on_browse_model_root)
        self.openModelDirBtn.clicked.connect(self._on_open_model_dir)
        self.refreshModelsBtn.clicked.connect(self._refresh_models)
        self.modelRootEdit.editingFinished.connect(self._on_model_root_edited)

        self.layerRadio.toggled.connect(self._on_input_source_changed)
        self.fileRadio.toggled.connect(self._on_input_source_changed)
        self.layerCombo.layerChanged.connect(self._suggest_output_path)
        self.inputFileWidget.fileChanged.connect(self._suggest_output_path)

        self.runBtn.clicked.connect(self._on_run)
        self.cancelBtn.clicked.connect(self._on_cancel)
        self.closeBtn.clicked.connect(self.close)

    # ------------------------------------------------------------- 辅助方法
    def _persist_model_root(self):
        QSettings().setValue(
            "{}/model_root".format(SETTINGS_GROUP), self.modelRootEdit.text())

    def _refresh_models(self):
        self.modelCombo.clear()
        root = self.modelRootEdit.text().strip()
        if not root or not os.path.isdir(root):
            self.statusLabel.setText(
                self.tr("模型根目录不存在:{}").format(root))
            return
        models = scan_models(root)
        if not models:
            self.statusLabel.setText(
                self.tr("目录 {} 下未发现可用的分割模型").format(root))
            return
        for entry in models:
            self.modelCombo.addItem(entry["name"], entry["path"])
        self.statusLabel.setText(
            self.tr("已发现 {} 个模型").format(len(models)))

    def _on_browse_model_root(self):
        current = self.modelRootEdit.text().strip() or _default_model_root()
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.tr("选择模型根目录"), current)
        if chosen:
            self.modelRootEdit.setText(chosen)
            self._persist_model_root()
            self._refresh_models()

    def _on_open_model_dir(self):
        path = self.modelRootEdit.text().strip()
        if not path:
            return
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_model_root_edited(self):
        self._persist_model_root()
        self._refresh_models()

    def _on_input_source_changed(self):
        layer_mode = self.layerRadio.isChecked()
        self.layerCombo.setEnabled(layer_mode)
        self.inputFileWidget.setEnabled(not layer_mode)
        self._suggest_output_path()

    def _resolve_input_path(self):
        if self.layerRadio.isChecked():
            layer = self.layerCombo.currentLayer()
            if layer is None:
                return None
            return layer.source()
        path = self.inputFileWidget.filePath().strip()
        return path or None

    def _suggest_output_path(self, *args, **kwargs):
        # 不覆盖用户已经手填的路径
        if self.outputFileWidget.filePath().strip():
            return
        src = self._resolve_input_path()
        if not src or not os.path.exists(src):
            return
        base, ext = os.path.splitext(src)
        suggested = "{}_seg{}".format(base, ".tif" if ext.lower()
                                      in (".tif", ".tiff") else ext or ".png")
        self.outputFileWidget.setFilePath(suggested)

    # ------------------------------------------------------------------ 运行
    def _on_run(self):
        # 校验
        if self.modelCombo.count() == 0:
            self._warn(self.tr("尚未选择模型。"))
            return
        model_path = self.modelCombo.currentData()
        if not model_path or not os.path.isdir(model_path):
            self._warn(self.tr("所选模型路径无效。"))
            return

        input_path = self._resolve_input_path()
        if not input_path:
            self._warn(self.tr("请选择输入图层或输入文件。"))
            return
        if not os.path.exists(input_path):
            self._warn(
                self.tr("输入文件不存在:{}").format(input_path))
            return

        output_path = self.outputFileWidget.filePath().strip()
        if not output_path:
            self._warn(self.tr("请选择输出文件路径。"))
            return
        out_dir = os.path.dirname(output_path) or "."
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                self._warn(
                    self.tr("无法创建输出目录:{}").format(exc))
                return

        flags = {
            "clahe": self.claheCheck.isChecked(),
            "sharpen": self.sharpenCheck.isChecked(),
            "median": self.medianCheck.isChecked(),
            "gaussian": self.gaussianCheck.isChecked(),
        }
        georef = is_georeferenced(input_path)

        self._task = SegmenterTask(
            self.tr("地物分类推理任务"),
            model_path=model_path,
            input_path=input_path,
            output_path=output_path,
            preprocess_flags=flags,
            is_georef=georef,
        )
        self._task.progressChanged.connect(self._on_task_progress)
        self._task.taskCompleted.connect(self._on_task_completed)
        self._task.taskTerminated.connect(self._on_task_terminated)

        self.runBtn.setEnabled(False)
        self.cancelBtn.setEnabled(True)
        self.progressBar.setValue(0)
        self.statusLabel.setText(self.tr("运行中({}模式)...").format(
            "带地理坐标" if georef else "普通图像"))
        QgsApplication.taskManager().addTask(self._task)

    def _on_cancel(self):
        if self._task is not None:
            self._task.cancel()
            self.statusLabel.setText(self.tr("正在取消..."))

    def _on_task_progress(self, progress):
        self.progressBar.setValue(int(progress))

    def _on_task_completed(self):
        if self._task is None:
            return
        output_path = self._task.output_path
        self.statusLabel.setText(self.tr("完成:{}").format(output_path))
        self.progressBar.setValue(100)
        self.runBtn.setEnabled(True)
        self.cancelBtn.setEnabled(False)
        layer_name = os.path.splitext(os.path.basename(output_path))[0]
        layer = self.iface.addRasterLayer(output_path, layer_name)
        if layer is None or not layer.isValid():
            self.iface.messageBar().pushWarning(
                "地物分类",
                self.tr("结果已写出,但无法作为图层加载:{}")
                .format(output_path))
        else:
            self.iface.messageBar().pushSuccess(
                "地物分类",
                self.tr("分割结果已加载为图层:{}").format(layer_name))
        self._task = None

    def _on_task_terminated(self):
        if self._task is None:
            return
        exc = getattr(self._task, "exception", None)
        msg = str(exc) if exc else self.tr("任务已取消。")
        self.statusLabel.setText(msg)
        self.runBtn.setEnabled(True)
        self.cancelBtn.setEnabled(False)
        if exc is not None:
            self.iface.messageBar().pushCritical(
                "地物分类", msg)
        self._task = None

    # ----------------------------------------------------------------- 工具
    def _warn(self, message):
        self.statusLabel.setText(message)
        self.iface.messageBar().pushMessage(
            "地物分类", message, level=Qgis.Warning, duration=5)

    def closeEvent(self, event):
        if self._task is not None:
            self._task.cancel()
        super().closeEvent(event)
