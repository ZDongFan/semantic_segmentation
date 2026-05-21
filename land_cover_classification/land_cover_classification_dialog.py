# -*- coding: utf-8 -*-
"""LandCoverClassification 对话框。"""

import json
import os
import sys
import tempfile

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QProcess, QProcessEnvironment, QSettings, QUrl
from qgis.PyQt.QtGui import QDesktopServices

from qgis.core import (
    QgsApplication,
    QgsMapLayerProxyModel,
    Qgis,
)
from qgis.gui import QgsFileWidget

from .inference import is_georeferenced
from .model_scan import scan as scan_models


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__),
                 "land_cover_classification_dialog_base.ui"))

SETTINGS_GROUP = "LandCoverClassification"


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


def _qgis_plugin_path():
    try:
        path = QgsApplication.pluginPath()
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


class LandCoverClassificationDialog(QtWidgets.QDialog, FORM_CLASS):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self._process = None
        self._params_file = None
        self._launcher_file = None
        self._output_path = None
        self._process_error_message = ""

        self._init_defaults()
        self._wire_signals()
        self._refresh_models()

    # --------------------------------------------------------------- 初始化
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
        self.outputFileWidget.setStorageMode(QgsFileWidget.SaveFile)
        self.outputFileWidget.setFilter(
            "GeoTIFF (*.tif);;PNG (*.png);;JPEG (*.jpg)")

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
            self._warn(self.tr("输入文件不存在:{}").format(
                input_path))
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
                self._warn(self.tr("无法创建输出目录:{}").
                           format(exc))
                return

        flags = {
            "clahe": self.claheCheck.isChecked(),
            "sharpen": self.sharpenCheck.isChecked(),
            "median": self.medianCheck.isChecked(),
            "gaussian": self.gaussianCheck.isChecked(),
        }
        georef = is_georeferenced(input_path)
        self._start_inference_process(model_path, input_path, output_path,
                                      flags, georef)

    def _start_inference_process(self, model_path, input_path, output_path,
                                 flags, georef):
        self._output_path = output_path
        self._process_error_message = ""
        params = {
            "model_path": model_path,
            "input_path": input_path,
            "output_path": output_path,
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
        self.progressBar.setValue(0)
        self.statusLabel.setText(self.tr("运行中({}模式)...").format(
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
            self.statusLabel.setText(self.tr("正在取消..."))

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
                self._output_path = payload.get(
                    "output_path", self._output_path)

    def _on_process_finished(self, exit_code, exit_status):
        if self._process is None:
            return
        if exit_code == 0 and exit_status == QProcess.NormalExit:
            self._on_process_completed()
        else:
            msg = self._process_error_message
            if not msg:
                msg = self.tr("推理子进程异常退出(exit_code={})。").format(
                    exit_code)
            self.statusLabel.setText(msg)
            self.iface.messageBar().pushCritical("地物分类",
                                                 msg)
        self._cleanup_process()

    def _on_process_completed(self):
        output_path = self._output_path
        self.statusLabel.setText(self.tr("完成:{}").format(output_path))
        self.progressBar.setValue(100)
        layer_name = os.path.splitext(os.path.basename(output_path))[0]
        layer = self.iface.addRasterLayer(output_path, layer_name)
        if layer is None or not layer.isValid():
            self.iface.messageBar().pushWarning(
                "地物分类",
                self.tr("结果已写出,但无法作为图层加载:{}").
                format(output_path))
        else:
            self.iface.messageBar().pushSuccess(
                "地物分类",
                self.tr("分割结果已加载为图层:{}").format(layer_name))

    def _on_process_error(self, error):
        if error == QProcess.Crashed:
            return
        msg = self.tr("无法启动推理子进程:{}").format(error)
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

    # ----------------------------------------------------------------- 工具
    def _warn(self, message):
        self.statusLabel.setText(message)
        self.iface.messageBar().pushMessage(
            "地物分类", message,
            level=Qgis.Warning, duration=5)

    def closeEvent(self, event):
        if self._process is not None:
            self._process.kill()
        super().closeEvent(event)
