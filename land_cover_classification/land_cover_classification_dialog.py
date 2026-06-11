# -*- coding: utf-8 -*-
"""LandCoverClassification 对话框。"""

import json
import os
import sys
import tempfile

from qgis.PyQt import sip, uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import (
    QProcess,
    QProcessEnvironment,
    QSettings,
    QUrl,
    QVariant,
)
from qgis.PyQt.QtGui import QColor, QDesktopServices

from qgis.core import (
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
    Qgis,
)
from qgis.gui import QgsFileWidget

from . import sam_deps_check
from .ai_segment_tool import AiSegmentMapTool
from .inference import is_georeferenced
from .model_scan import get_model_info
from .model_scan import scan as scan_models


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__),
                 "land_cover_classification_dialog_base.ui"))

SETTINGS_GROUP = "LandCoverClassification"
FIELD_CLASS_ID = "class_id"
FIELD_CLASS_NAME = "class_name"
FIELD_REVIEW_STATUS = "review_status"
FIELD_SOURCE_ID = "source_id"
BACKGROUND_CLASS_NAMES = {"background"}
DRAFT_SIMPLIFY_PIXEL_TOLERANCE = 5
AI_PREVIEW_SIMPLIFY_PIXEL_TOLERANCE = 8
AI_PREVIEW_MAX_VERTICES = 6000
OUTPUT_FORMAT_RASTER = "raster"
OUTPUT_FORMAT_VECTOR = "vector"
STATUS_PENDING = "待确认"
STATUS_CONFIRMED = "已确认"

SAM_DEFAULT_BACKEND = sam_deps_check.DEFAULT_BACKEND
LANDSLIDE_CLASS_NAME = "landslide"


def _default_model_root():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "models", "semantic_segmentation")


def _python_executable():
    candidates = [sys.executable]
    osgeo_root = _osgeo4w_root()
    if osgeo_root:
        candidates.insert(0, os.path.join(osgeo_root, "bin", "python.exe"))
        apps_dir = os.path.join(osgeo_root, "apps")
        if os.path.isdir(apps_dir):
            for name in sorted(os.listdir(apps_dir)):
                if name.lower().startswith("python"):
                    candidates.append(os.path.join(apps_dir, name,
                                                   "python.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return sys.executable


def _osgeo4w_root():
    root = os.environ.get("OSGEO4W_ROOT")
    if root and os.path.isfile(os.path.join(root, "bin", "o4w_env.bat")):
        return root

    candidates = []
    prefix = _qgis_prefix_path()
    if prefix:
        apps_dir = os.path.dirname(prefix)
        candidates.append(os.path.dirname(apps_dir))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.dirname(exe_dir))
    for path in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.basename(path).lower() == "bin":
            candidates.append(os.path.dirname(path))

    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "bin",
                                                    "o4w_env.bat")):
            return candidate
    return None


def _qgis_prefix_path():
    prefix = QgsApplication.prefixPath() or os.environ.get("QGIS_PREFIX_PATH")
    if prefix and os.path.isdir(prefix):
        return prefix

    root = os.environ.get("OSGEO4W_ROOT")
    apps_dir = os.path.join(root, "apps") if root else None
    if apps_dir and os.path.isdir(apps_dir):
        for name in sorted(os.listdir(apps_dir)):
            candidate = os.path.join(apps_dir, name)
            if name.lower().startswith("qgis") and os.path.isdir(candidate):
                return candidate
    return None


def _qgis_relative_path(*parts):
    prefix = _qgis_prefix_path()
    if prefix:
        path = os.path.join(prefix, *parts)
        if os.path.isdir(path):
            return path
    return None


def _qgis_pkg_data_path():
    try:
        path = QgsApplication.pkgDataPath()
    except AttributeError:
        path = None
    return path if path and os.path.isdir(path) else None


def _qgis_qt_plugin_path():
    return _qgis_relative_path("qtplugins")


def _qgis_python_path():
    candidates = [
        _qgis_relative_path("python"),
        os.path.join(_qgis_prefix_path() or "", "share", "qgis", "python"),
        os.path.join(_qgis_pkg_data_path() or "", "python"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def _cmd_executable():
    system_root = os.environ.get("SystemRoot")
    candidate = (os.path.join(system_root, "System32", "cmd.exe")
                 if system_root else None)
    if candidate and os.path.exists(candidate):
        return candidate
    return "cmd.exe"


def _cuda_paths():
    roots = []
    for key, value in os.environ.items():
        if key in ("CUDA_PATH", "CUDA_HOME") or key.startswith("CUDA_PATH_"):
            if value and os.path.isdir(value) and value not in roots:
                roots.append(value)
    for path in os.environ.get("PATH", "").split(os.pathsep):
        parent = os.path.dirname(path)
        if "cuda" in path.lower() and os.path.isdir(parent):
            root = parent if os.path.basename(path).lower() == "bin" else path
            if root not in roots:
                roots.append(root)

    paths = []
    for root in roots:
        for rel in ("bin", "libnvvp"):
            path = os.path.join(root, rel)
            if os.path.isdir(path) and path not in paths:
                paths.append(path)
    return roots, paths


def _prepend_env_paths(env, key, parts):
    existing = env.value(key) or os.environ.get(key, "")
    merged = []
    for path in parts + existing.split(os.pathsep):
        if path and path not in merged:
            merged.append(path)
    env.insert(key, os.pathsep.join(merged))


def _prepend_path_parts(env, parts):
    _prepend_env_paths(env, "PATH", parts)


def _insert_existing(env, key, value):
    if value:
        env.insert(key, value)


def _env_passthrough_keys():
    keys = [
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "USERNAME",
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "GDAL_DATA",
        "GDAL_DRIVER_PATH",
        "GEOTIFF_CSV",
        "PROJ_LIB",
        "QT_PLUGIN_PATH",
        "QGIS_PREFIX_PATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
    ]
    for key in os.environ:
        if key.startswith(("CUDA", "CUDNN", "NVIDIA")):
            keys.append(key)
    return keys


def _shape_base_files(path):
    base, _ext = os.path.splitext(path)
    return [
        base + ext for ext in (
            ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx")
    ]


def _remove_existing_shapefile(path):
    for candidate in _shape_base_files(path):
        if os.path.exists(candidate):
            os.remove(candidate)


class _MirroredLabel:

    def __init__(self, *widgets):
        self._widgets = widgets

    def setText(self, text):
        for widget in self._widgets:
            widget.setText(text)

    def text(self):
        return self._widgets[0].text()


class _MirroredProgressBar:

    def __init__(self, *widgets):
        self._widgets = widgets

    def setValue(self, value):
        for widget in self._widgets:
            widget.setValue(value)

    def value(self):
        return self._widgets[0].value()


class LandCoverClassificationDialog(QtWidgets.QDialog, FORM_CLASS):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.statusLabel = _MirroredLabel(
            self.statusLabel, self.exportStatusLabel)
        self.progressBar = _MirroredProgressBar(
            self.progressBar, self.exportProgressBar)
        self.iface = iface
        self._process = None
        self._params_file = None
        self._launcher_file = None
        self._process_error_message = ""
        self._label_path = None
        self._draft_path = None
        self._draft_layer = None
        self._draft_layer_id = None
        self._final_layer = None
        self._final_layer_id = None
        self._input_layer = None
        self._input_path = None
        self._class_labels = []

        self._ai_tool = None
        self._ai_previous_tool = None
        self._ai_worker = None
        self._ai_worker_ready = False
        self._ai_request_seq = 0
        self._ai_pending_id = None
        self._ai_responses = {}
        self._ai_image_path = None
        self._ai_image_loaded = False
        self._ai_image_size = None
        self._ai_image_geotransform = None
        self._ai_image_crs = None
        self._ai_preview_geometry = None
        self._draft_layer_original_name = None
        self._ai_buffer = ""
        self._ai_predicting = False
        self._ai_queued_points = None

        self._init_defaults()
        self._wire_signals()
        self._refresh_models()

    def _init_defaults(self):
        settings = QSettings()
        default_root = _default_model_root()
        saved_root = settings.value(
            "{}/model_root".format(SETTINGS_GROUP), default_root)
        self.modelRootEdit.setText(saved_root)

        self.layerCombo.setFilters(QgsMapLayerProxyModel.RasterLayer)

        self.inputFileWidget.setStorageMode(QgsFileWidget.GetFile)
        self.inputFileWidget.setFilter(
            "影像文件 (*.tif *.tiff *.png *.jpg *.jpeg)")
        self.outputDirWidget.setStorageMode(QgsFileWidget.GetDirectory)
        self.outputFileWidget.setFilter("Shapefile 文件 (*.shp)")
        self.outputFormatCombo.setItemData(0, OUTPUT_FORMAT_RASTER)
        self.outputFormatCombo.setItemData(1, OUTPUT_FORMAT_VECTOR)
        self.rasterFileWidget.setFilter("GeoTIFF 影像 (*.tif *.tiff)")

        self.layerRadio.setChecked(True)
        self.confirmSelectedBtn.setEnabled(False)
        self.confirmAllBtn.setEnabled(False)
        self.exportRasterBtn.setEnabled(False)
        self._on_input_source_changed()

    def _wire_signals(self):
        self.browseModelRootBtn.clicked.connect(self._on_browse_model_root)
        self.openModelDirBtn.clicked.connect(self._on_open_model_dir)
        self.refreshModelsBtn.clicked.connect(self._refresh_models)
        self.modelRootEdit.editingFinished.connect(self._on_model_root_edited)

        self.layerRadio.toggled.connect(self._on_input_source_changed)
        self.fileRadio.toggled.connect(self._on_input_source_changed)
        self.layerCombo.layerChanged.connect(self._suggest_output_paths)
        self.inputFileWidget.fileChanged.connect(self._suggest_output_paths)

        self.runBtn.clicked.connect(self._on_run)
        self.cancelBtn.clicked.connect(self._on_cancel)
        self.confirmSelectedBtn.clicked.connect(self._on_confirm_selected)
        self.confirmAllBtn.clicked.connect(self._on_confirm_all)
        self.exportRasterBtn.clicked.connect(self._on_export_raster)
        self.closeBtn.clicked.connect(self.close)
        self.exportCloseBtn.clicked.connect(self.close)

        self.aiStartBtn.clicked.connect(self._on_ai_start)
        self.aiStopBtn.clicked.connect(self._on_ai_stop)
        self.aiUndoPointBtn.clicked.connect(self._on_ai_undo_point)
        self.aiClearPointsBtn.clicked.connect(self._on_ai_clear_points)
        self.aiAppendDraftBtn.clicked.connect(self._on_ai_append_draft)

        QgsProject.instance().layersWillBeRemoved.connect(
            self._on_layers_will_be_removed)

    def _persist_model_root(self):
        QSettings().setValue(
            "{}/model_root".format(SETTINGS_GROUP), self.modelRootEdit.text())

    def _refresh_models(self):
        self.modelCombo.clear()
        root = self.modelRootEdit.text().strip()
        if not root or not os.path.isdir(root):
            self.statusLabel.setText("模型根目录不存在:{}".format(root))
            return
        models = scan_models(root)
        if not models:
            self.statusLabel.setText("目录 {} 下未发现可用的分割模型".format(root))
            return
        for entry in models:
            self.modelCombo.addItem(entry["name"], entry["path"])
        self.statusLabel.setText("已发现 {} 个模型".format(len(models)))

    def _on_browse_model_root(self):
        current = self.modelRootEdit.text().strip() or _default_model_root()
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择模型根目录", current)
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
        self._suggest_output_paths()

    def _resolve_input_path(self):
        if self.layerRadio.isChecked():
            layer = self.layerCombo.currentLayer()
            if layer is None:
                return None
            return self._normalize_input_path(layer.source())
        path = self.inputFileWidget.filePath().strip()
        return self._normalize_input_path(path) or None

    def _normalize_input_path(self, path):
        if not path:
            return None
        if os.path.exists(path):
            return path
        candidate = path.split("|", 1)[0]
        if candidate and os.path.exists(candidate):
            return candidate
        return path

    def _resolve_input_layer(self):
        if self.layerRadio.isChecked():
            return self.layerCombo.currentLayer()
        return None

    def _suggest_output_paths(self, *args, **kwargs):
        src = self._resolve_input_path()
        if not src or not os.path.exists(src):
            return
        base, _ext = os.path.splitext(src)
        if not self.outputFileWidget.filePath().strip():
            self.outputFileWidget.setFilePath("{}_final.shp".format(base))
        if not self.rasterFileWidget.filePath().strip():
            self.rasterFileWidget.setFilePath("{}_final.tif".format(base))

    def _on_run(self):
        if self.modelCombo.count() == 0:
            self._warn("尚未选择模型。")
            return
        model_path = self.modelCombo.currentData()
        if not model_path or not os.path.isdir(model_path):
            self._warn("所选模型路径无效。")
            return

        input_path = self._resolve_input_path()
        if not input_path:
            self._warn("请选择输入图层或输入文件。")
            return
        if not os.path.exists(input_path):
            self._warn("输入文件不存在:{}".format(input_path))
            return

        vector_path = self.outputFileWidget.filePath().strip()
        if not vector_path:
            self._warn("请选择最终 Shapefile 输出路径。")
            return
        if os.path.splitext(vector_path)[1].lower() != ".shp":
            vector_path = os.path.splitext(vector_path)[0] + ".shp"
            self.outputFileWidget.setFilePath(vector_path)
        out_dir = os.path.dirname(vector_path) or "."
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                self._warn("无法创建输出目录:{}".format(exc))
                return

        if os.path.exists(vector_path):
            answer = QtWidgets.QMessageBox.question(
                self, "覆盖确认",
                "最终 Shapefile 已存在，确认后会覆盖同名结果。是否继续？")
            if answer != QtWidgets.QMessageBox.Yes:
                return
            try:
                _remove_existing_shapefile(vector_path)
            except OSError as exc:
                self._warn("无法覆盖已有 Shapefile:{}".format(exc))
                return

        self._class_labels = self._read_class_labels(model_path)
        self._input_layer = self._resolve_input_layer()
        self._input_path = input_path
        self._final_layer = None
        self._final_layer_id = None

        flags = {
            "clahe": self.claheCheck.isChecked(),
            "sharpen": self.sharpenCheck.isChecked(),
            "median": self.medianCheck.isChecked(),
            "gaussian": self.gaussianCheck.isChecked(),
        }
        georef = is_georeferenced(input_path)
        self._start_inference_process(model_path, input_path, flags, georef)

    def _read_class_labels(self, model_path):
        try:
            info = get_model_info(model_path)
            labels = info.get("_Attributes", {}).get("labels") or []
            return [str(label) for label in labels]
        except Exception:
            return []

    def _start_inference_process(self, model_path, input_path, flags, georef):
        self._process_error_message = ""
        fd, self._label_path = tempfile.mkstemp(
            prefix="lcc_label_", suffix=".tif")
        os.close(fd)
        params = {
            "model_path": model_path,
            "input_path": input_path,
            "output_path": self._label_path,
            "preprocess_flags": flags,
            "is_georef": georef,
        }
        fd, self._params_file = tempfile.mkstemp(
            prefix="lcc_params_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(params, handle, ensure_ascii=False)

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.setProcessEnvironment(self._clean_process_environment())
        self._process.setWorkingDirectory(tempfile.gettempdir())
        self._process.readyReadStandardOutput.connect(
            self._on_process_output)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)

        self.runBtn.setEnabled(False)
        self.cancelBtn.setEnabled(True)
        self.confirmSelectedBtn.setEnabled(False)
        self.confirmAllBtn.setEnabled(False)
        self.exportRasterBtn.setEnabled(False)
        self.progressBar.setValue(0)
        self.statusLabel.setText("运行中({}模式)...".format(
            "带地理坐标" if georef else "普通图像"))

        runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "inference_runner.py")
        launcher = self._create_windows_launcher(runner, self._params_file)
        if launcher:
            self._process.start(_cmd_executable(), ["/c", launcher])
        else:
            self._process.start(_python_executable(), [runner, "--params",
                                                 self._params_file])

    def _clean_process_environment(self):
        env = QProcessEnvironment()
        for key in _env_passthrough_keys():
            value = os.environ.get(key)
            if value:
                env.insert(key, value)

        root = _osgeo4w_root()
        _insert_existing(env, "OSGEO4W_ROOT", root)
        _insert_existing(env, "QGIS_PREFIX_PATH", _qgis_prefix_path())

        cuda_roots, cuda_paths = _cuda_paths()
        if cuda_roots:
            env.insert("CUDA_PATH", cuda_roots[0])

        system_root = os.environ.get("SystemRoot")
        path_parts = [
            os.path.join(system_root, "System32") if system_root else None,
            system_root,
            os.path.join(system_root, "System32", "Wbem")
            if system_root else None,
            os.path.join(root, "bin") if root else None,
            _qgis_relative_path("bin"),
            os.path.join(root, "apps", "Qt5", "bin") if root else None,
        ] + cuda_paths
        _prepend_path_parts(env, [path for path in path_parts if path])
        qgis_python = _qgis_python_path()
        if qgis_python:
            _prepend_env_paths(env, "PYTHONPATH", [qgis_python])
        qt_plugin_paths = [
            _qgis_qt_plugin_path(),
            os.path.join(root, "apps", "qt5", "plugins") if root else None,
        ]
        _prepend_env_paths(
            env, "QT_PLUGIN_PATH",
            [path for path in qt_plugin_paths if path and os.path.isdir(path)])
        return env

    def _create_windows_launcher(self, runner, params_file):
        if os.name != "nt":
            return None
        root = _osgeo4w_root()
        if not root:
            return None
        fd, self._launcher_file = tempfile.mkstemp(
            prefix="lcc_runner_", suffix=".bat")
        os.close(fd)
        qgis_prefix = _qgis_prefix_path()
        qgis_bin = _qgis_relative_path("bin")
        qgis_plugins = _qgis_qt_plugin_path()
        qgis_python = _qgis_python_path()
        qt_bin = os.path.join(root, "apps", "Qt5", "bin")
        qt_plugins = os.path.join(root, "apps", "qt5", "plugins")
        cuda_roots, cuda_paths = _cuda_paths()
        extra_path_value = ";".join(
            path for path in [qgis_bin, qt_bin] + cuda_paths
            if path and os.path.isdir(path))
        qt_plugin_value = ";".join(
            path for path in [qgis_plugins, qt_plugins]
            if path and os.path.isdir(path))
        cuda_root_value = cuda_roots[0] if cuda_roots else ""
        lines = [
            "@echo off",
            "setlocal",
            'set "QGIS_ROOT={}"'.format(root),
            'set "PYTHONPATH="',
            'call "%QGIS_ROOT%\\bin\\o4w_env.bat"',
        ]
        if cuda_root_value:
            lines.append('set "CUDA_PATH={}"'.format(cuda_root_value))
        if extra_path_value:
            lines.append('set "PATH={};%PATH%"'.format(extra_path_value))
        if qgis_prefix:
            lines.append('set "QGIS_PREFIX_PATH={}"'.format(
                qgis_prefix.replace("\\", "/")))
        if qt_plugin_value:
            lines.append('set "QT_PLUGIN_PATH={}"'.format(qt_plugin_value))
        if qgis_python:
            lines.append('set "PYTHONPATH={};%PYTHONPATH%"'.format(
                qgis_python))
        lines.extend([
            'set "GDAL_FILENAME_IS_UTF8=YES"',
            'set "VSI_CACHE=TRUE"',
            'set "VSI_CACHE_SIZE=1000000"',
            '"%QGIS_ROOT%\\bin\\python.exe" "{}" --params "{}"'.format(
                runner, params_file),
            "exit /b %ERRORLEVEL%",
        ])
        with open(self._launcher_file, "w", encoding="mbcs") as handle:
            handle.write("\r\n".join(lines))
        return self._launcher_file

    def _on_cancel(self):
        if self._process is not None:
            self._process.kill()
            self.statusLabel.setText("正在取消...")

    def _on_process_output(self):
        if self._process is None:
            return
        data = bytes(self._process.readAllStandardOutput()).decode(
            "utf-8", errors="replace")
        for line in data.splitlines():
            if not line.startswith("LCC_EVENT "):
                continue
            try:
                payload = json.loads(line[len("LCC_EVENT "):])
            except ValueError:
                continue
            event = payload.get("event")
            if event == "progress":
                self.progressBar.setValue(int(payload.get("value", 0)))
            elif event == "error":
                self._process_error_message = payload.get("message", "")
            elif event == "done":
                self._label_path = payload.get("label_path", self._label_path)

    def _on_process_finished(self, exit_code, exit_status):
        if self._process is None:
            return
        if exit_code == 0 and exit_status == QProcess.NormalExit:
            self._on_process_completed()
        else:
            msg = self._process_error_message
            if not msg:
                msg = "推理子进程异常退出(exit_code={})。".format(exit_code)
            self.statusLabel.setText(msg)
            self.iface.messageBar().pushCritical("地物分类", msg)
        self._cleanup_process()

    def _on_process_completed(self):
        try:
            self.statusLabel.setText("正在生成可编辑草稿层...")
            self.progressBar.setValue(100)
            self._load_reference_input_layer()
            self._draft_layer = self._create_draft_layer(self._label_path)
            self._place_layer_above_input(self._draft_layer)
            self.confirmSelectedBtn.setEnabled(True)
            self.confirmAllBtn.setEnabled(True)
            self.exportRasterBtn.setEnabled(True)
            self._switch_to_export_tab()
            self.statusLabel.setText("草稿层已生成,请编辑后确认对象。")
            self.iface.messageBar().pushSuccess(
                "地物分类", "可编辑草稿层已加载,可在编辑与导出页启动 AI 编辑。")
        except Exception as exc:  # noqa: BLE001
            self._warn("生成草稿层失败:{}".format(exc))

    def _switch_to_export_tab(self):
        try:
            export_index = self.mainTabWidget.indexOf(self.exportTab)
            if export_index >= 0:
                self.mainTabWidget.setCurrentIndex(export_index)
        except AttributeError:
            pass

    def _load_reference_input_layer(self):
        if self._input_layer is not None and self._input_layer.isValid():
            return
        if not self._input_path:
            return
        name = os.path.splitext(os.path.basename(self._input_path))[0]
        layer = self.iface.addRasterLayer(self._input_path, name)
        if layer is not None and layer.isValid():
            self._input_layer = layer

    def _create_draft_layer(self, label_path):
        draft_fd, self._draft_path = tempfile.mkstemp(
            prefix="lcc_draft_", suffix=".gpkg")
        os.close(draft_fd)
        if os.path.exists(self._draft_path):
            os.remove(self._draft_path)
        self._polygonize_to_gpkg(label_path, self._draft_path)
        layer = QgsVectorLayer(
            "{}|layername=draft".format(self._draft_path),
            "地物分类草稿",
            "ogr")
        if not layer.isValid():
            raise IOError("无法加载草稿图层:{}".format(self._draft_path))
        QgsProject.instance().addMapLayer(layer, False)
        self._draft_layer_id = layer.id()
        self._prepare_draft_fields(layer)
        self._apply_vector_style(layer, draft=True)
        return layer

    def _polygonize_to_gpkg(self, label_path, gpkg_path):
        from osgeo import gdal, ogr, osr

        src = gdal.Open(label_path)
        if src is None:
            raise IOError("无法打开类别栅格:{}".format(label_path))
        driver = ogr.GetDriverByName("GPKG")
        ds = driver.CreateDataSource(gpkg_path)
        if ds is None:
            src = None
            raise IOError("无法创建草稿 GeoPackage:{}".format(gpkg_path))
        srs = None
        proj = src.GetProjection()
        if proj:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(proj)
        layer = ds.CreateLayer("draft", srs=srs, geom_type=ogr.wkbPolygon)
        layer.CreateField(ogr.FieldDefn(FIELD_CLASS_ID, ogr.OFTInteger))
        field_index = layer.GetLayerDefn().GetFieldIndex(FIELD_CLASS_ID)
        result = gdal.Polygonize(src.GetRasterBand(1), None, layer,
                                 field_index, [], callback=None)
        ds = None
        src = None
        if result != 0:
            raise IOError("类别栅格矢量化失败。")

    def _prepare_draft_fields(self, layer):
        provider = layer.dataProvider()
        existing = [field.name() for field in layer.fields()]
        new_fields = []
        if FIELD_CLASS_NAME not in existing:
            new_fields.append(QgsField(FIELD_CLASS_NAME, QVariant.String,
                                       "", 80))
        if FIELD_REVIEW_STATUS not in existing:
            new_fields.append(QgsField(FIELD_REVIEW_STATUS, QVariant.String,
                                       "", 20))
        if FIELD_SOURCE_ID not in existing:
            new_fields.append(QgsField(FIELD_SOURCE_ID, QVariant.Int))
        if new_fields:
            provider.addAttributes(new_fields)
            layer.updateFields()

        updates = []
        background_fids = []
        geometry_updates = []
        tolerance = self._draft_simplify_tolerance()
        for feature in layer.getFeatures():
            class_id = self._safe_int(feature[FIELD_CLASS_ID], 0)
            if self._is_background_class_id(class_id):
                background_fids.append(feature.id())
                continue
            geometry = self._simplified_draft_geometry(feature.geometry(),
                                                       tolerance)
            if geometry is not None:
                geometry_updates.append((feature.id(), geometry))
            updates.append({
                "fid": feature.id(),
                FIELD_CLASS_NAME: self._class_name(class_id),
                FIELD_REVIEW_STATUS: STATUS_PENDING,
                FIELD_SOURCE_ID: int(feature.id()),
            })
        if updates or background_fids or geometry_updates:
            layer.startEditing()
            if background_fids:
                layer.deleteFeatures(background_fids)
            for fid, geometry in geometry_updates:
                layer.changeGeometry(fid, geometry)
            for item in updates:
                fid = item.pop("fid")
                for name, value in item.items():
                    layer.changeAttributeValue(
                        fid, layer.fields().indexFromName(name), value)
            layer.commitChanges()

    def _draft_simplify_tolerance(self):
        from osgeo import gdal

        if not self._label_path:
            return float(DRAFT_SIMPLIFY_PIXEL_TOLERANCE)
        ds = gdal.Open(self._label_path)
        if ds is None:
            return float(DRAFT_SIMPLIFY_PIXEL_TOLERANCE)
        gt = ds.GetGeoTransform()
        ds = None
        if gt is None:
            return float(DRAFT_SIMPLIFY_PIXEL_TOLERANCE)
        pixel_width = (gt[1] ** 2 + gt[2] ** 2) ** 0.5
        pixel_height = (gt[4] ** 2 + gt[5] ** 2) ** 0.5
        pixel_size = max(pixel_width, pixel_height)
        if pixel_size <= 0:
            return float(DRAFT_SIMPLIFY_PIXEL_TOLERANCE)
        return DRAFT_SIMPLIFY_PIXEL_TOLERANCE * pixel_size

    def _simplified_draft_geometry(self, geometry, tolerance):
        if geometry is None or geometry.isEmpty() or tolerance <= 0:
            return None
        simplified = geometry.simplify(tolerance)
        if simplified is None or simplified.isEmpty():
            return None
        if not simplified.isGeosValid():
            simplified = simplified.makeValid()
        if simplified is None or simplified.isEmpty():
            return None
        return simplified

    def _ai_preview_simplify_tolerance(self):
        gt = self._ai_image_geotransform
        if gt:
            pixel_width = (gt[1] ** 2 + gt[2] ** 2) ** 0.5
            pixel_height = (gt[4] ** 2 + gt[5] ** 2) ** 0.5
            pixel_size = max(pixel_width, pixel_height)
            if pixel_size > 0:
                return AI_PREVIEW_SIMPLIFY_PIXEL_TOLERANCE * pixel_size
        return float(AI_PREVIEW_SIMPLIFY_PIXEL_TOLERANCE)

    def _place_layer_above_input(self, layer):
        root = QgsProject.instance().layerTreeRoot()
        input_node = (root.findLayer(self._input_layer.id())
                      if self._input_layer is not None else None)
        if input_node is None:
            root.insertLayer(0, layer)
            return
        parent = input_node.parent()
        index = parent.children().index(input_node)
        parent.insertLayer(index, layer)

    def _on_confirm_selected(self):
        if not self._ensure_draft_layer():
            return
        features = list(self._draft_layer.selectedFeatures())
        if not features:
            self._warn("请先在草稿层选择要确认的对象。")
            return
        self._confirm_features(features, delete_missing=False)

    def _on_confirm_all(self):
        if not self._ensure_draft_layer():
            return
        features = list(self._draft_layer.getFeatures())
        if not features:
            self._warn("草稿层中没有可确认对象。")
            return
        self._confirm_features(features, delete_missing=True)

    def _confirm_features(self, features, delete_missing):
        if not self._commit_layer_if_needed(self._draft_layer):
            return
        final_layer = self._ensure_final_layer()
        if final_layer is None:
            return
        if not final_layer.isEditable():
            if not final_layer.startEditing():
                self._warn("最终结果图层无法进入编辑状态。")
                return

        final_by_source = self._final_features_by_source(final_layer)
        current_sources = set()
        confirmed_count = 0
        for feature in features:
            source_id = self._safe_int(feature[FIELD_SOURCE_ID], feature.id())
            current_sources.add(source_id)
            class_id = self._safe_int(feature[FIELD_CLASS_ID], 0)
            if self._is_background_class_id(class_id):
                if source_id in final_by_source:
                    final_layer.deleteFeature(final_by_source[source_id].id())
                continue
            attrs = self._final_attrs_from_draft(feature, source_id)
            if source_id in final_by_source:
                final_feature = final_by_source[source_id]
                final_layer.changeGeometry(final_feature.id(),
                                           feature.geometry())
                for name, value in attrs.items():
                    final_layer.changeAttributeValue(
                        final_feature.id(),
                        final_layer.fields().indexFromName(name),
                        value)
            else:
                new_feature = QgsFeature(final_layer.fields())
                new_feature.setGeometry(feature.geometry())
                for name, value in attrs.items():
                    new_feature.setAttribute(name, value)
                final_layer.addFeature(new_feature)
            confirmed_count += 1

        if delete_missing:
            draft_sources = {
                self._safe_int(feature[FIELD_SOURCE_ID], feature.id())
                for feature in self._draft_layer.getFeatures()
                if not self._is_background_class_id(
                    self._safe_int(feature[FIELD_CLASS_ID], 0))
            }
            delete_ids = [
                feature.id()
                for source_id, feature in final_by_source.items()
                if source_id not in draft_sources
            ]
            if delete_ids:
                final_layer.deleteFeatures(delete_ids)

        if not final_layer.commitChanges():
            self._warn("写入最终结果图层失败:{}".format(
                final_layer.commitErrors()))
            final_layer.rollBack()
            return
        final_layer.triggerRepaint()
        self._mark_draft_confirmed(current_sources)
        self._apply_vector_style(final_layer, draft=False)
        self.statusLabel.setText("已确认 {} 个对象。".format(confirmed_count))
        self.iface.messageBar().pushSuccess(
            "地物分类", "最终结果图层已更新。")

    def _ensure_draft_layer(self):
        if not self._layer_is_usable(self._draft_layer):
            self._warn("当前没有可用的草稿图层。")
            return False
        return True

    def _commit_layer_if_needed(self, layer):
        if not self._layer_is_usable(layer):
            return False
        if layer.isEditable():
            if not layer.commitChanges():
                self._warn("提交草稿层编辑失败:{}".format(layer.commitErrors()))
                layer.rollBack()
                return False
        return True

    def _layer_is_usable(self, layer):
        if layer is None:
            return False
        if sip.isdeleted(layer):
            return False
        return layer.isValid()

    def _on_layers_will_be_removed(self, layer_ids):
        if self._draft_layer_id in layer_ids:
            self._draft_layer = None
            self._draft_layer_id = None
            self._draft_layer_original_name = None
        if self._final_layer_id in layer_ids:
            self._final_layer = None
            self._final_layer_id = None

    def _ensure_final_layer(self):
        path = self.outputFileWidget.filePath().strip()
        if not path:
            self._warn("请选择最终 Shapefile 输出路径。")
            return None
        if os.path.splitext(path)[1].lower() != ".shp":
            path = os.path.splitext(path)[0] + ".shp"
            self.outputFileWidget.setFilePath(path)

        if self._layer_is_usable(self._final_layer):
            return self._final_layer
        self._final_layer = None
        self._final_layer_id = None
        if os.path.exists(path):
            layer = QgsVectorLayer(path, "最终分割结果", "ogr")
            if layer.isValid():
                self._final_layer = layer
                self._final_layer_id = layer.id()
                if QgsProject.instance().mapLayer(layer.id()) is None:
                    QgsProject.instance().addMapLayer(layer)
                self._ensure_final_fields(layer)
                self._apply_vector_style(layer, draft=False)
                return layer
        self._create_empty_final_shapefile(path)
        layer = QgsVectorLayer(path, "最终分割结果", "ogr")
        if not layer.isValid():
            self._warn("无法创建最终 Shapefile:{}".format(path))
            return None
        QgsProject.instance().addMapLayer(layer)
        self._final_layer = layer
        self._final_layer_id = layer.id()
        self._apply_vector_style(layer, draft=False)
        return layer

    def _create_empty_final_shapefile(self, path):
        for candidate in _shape_base_files(path):
            if os.path.exists(candidate):
                os.remove(candidate)
        geom_name = QgsWkbTypes.displayString(self._draft_layer.wkbType())
        crs = self._draft_layer.crs()
        uri = geom_name
        if crs.isValid() and crs.authid():
            uri = "{}?crs={}".format(geom_name, crs.authid())
        memory = QgsVectorLayer(uri, "final_template", "memory")
        provider = memory.dataProvider()
        provider.addAttributes(self._final_fields())
        memory.updateFields()
        self._write_vector_layer(memory, path)

    def _ensure_final_fields(self, layer):
        provider = layer.dataProvider()
        existing = [field.name() for field in layer.fields()]
        new_fields = [
            field for field in self._final_fields()
            if field.name() not in existing
        ]
        if new_fields:
            provider.addAttributes(new_fields)
            layer.updateFields()

    def _final_fields(self):
        return [
            QgsField(FIELD_CLASS_ID, QVariant.Int),
            QgsField(FIELD_CLASS_NAME, QVariant.String, "", 80),
            QgsField(FIELD_SOURCE_ID, QVariant.Int),
        ]

    def _write_vector_layer(self, layer, path):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "ESRI Shapefile"
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsProject.instance().transformContext(), options)
        if isinstance(result, tuple):
            error = result[0]
        else:
            error = result
        if error != QgsVectorFileWriter.NoError:
            raise IOError("写出 Shapefile 失败:{}".format(path))

    def _final_features_by_source(self, layer):
        mapping = {}
        if layer.fields().indexFromName(FIELD_SOURCE_ID) < 0:
            return mapping
        for feature in layer.getFeatures():
            source_id = self._safe_int(feature[FIELD_SOURCE_ID], None)
            if source_id is not None:
                mapping[source_id] = feature
        return mapping

    def _final_attrs_from_draft(self, feature, source_id):
        class_id = self._safe_int(feature[FIELD_CLASS_ID], 0)
        return {
            FIELD_CLASS_ID: class_id,
            FIELD_CLASS_NAME: self._class_name(class_id),
            FIELD_SOURCE_ID: source_id,
        }

    def _mark_draft_confirmed(self, source_ids):
        if not source_ids:
            return
        layer = self._draft_layer
        if not layer.isEditable():
            if not layer.startEditing():
                self._warn("草稿层无法进入编辑状态。")
                return
        status_idx = layer.fields().indexFromName(FIELD_REVIEW_STATUS)
        for feature in layer.getFeatures():
            source_id = self._safe_int(feature[FIELD_SOURCE_ID], feature.id())
            if source_id in source_ids:
                layer.changeAttributeValue(
                    feature.id(), status_idx, STATUS_CONFIRMED)
        layer.commitChanges()
        layer.triggerRepaint()

    def _on_export_raster(self):
        if not self._layer_is_usable(self._final_layer):
            self._warn("请先确认对象，生成最终结果图层。")
            return
        if self._final_layer.isEditable() and not self._final_layer.commitChanges():
            self._warn("提交最终结果图层编辑失败:{}".format(
                self._final_layer.commitErrors()))
            self._final_layer.rollBack()
            return
        path = self.rasterFileWidget.filePath().strip()
        if not path:
            self._warn("请选择栅格图像导出路径。")
            return
        if os.path.splitext(path)[1].lower() not in (".tif", ".tiff"):
            path = os.path.splitext(path)[0] + ".tif"
            self.rasterFileWidget.setFilePath(path)
        out_dir = os.path.dirname(path) or "."
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        try:
            self._rasterize_final_layer(path)
            self.iface.addRasterLayer(path, os.path.splitext(
                os.path.basename(path))[0])
            self.iface.messageBar().pushSuccess(
                "地物分类", "最终栅格图像已导出。")
        except Exception as exc:  # noqa: BLE001
            self._warn("导出栅格图像失败:{}".format(exc))

    def _selected_output_format(self):
        value = self.outputFormatCombo.currentData()
        return value or OUTPUT_FORMAT_RASTER

    def _output_directory_path(self):
        return self.outputDirWidget.filePath().strip()

    def _output_base_name(self):
        src = self._resolve_input_path() or self._input_path
        if not src:
            return "land_cover_result"
        return os.path.splitext(os.path.basename(src))[0]

    def _vector_output_path(self):
        out_dir = self._output_directory_path()
        if not out_dir:
            return ""
        return os.path.join(out_dir, "{}_final.shp".format(
            self._output_base_name()))

    def _raster_output_path(self):
        out_dir = self._output_directory_path()
        if not out_dir:
            return ""
        return os.path.join(out_dir, "{}_final.tif".format(
            self._output_base_name()))

    def _suggest_output_paths(self, *args, **kwargs):
        src = self._resolve_input_path()
        if not src or not os.path.exists(src):
            return
        out_dir = os.path.dirname(src)
        if out_dir and not self.outputDirWidget.filePath().strip():
            self.outputDirWidget.setFilePath(out_dir)
        self.outputFileWidget.setFilePath(self._vector_output_path())
        self.rasterFileWidget.setFilePath(self._raster_output_path())

    def _on_run(self):
        if self.modelCombo.count() == 0:
            self._warn("尚未选择模型。")
            return
        model_path = self.modelCombo.currentData()
        if not model_path or not os.path.isdir(model_path):
            self._warn("所选模型路径无效。")
            return

        input_path = self._resolve_input_path()
        if not input_path:
            self._warn("请选择输入图层或输入文件。")
            return
        if not os.path.exists(input_path):
            self._warn("输入文件不存在:{}".format(input_path))
            return

        out_dir = self._output_directory_path()
        if not out_dir:
            self._warn("请选择导出目录。")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            self._warn("无法创建导出目录:{}".format(exc))
            return

        vector_path = self._vector_output_path()
        self.outputFileWidget.setFilePath(vector_path)
        self.rasterFileWidget.setFilePath(self._raster_output_path())
        if os.path.exists(vector_path):
            answer = QtWidgets.QMessageBox.question(
                self, "覆盖确认",
                "最终 Shapefile 已存在，确认后会覆盖同名结果。是否继续？")
            if answer != QtWidgets.QMessageBox.Yes:
                return
            try:
                _remove_existing_shapefile(vector_path)
            except OSError as exc:
                self._warn("无法覆盖已有 Shapefile:{}".format(exc))
                return

        self._class_labels = self._read_class_labels(model_path)
        self._input_layer = self._resolve_input_layer()
        self._input_path = input_path
        self._final_layer = None
        self._final_layer_id = None

        flags = {
            "clahe": self.claheCheck.isChecked(),
            "sharpen": self.sharpenCheck.isChecked(),
            "median": self.medianCheck.isChecked(),
            "gaussian": self.gaussianCheck.isChecked(),
        }
        georef = is_georeferenced(input_path)
        self._start_inference_process(model_path, input_path, flags, georef)

    def _ensure_final_layer(self):
        path = self._vector_output_path()
        if not path:
            self._warn("请选择导出目录。")
            return None
        self.outputFileWidget.setFilePath(path)

        if self._layer_is_usable(self._final_layer):
            return self._final_layer
        self._final_layer = None
        self._final_layer_id = None
        if os.path.exists(path):
            layer = QgsVectorLayer(path, "最终分割结果", "ogr")
            if layer.isValid():
                self._final_layer = layer
                self._final_layer_id = layer.id()
                if QgsProject.instance().mapLayer(layer.id()) is None:
                    QgsProject.instance().addMapLayer(layer)
                self._ensure_final_fields(layer)
                self._apply_vector_style(layer, draft=False)
                return layer
        self._create_empty_final_shapefile(path)
        layer = QgsVectorLayer(path, "最终分割结果", "ogr")
        if not layer.isValid():
            self._warn("无法创建最终 Shapefile:{}".format(path))
            return None
        QgsProject.instance().addMapLayer(layer)
        self._final_layer = layer
        self._final_layer_id = layer.id()
        self._apply_vector_style(layer, draft=False)
        return layer

    def _on_export_raster(self):
        if not self._layer_is_usable(self._final_layer):
            self._warn("请先确认对象，生成最终结果图层。")
            return
        if self._final_layer.isEditable() and not self._final_layer.commitChanges():
            self._warn("提交最终结果图层编辑失败:{}".format(
                self._final_layer.commitErrors()))
            self._final_layer.rollBack()
            return

        if self._selected_output_format() == OUTPUT_FORMAT_VECTOR:
            path = self._vector_output_path()
            self.iface.messageBar().pushSuccess(
                "地物分类", "最终 Shapefile 已导出:{}".format(path))
            return

        path = self._raster_output_path()
        if not path:
            self._warn("请选择导出目录。")
            return
        self.rasterFileWidget.setFilePath(path)
        out_dir = os.path.dirname(path) or "."
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        try:
            self._rasterize_final_layer(path)
            self.iface.addRasterLayer(path, os.path.splitext(
                os.path.basename(path))[0])
            self.iface.messageBar().pushSuccess(
                "地物分类", "最终栅格图像已导出:{}".format(path))
        except Exception as exc:  # noqa: BLE001
            self._warn("导出栅格图像失败:{}".format(exc))

    def _rasterize_final_layer(self, path):
        from osgeo import gdal

        template = gdal.Open(self._input_path)
        if template is None:
            raise IOError("无法打开原始影像作为栅格模板:{}".format(
                self._input_path))
        driver = gdal.GetDriverByName("GTiff")
        dst = driver.Create(
            path,
            template.RasterXSize,
            template.RasterYSize,
            1,
            gdal.GDT_Byte,
            ["COMPRESS=LZW", "TILED=YES"],
        )
        if dst is None:
            template = None
            raise IOError("无法创建栅格图像:{}".format(path))
        gt = template.GetGeoTransform()
        proj = template.GetProjection()
        if gt is not None:
            dst.SetGeoTransform(gt)
        if proj:
            dst.SetProjection(proj)
        band = dst.GetRasterBand(1)
        band.Fill(self._background_class_id())
        source = self._final_layer.source().split("|", 1)[0]
        vector_ds = gdal.OpenEx(source, gdal.OF_VECTOR)
        if vector_ds is None:
            dst = None
            template = None
            raise IOError("无法打开最终矢量图层:{}".format(source))
        vector_layer = vector_ds.GetLayer(0)
        result = gdal.RasterizeLayer(
            dst, [1], vector_layer,
            options=["ATTRIBUTE={}".format(FIELD_CLASS_ID)])
        vector_ds = None
        dst.FlushCache()
        dst = None
        template = None
        if result != 0:
            raise IOError("最终矢量图层栅格化失败。")

    def _apply_vector_style(self, layer, draft):
        categories = []
        values = self._known_class_ids(layer)
        for class_id in values:
            color = self._class_color(class_id)
            alpha = "90" if draft else "170"
            symbol = QgsFillSymbol.createSimple({
                "color": "{},{},{},{}".format(
                    color.red(), color.green(), color.blue(), alpha),
                "outline_color": "30,30,30,220",
                "outline_width": "0.3",
            })
            label = self._class_name(class_id)
            categories.append(QgsRendererCategory(class_id, symbol, label))
        renderer = QgsCategorizedSymbolRenderer(FIELD_CLASS_ID, categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def _known_class_ids(self, layer):
        values = set()
        for feature in layer.getFeatures():
            class_id = self._safe_int(feature[FIELD_CLASS_ID], 0)
            if not self._is_background_class_id(class_id):
                values.add(class_id)
        if not values and self._class_labels:
            values = {
                class_id for class_id in range(len(self._class_labels))
                if not self._is_background_class_id(class_id)
            }
        return sorted(values)

    def _class_name(self, class_id):
        if 0 <= class_id < len(self._class_labels):
            return self._class_labels[class_id]
        return str(class_id)

    def _is_background_class_id(self, class_id):
        return (
            self._class_name(class_id).strip().lower()
            in BACKGROUND_CLASS_NAMES
        )

    def _background_class_id(self):
        for class_id, label in enumerate(self._class_labels):
            if str(label).strip().lower() in BACKGROUND_CLASS_NAMES:
                return class_id
        return 0

    def _class_color(self, class_id):
        colors = [
            QColor(230, 25, 75),
            QColor(60, 180, 75),
            QColor(0, 130, 200),
            QColor(245, 130, 48),
            QColor(145, 30, 180),
            QColor(70, 240, 240),
            QColor(240, 50, 230),
            QColor(210, 245, 60),
            QColor(250, 190, 190),
            QColor(0, 128, 128),
        ]
        return colors[class_id % len(colors)]

    def _safe_int(self, value, default):
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _on_process_error(self, error):
        if error == QProcess.Crashed:
            return
        msg = "无法启动推理子进程:{}".format(error)
        self.statusLabel.setText(msg)
        self.iface.messageBar().pushCritical("地物分类", msg)
        self._cleanup_process()

    def _cleanup_process(self):
        self.runBtn.setEnabled(True)
        self.cancelBtn.setEnabled(False)
        if self._params_file and os.path.exists(self._params_file):
            try:
                os.remove(self._params_file)
            except OSError:
                pass
        self._params_file = None
        if self._launcher_file and os.path.exists(self._launcher_file):
            try:
                os.remove(self._launcher_file)
            except OSError:
                pass
        self._launcher_file = None
        if self._process is not None:
            self._process.deleteLater()
        self._process = None

    def _warn(self, message):
        self.statusLabel.setText(message)
        self.iface.messageBar().pushMessage(
            "地物分类", message,
            level=Qgis.Warning, duration=5)

    def closeEvent(self, event):
        if self._process is not None:
            self._process.kill()
        self._stop_ai_editing(silent=True)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # AI 辅助编辑相关
    # ------------------------------------------------------------------

    def _on_ai_start(self):
        if not self._ensure_draft_layer():
            self._warn("请先运行推理生成草稿图层,再启动 AI 编辑。")
            return
        image_path = self._input_path or self._resolve_input_path()
        if not image_path or not os.path.isfile(image_path):
            self._warn("找不到推理使用的输入影像,无法启动 AI 编辑。")
            return

        ok, message = sam_deps_check.ensure_ready(
            backend=SAM_DEFAULT_BACKEND)
        if not ok:
            QtWidgets.QMessageBox.warning(
                self, "SAM 环境未就绪", message)
            return

        if not self._start_ai_worker():
            return

        self._ai_image_path = image_path
        self._ai_image_loaded = False
        try:
            self._load_ai_image_metadata(image_path)
        except Exception as exc:  # noqa: BLE001
            self._warn("无法读取影像元数据:{}".format(exc))
            self._stop_ai_editing(silent=True)
            return

        if not self._send_ai_command({"op": "set_image",
                                      "image_path": image_path}):
            self._stop_ai_editing(silent=True)
            return
        self._ai_image_loaded = True

        canvas = self.iface.mapCanvas()
        self._ai_previous_tool = canvas.mapTool()
        self._ai_tool = AiSegmentMapTool(canvas, self._on_ai_points_changed)
        canvas.setMapTool(self._ai_tool)
        self._mark_draft_layer_ai_preview()
        self._remove_legacy_ai_preview_layers()

        self.aiStartBtn.setEnabled(False)
        self.aiStopBtn.setEnabled(True)
        self.aiUndoPointBtn.setEnabled(True)
        self.aiClearPointsBtn.setEnabled(True)
        self.aiAppendDraftBtn.setEnabled(False)
        self.aiStatusLabel.setText(
            "AI 编辑已启动。左键添加正点,右键添加负点。")
        self.iface.messageBar().pushInfo(
            "地物分类", "AI 编辑已启动,请在画布上标注正负样本点。")

    def _on_ai_stop(self):
        self._stop_ai_editing(silent=False)

    def _stop_ai_editing(self, silent=False):
        canvas = self.iface.mapCanvas() if self.iface else None
        self._clear_ai_preview()
        self._restore_draft_layer_name()
        if self._ai_tool is not None:
            try:
                self._ai_tool.clear_points()
                self._ai_tool.clear_preview()
            except Exception:
                pass
            if canvas is not None and self._ai_previous_tool is not None:
                try:
                    canvas.setMapTool(self._ai_previous_tool)
                except Exception:
                    pass
            try:
                self._ai_tool.dispose()
            except Exception:
                pass
        self._ai_tool = None
        self._ai_previous_tool = None
        self._ai_preview_geometry = None
        self._ai_pending_id = None

        if self._ai_worker is not None:
            try:
                self._send_ai_command({"op": "quit"}, expect_response=False)
            except Exception:
                pass
            try:
                if self._ai_worker.state() != QProcess.NotRunning:
                    self._ai_worker.waitForFinished(2000)
            except Exception:
                pass
            try:
                self._ai_worker.kill()
            except Exception:
                pass
            try:
                self._ai_worker.deleteLater()
            except Exception:
                pass
        self._ai_worker = None
        self._ai_worker_ready = False
        self._ai_responses = {}
        self._ai_image_loaded = False
        self._ai_preview_geometry = None
        self._ai_buffer = ""
        self._ai_predicting = False
        self._ai_queued_points = None

        self.aiStartBtn.setEnabled(True)
        self.aiStopBtn.setEnabled(False)
        self.aiUndoPointBtn.setEnabled(False)
        self.aiClearPointsBtn.setEnabled(False)
        self.aiAppendDraftBtn.setEnabled(False)
        if not silent:
            self.aiStatusLabel.setText("AI 编辑已停止。")

    def _on_ai_undo_point(self):
        if self._ai_tool is None:
            return
        self._ai_tool.undo_last_point()

    def _on_ai_clear_points(self):
        if self._ai_tool is None:
            return
        self._ai_tool.clear_points()
        self._clear_ai_preview()
        self.aiAppendDraftBtn.setEnabled(False)
        self.aiStatusLabel.setText("已清空提示点。")

    def _on_ai_append_draft(self):
        if not self._ensure_draft_layer() or self._ai_preview_geometry is None:
            return
        class_id = self._ai_landslide_class_id()
        self._write_ai_geometry_to_draft(self._ai_preview_geometry, class_id)

    def _ai_landslide_class_id(self):
        for class_id, label in enumerate(self._class_labels or []):
            if str(label).strip().lower() == LANDSLIDE_CLASS_NAME:
                return class_id
        for class_id, _label in enumerate(self._class_labels or []):
            if not self._is_background_class_id(class_id):
                return class_id
        return 1

    def _write_ai_geometry_to_draft(self, geometry, class_id):
        layer = self._draft_layer
        geometry = self._coerce_ai_geometry_for_layer(geometry, layer)
        if geometry is None or geometry.isEmpty():
            self._warn("当前没有可写入的 AI 预览几何。")
            return
        if not layer.isEditable():
            if not layer.startEditing():
                self._warn("草稿层无法进入编辑状态。")
                return

        class_name = self._class_name(class_id)
        new_feature = QgsFeature(layer.fields())
        new_feature.setGeometry(geometry)
        new_feature.setAttribute(FIELD_CLASS_ID, class_id)
        new_feature.setAttribute(FIELD_CLASS_NAME, class_name)
        new_feature.setAttribute(FIELD_REVIEW_STATUS, STATUS_PENDING)
        new_source_id = self._next_draft_source_id(layer)
        new_feature.setAttribute(FIELD_SOURCE_ID, new_source_id)
        layer.addFeature(new_feature)

        if not layer.commitChanges():
            self._warn("写入草稿层失败:{}".format(layer.commitErrors()))
            layer.rollBack()
            return
        layer.triggerRepaint()
        self._apply_vector_style(layer, draft=True)

        if self._ai_tool is not None:
            self._ai_tool.clear_points()
        self._clear_ai_preview()
        self._restore_draft_layer_name()
        self.aiAppendDraftBtn.setEnabled(False)
        self.aiStatusLabel.setText(
            "追加 1 个 landslide 草稿对象,类别 {}({})。".format(
                class_id, class_name))
        self.iface.messageBar().pushSuccess(
            "地物分类", "AI 编辑结果已写入草稿层。")

    def _next_draft_source_id(self, layer):
        max_source = 0
        for feature in layer.getFeatures():
            source_id = self._safe_int(feature[FIELD_SOURCE_ID], 0)
            if source_id > max_source:
                max_source = source_id
        return max_source + 1

    # ------------------------------------------------------------------
    # AI Worker 子进程
    # ------------------------------------------------------------------

    def _start_ai_worker(self):
        worker_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sam_worker.py")
        python_exe = sam_deps_check.default_python_executable()
        if not os.path.isfile(python_exe):
            self._warn("找不到 SAM 子进程解释器:{}".format(python_exe))
            return False

        self._ai_worker = QProcess(self)
        self._ai_responses = {}
        self._ai_pending_id = None
        self._ai_image_loaded = False
        self._ai_worker.setProcessChannelMode(QProcess.SeparateChannels)
        env = QProcessEnvironment()
        for key, value in sam_deps_check.runtime_environment(
                python_exe).items():
            env.insert(key, value)
        self._ai_worker.setProcessEnvironment(env)
        self._ai_worker.readyReadStandardOutput.connect(
            self._on_ai_worker_output)
        self._ai_worker.readyReadStandardError.connect(
            self._on_ai_worker_stderr)
        self._ai_worker.finished.connect(self._on_ai_worker_finished)
        self._ai_worker.errorOccurred.connect(self._on_ai_worker_error)
        self._ai_worker.start(python_exe, [worker_script])
        if not self._ai_worker.waitForStarted(5000):
            self._warn("SAM 子进程启动失败。")
            self._ai_worker = None
            return False

        self.aiStatusLabel.setText("正在加载 SAM 模型...")
        QgsApplication.processEvents()
        if not self._send_ai_command({
                "op": "init",
                "backend": SAM_DEFAULT_BACKEND,
                "model_path": sam_deps_check.default_model_path(
                    SAM_DEFAULT_BACKEND),
                "config_path": sam_deps_check.default_config_path(
                    SAM_DEFAULT_BACKEND),
                "model_type": sam_deps_check.default_model_type(
                    SAM_DEFAULT_BACKEND),
        }):
            return False
        self._ai_worker_ready = True
        return True

    def _send_ai_command(self, payload, expect_response=True, timeout_ms=60000):
        if self._ai_worker is None:
            return False
        if self._ai_worker.state() != QProcess.Running:
            return False
        self._ai_request_seq += 1
        req_id = self._ai_request_seq
        self._ai_responses.pop(req_id, None)
        payload = dict(payload)
        payload["id"] = req_id
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        bytes_written = self._ai_worker.write(data)
        if bytes_written < 0:
            return False
        self._ai_worker.waitForBytesWritten(2000)
        if not expect_response:
            return True

        self._ai_pending_id = req_id
        elapsed = 0
        while req_id not in self._ai_responses and elapsed < timeout_ms:
            if self._ai_worker.state() != QProcess.Running:
                self._ai_pending_id = None
                self._warn("SAM 子进程已退出。")
                return False
            if self._ai_worker.waitForReadyRead(500):
                self._on_ai_worker_output()
            elapsed += 500
            QgsApplication.processEvents()
        if req_id not in self._ai_responses:
            self._ai_pending_id = None
            self._warn("SAM 子进程响应超时。")
            return False
        response = self._ai_responses.pop(req_id)
        if self._ai_pending_id == req_id:
            self._ai_pending_id = None
        if not response.get("ok", True):
            error = response.get("error", "SAM 子进程返回错误。")
            self._warn("SAM 子进程错误:{}".format(error))
            return False
        return True

    def _on_ai_worker_output(self):
        if self._ai_worker is None:
            return
        data = bytes(self._ai_worker.readAllStandardOutput()).decode(
            "utf-8", errors="replace")
        self._ai_buffer += data
        while "\n" in self._ai_buffer:
            line, self._ai_buffer = self._ai_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self._handle_ai_message(message)

    def _on_ai_worker_stderr(self):
        if self._ai_worker is None:
            return
        data = bytes(self._ai_worker.readAllStandardError()).decode(
            "utf-8", errors="replace").strip()
        if data:
            QgsApplication.messageLog().logMessage(
                data, "LandCoverClassification", Qgis.Warning)

    def _handle_ai_message(self, message):
        if message.get("event") == "ready":
            return
        req_id = message.get("id")
        if req_id is not None:
            self._ai_responses[req_id] = message
            if req_id == self._ai_pending_id:
                self._ai_pending_id = None
        if not message.get("ok", True):
            if req_id is None:
                error = message.get("error", "SAM 子进程返回错误。")
                self._warn("SAM 子进程错误:{}".format(error))
            return

        if "polygons" in message:
            geometry = self._build_geometry_from_message(message)
            self._ai_preview_geometry = (
                QgsGeometry(geometry) if geometry is not None
                and not geometry.isEmpty() else None)
            enabled = self._ai_preview_geometry is not None
            if enabled:
                self._show_ai_preview_on_draft(self._ai_preview_geometry)
            else:
                self._clear_ai_preview()
            self.aiAppendDraftBtn.setEnabled(enabled)
            score = message.get("score")
            if enabled and score is not None:
                self.aiStatusLabel.setText(
                    "已生成 mask 预览,score={:.3f}".format(float(score)))
            elif enabled:
                self.aiStatusLabel.setText("已生成 mask 预览。")
            else:
                self.aiStatusLabel.setText("当前点提示未生成有效 mask。")

    def _on_ai_worker_finished(self, exit_code, exit_status):
        self._ai_worker_ready = False
        self._ai_image_loaded = False
        if exit_status != QProcess.NormalExit or exit_code != 0:
            QgsApplication.messageLog().logMessage(
                "SAM worker exited with code {}".format(exit_code),
                "LandCoverClassification", Qgis.Warning)

    def _on_ai_worker_error(self, error):
        if self._ai_worker is None:
            return
        QgsApplication.messageLog().logMessage(
            "SAM worker error: {}".format(error),
            "LandCoverClassification", Qgis.Warning)

    # ------------------------------------------------------------------
    # 影像 / 几何坐标转换
    # ------------------------------------------------------------------

    def _load_ai_image_metadata(self, image_path):
        from osgeo import gdal, osr

        ds = gdal.Open(image_path)
        if ds is None:
            raise IOError("无法打开影像:{}".format(image_path))
        try:
            width = ds.RasterXSize
            height = ds.RasterYSize
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
        finally:
            ds = None

        self._ai_image_size = (width, height)
        self._ai_image_geotransform = gt if gt and gt != (0, 1, 0, 0, 0, 1) \
            else None
        if proj:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(proj)
            authid = None
            try:
                code = srs.GetAuthorityCode(None)
                if code:
                    authid = "{}:{}".format(
                        srs.GetAuthorityName(None) or "EPSG", code)
            except Exception:
                authid = None
            self._ai_image_crs = QgsCoordinateReferenceSystem(authid) \
                if authid else QgsCoordinateReferenceSystem.fromWkt(proj)
        else:
            self._ai_image_crs = QgsCoordinateReferenceSystem()

    def _map_point_to_image(self, map_point):
        """把地图坐标转换为影像像素坐标。"""
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        image_crs = self._ai_image_crs

        if (image_crs is not None and image_crs.isValid()
                and canvas_crs.isValid() and image_crs != canvas_crs):
            transform = QgsCoordinateTransform(
                canvas_crs, image_crs, QgsProject.instance())
            image_point = transform.transform(QgsPointXY(map_point))
        else:
            image_point = QgsPointXY(map_point)

        gt = self._ai_image_geotransform
        if gt is None:
            # 没有地理坐标的影像,直接把地图坐标视作像素坐标
            return (float(image_point.x()), float(image_point.y()))

        det = gt[1] * gt[5] - gt[2] * gt[4]
        if abs(det) < 1e-12:
            return None
        x = image_point.x() - gt[0]
        y = image_point.y() - gt[3]
        px = (gt[5] * x - gt[2] * y) / det
        py = (-gt[4] * x + gt[1] * y) / det
        return (float(px), float(py))

    def _ai_canvas_scale_factor(self):
        """按当前 QGIS 画布分辨率估算 SAM crop 的源影像读取尺度。"""
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        corners = [
            QgsPointXY(extent.xMinimum(), extent.yMinimum()),
            QgsPointXY(extent.xMinimum(), extent.yMaximum()),
            QgsPointXY(extent.xMaximum(), extent.yMinimum()),
            QgsPointXY(extent.xMaximum(), extent.yMaximum()),
        ]
        mapped = [
            self._map_point_to_image(point)
            for point in corners
        ]
        mapped = [point for point in mapped if point is not None]
        if len(mapped) < 2:
            return 1.0
        xs = [point[0] for point in mapped]
        ys = [point[1] for point in mapped]
        canvas_width = max(1, int(canvas.width()))
        canvas_height = max(1, int(canvas.height()))
        scale_x = (max(xs) - min(xs)) / float(canvas_width)
        scale_y = (max(ys) - min(ys)) / float(canvas_height)
        scale = max(scale_x, scale_y, 0.25)
        return max(0.25, min(8.0, float(scale)))

    def _image_point_to_layer(self, px, py):
        gt = self._ai_image_geotransform
        if gt is None:
            return QgsPointXY(px, py)
        x = gt[0] + gt[1] * px + gt[2] * py
        y = gt[3] + gt[4] * px + gt[5] * py
        point = QgsPointXY(x, y)
        layer_crs = (self._draft_layer.crs()
                     if self._draft_layer is not None else None)
        image_crs = self._ai_image_crs
        if (image_crs is not None and image_crs.isValid()
                and layer_crs is not None and layer_crs.isValid()
                and image_crs != layer_crs):
            transform = QgsCoordinateTransform(
                image_crs, layer_crs, QgsProject.instance())
            point = transform.transform(point)
        return point

    def _closed_ring(self, points):
        if points and points[0] != points[-1]:
            points = list(points) + [QgsPointXY(points[0])]
        return points

    def _build_geometry_from_message(self, message):
        polygons = message.get("polygons") or []
        if not polygons:
            return QgsGeometry()
        polygons = sorted(
            polygons,
            key=lambda polygon: self._pixel_ring_area(
                polygon.get("shell") or []),
            reverse=True)[:1]
        multi_polygon = []
        for polygon in polygons:
            shell = polygon.get("shell") or []
            holes = polygon.get("holes") or []
            shell_pts = [self._image_point_to_layer(p[0], p[1])
                         for p in shell]
            if len(shell_pts) < 3:
                continue
            rings = [self._closed_ring(shell_pts)]
            for hole in holes:
                hole_pts = [self._image_point_to_layer(p[0], p[1])
                            for p in hole]
                if len(hole_pts) >= 3:
                    rings.append(self._closed_ring(hole_pts))
            multi_polygon.append(rings)
        if not multi_polygon:
            return QgsGeometry()
        if len(multi_polygon) == 1:
            geometry = QgsGeometry.fromPolygonXY(multi_polygon[0])
        else:
            geometry = QgsGeometry.fromMultiPolygonXY(multi_polygon)
        return self._polygon_only_geometry(geometry, make_valid=False)

    def _pixel_ring_area(self, ring):
        if not ring:
            return 0.0
        area = 0.0
        for idx, point in enumerate(ring):
            previous = ring[idx - 1]
            area += float(previous[0]) * float(point[1])
            area -= float(point[0]) * float(previous[1])
        return abs(area) * 0.5

    def _clear_ai_preview(self):
        if self._ai_tool is not None:
            self._ai_tool.clear_preview()
        self._ai_preview_geometry = None

    def _show_ai_preview_on_draft(self, geometry):
        layer = self._draft_layer if self._layer_is_usable(
            self._draft_layer) else None
        if self._ai_tool is None:
            return
        preview_geometry = self._preview_geometry_for_canvas(geometry)
        if preview_geometry is None or preview_geometry.isEmpty():
            self._ai_tool.clear_preview()
            return
        self._ai_tool.show_preview(preview_geometry, layer)

    def _mark_draft_layer_ai_preview(self):
        if not self._layer_is_usable(self._draft_layer):
            return
        if self._draft_layer_original_name is None:
            self._draft_layer_original_name = self._draft_layer.name()
        self._draft_layer.setName("地物分类草稿 / AI mask 预览")

    def _restore_draft_layer_name(self):
        if (self._draft_layer_original_name is None
                or not self._layer_is_usable(self._draft_layer)):
            self._draft_layer_original_name = None
            return
        self._draft_layer.setName(self._draft_layer_original_name)
        self._draft_layer_original_name = None

    def _remove_legacy_ai_preview_layers(self):
        project = QgsProject.instance()
        layer_ids = [
            layer.id()
            for layer in project.mapLayersByName("AI mask 预览")
        ]
        if layer_ids:
            project.removeMapLayers(layer_ids)

    def _preview_geometry_for_canvas(self, geometry):
        if geometry is None or geometry.isEmpty():
            return QgsGeometry()
        parts = self._polygon_parts_xy(geometry)
        if not parts:
            return QgsGeometry()
        largest = max(parts, key=self._polygon_part_area)
        preview = QgsGeometry.fromPolygonXY(largest)
        if preview is None or preview.isEmpty():
            return QgsGeometry()
        vertex_count = sum(len(ring) for ring in largest)
        if vertex_count <= AI_PREVIEW_MAX_VERTICES:
            return preview

        tolerance = self._ai_preview_simplify_tolerance()
        for multiplier in (1, 2, 4, 8):
            simplified = preview.simplify(tolerance * multiplier)
            parts = self._polygon_parts_xy(simplified)
            if not parts:
                continue
            largest = max(parts, key=self._polygon_part_area)
            preview = QgsGeometry.fromPolygonXY(largest)
            vertex_count = sum(len(ring) for ring in largest)
            if vertex_count <= AI_PREVIEW_MAX_VERTICES:
                return preview

        # 极复杂或很不明确的 mask 只用于画布提示时退化为外接范围,
        # 完整几何仍保存在 _ai_preview_geometry,供追加按钮使用。
        rect = preview.boundingBox()
        if rect is not None and not rect.isEmpty():
            return QgsGeometry.fromRect(rect)
        return preview

    def _polygon_part_area(self, part):
        if not part or not part[0]:
            return 0.0
        ring = part[0]
        area = 0.0
        for idx, point in enumerate(ring):
            previous = ring[idx - 1]
            area += previous.x() * point.y() - point.x() * previous.y()
        return abs(area) * 0.5

    def _polygon_only_geometry(self, geometry, make_valid=True):
        if geometry is None or geometry.isEmpty():
            return QgsGeometry()
        candidate = QgsGeometry(geometry)
        if make_valid:
            try:
                if not candidate.isGeosValid():
                    candidate = candidate.makeValid()
            except Exception:
                return QgsGeometry()
        polygon_parts = self._polygon_parts_xy(candidate)
        if not polygon_parts:
            return QgsGeometry()
        if len(polygon_parts) == 1:
            return QgsGeometry.fromPolygonXY(polygon_parts[0])
        return QgsGeometry.fromMultiPolygonXY(polygon_parts)

    def _polygon_parts_xy(self, geometry):
        if geometry is None or geometry.isEmpty():
            return []
        if QgsWkbTypes.geometryType(geometry.wkbType()) == \
                QgsWkbTypes.PolygonGeometry:
            if QgsWkbTypes.isMultiType(geometry.wkbType()):
                return geometry.asMultiPolygon()
            polygon = geometry.asPolygon()
            return [polygon] if polygon else []

        parts = []
        try:
            children = geometry.asGeometryCollection()
        except Exception:
            children = []
        for child in children:
            parts.extend(self._polygon_parts_xy(child))
        return parts

    def _coerce_ai_geometry_for_layer(self, geometry, layer, make_valid=True):
        geometry = self._polygon_only_geometry(geometry, make_valid=make_valid)
        if geometry is None or geometry.isEmpty():
            return QgsGeometry()
        if layer is not None and QgsWkbTypes.isMultiType(layer.wkbType()) \
                and QgsWkbTypes.isSingleType(geometry.wkbType()):
            geometry.convertToMultiType()
        elif layer is not None and QgsWkbTypes.isSingleType(layer.wkbType()) \
                and QgsWkbTypes.isMultiType(geometry.wkbType()):
            parts = geometry.asMultiPolygon()
            if len(parts) == 1:
                geometry = QgsGeometry.fromPolygonXY(parts[0])
            elif parts:
                # 旧草稿层可能是单 Polygon,取最大面保证预览仍能落到同一层。
                parts = sorted(
                    parts,
                    key=lambda part: abs(
                        QgsGeometry.fromPolygonXY(part).area()),
                    reverse=True)
                geometry = QgsGeometry.fromPolygonXY(parts[0])
        return geometry

    def _on_ai_points_changed(self, positive_points, negative_points):
        if not positive_points and not negative_points:
            if self._ai_predicting:
                self._ai_queued_points = ([], [])
                self.aiStatusLabel.setText("正在推理,已记录清空提示点...")
                self._clear_ai_preview()
                self.aiAppendDraftBtn.setEnabled(False)
                return
            self._clear_ai_worker_context()
            self._clear_ai_preview()
            self.aiAppendDraftBtn.setEnabled(False)
            return
        if not self._ai_worker_ready:
            self._warn("SAM 子进程尚未就绪。")
            return
        if not self._ai_image_loaded:
            self._warn("SAM 子进程尚未设置推理影像,请重新启动 AI 编辑。")
            return

        if self._ai_predicting:
            self._ai_queued_points = (
                list(positive_points), list(negative_points))
            self.aiStatusLabel.setText("正在推理,已记录最新提示点...")
            return

        self._predict_ai_points(positive_points, negative_points)

    def _predict_ai_points(self, positive_points, negative_points):
        if not positive_points and not negative_points:
            self._clear_ai_worker_context()
            return
        self._ai_predicting = True
        try:
            self._send_ai_predict_command(positive_points, negative_points)
        finally:
            self._ai_predicting = False
        queued = self._ai_queued_points
        self._ai_queued_points = None
        if queued is not None and (self._ai_worker_ready
                                   and self._ai_image_loaded):
            self._predict_ai_points(queued[0], queued[1])

    def _send_ai_predict_command(self, positive_points, negative_points):
        if not positive_points and not negative_points:
            return

        positive_image = []
        for point in positive_points:
            mapped = self._map_point_to_image(point)
            if mapped is not None:
                positive_image.append(mapped)
        negative_image = []
        for point in negative_points:
            mapped = self._map_point_to_image(point)
            if mapped is not None:
                negative_image.append(mapped)

        self.aiStatusLabel.setText(
            "正在推理...(正点 {},负点 {})".format(
                len(positive_image), len(negative_image)))

        self._send_ai_command({
            "op": "predict",
            "positive_points": positive_image,
            "negative_points": negative_image,
            "multimask_output": False,
            "scale_factor": self._ai_canvas_scale_factor(),
        })

    def _clear_ai_worker_context(self):
        """清空 SAM worker 的 crop 与 logits 上下文，保留当前影像。"""
        if not self._ai_worker_ready or not self._ai_image_loaded:
            return
        self._send_ai_command({"op": "clear_context"}, timeout_ms=10000)
