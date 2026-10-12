# -*- coding: utf-8 -*-
"""以项目为入口，自动关联输入并发布整个项目的成果版本。"""

import json
import os
import shutil
import threading
from contextlib import contextmanager
import tempfile
import uuid

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import QEvent, QEventLoop, QSettings, QThread, QTimer, pyqtSignal, Qt
from qgis.core import Qgis, QgsMessageLog, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsProject

from .result_attributes import RESULT_FIELDS, build_result_attributes, normalize_cartographer, result_context

from .project_client import ProjectClient, ProjectClientError, snapshot_gpkg


_LIVE_REQUESTS = set()


class _ClosingRequest(RuntimeError):
    """界面已关闭，停止原调用栈继续操作控件。"""


class _RequestFiles:
    """快照由界面调用栈和网络线程共同持有，双方结束后才清理。"""

    def __init__(self, prefix):
        self.path = tempfile.mkdtemp(prefix=prefix)
        self.owners = 1
        self.lock = threading.Lock()

    def retain(self):
        with self.lock:
            self.owners += 1

    def release(self):
        with self.lock:
            self.owners -= 1
            if not self.owners:
                shutil.rmtree(self.path, ignore_errors=True)


def _release_request(worker):
    """仅释放已结束线程，不依赖可能已经销毁的面板。"""
    _LIVE_REQUESTS.discard(worker)
    worker.deleteLater()


class _RequestThread(QThread):
    """工作线程只执行网络与文件任务，不访问 QGIS 对象。"""

    result_ready = pyqtSignal()

    def __init__(self, operation, resources=()):
        super().__init__()
        self.resources = tuple(resources)
        for resource in self.resources:
            resource.retain()
        self.operation = operation
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.operation()
        except Exception as exc:
            self.error = exc
        finally:
            for resource in self.resources:
                resource.release()
        self.result_ready.emit()


class _ProjectCombo(QtWidgets.QComboBox):
    """展开列表时自动读取服务端项目，不需要连接或刷新按钮。"""

    about_to_open = pyqtSignal()

    def showPopup(self):
        self.about_to_open.emit()
        super().showPopup()


class ProjectPanel(QtWidgets.QWidget):
    """项目选择、输入自动登记、草稿恢复和整项目发布。"""

    closing_requested = pyqtSignal()

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.client = None
        self.project = None
        self.image = None
        self.settings = QSettings()
        self.busy = False
        self.switching = False
        self._started = False
        self._leases = []
        self._worker = None
        self._close_running = False
        self._close_outcome = None
        self._host_closing = False
        self._closed = False
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("创建或选择项目，然后在输入区选择影像和 DEM。\n"
                                "完成推理和草稿编辑后，发布整个项目的成果版本。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QtWidgets.QFormLayout()
        self.projects = _ProjectCombo()
        self.projects.addItem("请选择项目", None)
        self.images = QtWidgets.QComboBox()
        self.images.addItem("已关联影像", None)
        form.addRow("当前项目", self.projects)
        form.addRow("项目影像", self.images)
        layout.addLayout(form)
        for text, action in [
                ("创建项目", self.create_project),
                ("保存当前草稿", self.save),
                ("预览当前成果属性", self.preview),
                ("发布项目版本", self.publish)]:
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(lambda checked=False, fn=action: self.invoke(fn))
            layout.addWidget(button)
        self.label = QtWidgets.QLabel("请选择项目，或创建一个新项目。")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        layout.addStretch()
        self.projects.about_to_open.connect(lambda: self.invoke(self.refresh))
        self.projects.currentIndexChanged.connect(lambda: self.invoke(self.select_project))
        self.images.activated.connect(lambda: self.invoke(self.open_image))
        self.host_window = dialog.iface.mainWindow() if hasattr(dialog.iface, "mainWindow") else None
        if self.host_window:
            self.host_window.installEventFilter(self)
        self._freeze_inputs(True)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._started:
            self._started = True
            QTimer.singleShot(0, self._initial_refresh)

    def _initial_refresh(self):
        if self._closed or sip.isdeleted(self):
            return
        try:
            self.refresh()
        except Exception:
            if not sip.isdeleted(self) and not self._closed:
                self.label.setText("暂时无法读取项目。选择项目或创建项目时会自动重试。")

    def eventFilter(self, watched, event):
        """宿主关闭始终放行，由 QGIS 自己决定是否退出。"""
        if watched is getattr(self, "host_window", None) and event.type() == QEvent.Close:
            self.save_on_exit(host=True)
            QTimer.singleShot(0, self._host_close_settled)
        return False

    def _host_close_settled(self):
        """宿主取消退出后解除本轮防重入状态，不干预其保存确认。"""
        if sip.isdeleted(self) or self._closed:
            return
        if QtWidgets.QApplication.activeModalWidget() is not None:
            QTimer.singleShot(100, self._host_close_settled)
        elif self.host_window and not sip.isdeleted(self.host_window) and self.host_window.isVisible():
            self.reset_close_state()

    def reset_close_state(self):
        """重新打开插件或宿主取消退出后允许新的保存尝试。"""
        if not self._close_running:
            self._close_outcome = None
            self._host_closing = False
            self._closed = False

    def exit_notice(self, message, success=False):
        """退出日志与消息栏均尽力写入，不能因界面销毁再次抛出异常。"""
        try:
            QgsMessageLog.logMessage(message, "LandCoverClassification",
                                     Qgis.Info if success else Qgis.Warning)
        except Exception:
            pass
        try:
            self.dialog.iface.messageBar().pushMessage(
                "地物分类", message, level=Qgis.Info if success else Qgis.Warning, duration=5)
        except Exception:
            pass

    def save_on_exit(self, host=False):
        """一次关闭流程只尽力保存一次，失败和忙碌均允许退出。"""
        self._host_closing = self._host_closing or host
        if self._close_running or self._close_outcome is not None:
            return self._close_outcome or "skipped"
        self._close_running = True
        try:
            self._close_outcome = "skipped"
            if self.busy or self.dialog._process is not None or self.dialog._ai_predicting:
                self.exit_notice("退出保存已跳过：当前任务忙碌，未确认保存成功。")
                self.closing_requested.emit()
                return self._close_outcome
            if not self.dialog._draft_session.is_active:
                self._close_outcome = "empty"
                return self._close_outcome
            if not self.active:
                self.exit_notice("退出保存已跳过：草稿未关联项目。")
                return self._close_outcome
            self.save(exiting=True)
            self._close_outcome = "saved"
            self.exit_notice("退出前项目草稿已保存。", success=True)
        except Exception as exc:
            self._close_outcome = "failed"
            self.exit_notice("退出保存未确认成功：{}；将保留磁盘 generation。"
                             "若编辑提交失败，磁盘文件不包含未提交缓冲。".format(exc))
        finally:
            self._close_running = False
        return self._close_outcome

    @property
    def active(self):
        return self.project is not None and self.image is not None

    def _check_idle(self):
        if self._worker is not None and (sip.isdeleted(self._worker) or self._worker.isFinished()):
            self.busy = False
            self._worker = None
        if self.busy or self.dialog._process is not None or self.dialog._ai_predicting:
            raise RuntimeError("请先等待当前操作完成。")

    def invoke(self, action):
        """统一报告 Qt 回调错误，并把选择恢复到仍然有效的会话。"""
        try:
            self._check_idle()
            action()
        except _ClosingRequest:
            return
        except Exception as exc:
            if self._closed or sip.isdeleted(self):
                return
            self._select_row(self.projects, self.project)
            self._select_row(self.images, self.image)
            self.dialog._warn(str(exc))

    def _client(self):
        if self.client is None:
            self.client = ProjectClient.from_config()
        return self.client

    @staticmethod
    def _select_row(combo, row):
        combo.blockSignals(True)
        try:
            index = next((i for i in range(combo.count())
                          if combo.itemData(i) and row and
                          combo.itemData(i)["id"] == row["id"]), 0)
            combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    @contextmanager
    def request_directory(self, prefix):
        """退出中仍运行的请求持有文件，完成后自行释放。"""
        lease = _RequestFiles(prefix)
        self._leases.append(lease)
        try:
            yield lease.path
        finally:
            self._leases.remove(lease)
            lease.release()

    def call(self, operation):
        """普通请求维持原超时；关闭界面时停止等待，线程独立完成。"""
        progress = QtWidgets.QProgressDialog("正在访问内部项目服务…", "", 0, 0, None)
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setWindowFlags(progress.windowFlags() & ~Qt.WindowCloseButtonHint)
        worker = _RequestThread(operation, self._leases)
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        worker.finished.connect(lambda worker=worker: _release_request(worker))
        self.closing_requested.connect(loop.quit)
        _LIVE_REQUESTS.add(worker)
        self._worker = worker
        self.busy = True
        try:
            worker.start()
            progress.show()
            if not worker.isFinished():
                loop.exec_()
        finally:
            progress.close()
            progress.deleteLater()
            if not sip.isdeleted(self):
                self.closing_requested.disconnect(loop.quit)
            self.busy = worker.isRunning()
        if self._closed or self._host_closing or sip.isdeleted(self) or not worker.isFinished():
            raise _ClosingRequest("界面已关闭，原网络请求将在后台结束。")
        self._worker = None
        if worker.error:
            raise worker.error
        return worker.result

    def refresh(self):
        self._check_idle()
        client = self._client()
        rows = self.call(lambda: client.request("GET", "/internal/projects"))
        self.projects.blockSignals(True)
        try:
            self.projects.clear()
            self.projects.addItem("请选择项目", None)
            for row in rows:
                self.projects.addItem(row["name"], row)
                if self.project and row["id"] == self.project["id"]:
                    self.project = row
        finally:
            self.projects.blockSignals(False)
        self._select_row(self.projects, self.project)
        if self.project:
            self.refresh_images()
        else:
            self.label.setText("请选择项目，或创建一个新项目。")

    def selected_project(self):
        if not self.project:
            raise RuntimeError("请先在项目页创建或选择项目。")
        return self.project

    def create_project(self):
        form = QtWidgets.QDialog(self)
        form.setWindowTitle("创建项目")
        layout = QtWidgets.QFormLayout(form)
        name = QtWidgets.QLineEdit(form)
        cartographer = QtWidgets.QLineEdit(form)
        cartographer.setPlaceholderText("选填，最多 32 个字符")
        layout.addRow("项目名称（必填）", name)
        layout.addRow("制图者", cartographer)
        error = QtWidgets.QLabel(form)
        layout.addRow(error)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=form)
        layout.addRow(buttons)
        payload = {}

        def accept():
            try:
                text = name.text().strip()
                if not text or len(text) > 200:
                    raise ValueError("项目名称须为 1–200 个字符。")
                payload.update(name=text, ztz=normalize_cartographer(cartographer.text()))
            except ValueError as exc:
                error.setText(str(exc))
                return
            form.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(form.reject)
        if form.exec_() != QtWidgets.QDialog.Accepted:
            return
        client = self._client()
        row = self.call(lambda: client.request("POST", "/internal/projects", payload))
        self.refresh()
        self._select_row(self.projects, row)
        self.select_project()

    def select_project(self):
        row = self.projects.currentData()
        if self.project and row and row["id"] == self.project["id"]:
            return
        self._check_idle()
        self.save()
        if self.dialog._draft_session.is_active and not self.active:
            raise RuntimeError("当前有未关联的草稿，请先导出并关闭插件，避免覆盖编辑。")
        # 先读取目标项目；服务失败时保留旧项目和草稿。
        rows = self.call(lambda: self._client().request(
            "GET", "/internal/projects/{}/images".format(row["id"]))) if row else []
        self.switching = True
        try:
            self.dialog._stop_ai_editing(silent=True)
            self.dialog._discard_draft_session()
            self.project, self.image = row, None
            self.dialog._input_path = None
            self.dialog._input_layer = None
            self._set_inputs("", None)
        finally:
            self.switching = False
        self._set_images(rows)
        self._freeze_inputs(row is None)
        self.label.setText("在输入区选择影像和可选 DEM，或选择项目中已有影像继续编辑。"
                           if row else "请选择项目，或创建一个新项目。")

    def _set_images(self, rows):
        self.images.blockSignals(True)
        try:
            self.images.clear()
            self.images.addItem("选择项目影像继续编辑", None)
            for row in rows:
                self.images.addItem(row["name"] + ("（有草稿）" if row["draft_id"] else "（未生成成果）"), row)
        finally:
            self.images.blockSignals(False)
        self._select_row(self.images, self.image)

    def refresh_images(self):
        project = self.selected_project()
        rows = self.call(lambda: self._client().request(
            "GET", "/internal/projects/{}/images".format(project["id"])))
        self._set_images(rows)
        return rows

    def _image_path(self, suffix=""):
        return "/internal/projects/{}/images/{}{}".format(
            self.project["id"], self.image["id"], suffix)

    def _freeze_inputs(self, frozen):
        self.dialog.inputGroup.setEnabled(not frozen)
        self.dialog.mDemFile.setEnabled(not frozen)

    def _set_inputs(self, path, dem):
        widgets = (self.dialog.fileRadio, self.dialog.layerRadio,
                   self.dialog.inputFileWidget, self.dialog.mDemFile)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.dialog.fileRadio.setChecked(True)
            self.dialog.inputFileWidget.setFilePath(path)
            self.dialog.mDemFile.setFilePath(dem or "")
            self.dialog.inputFileWidget.setEnabled(True)
            self.dialog.layerCombo.setEnabled(False)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def bind_input(self, path):
        """正常输入选择即完成登记；先保存旧景，再恢复目标影像草稿。"""
        self._check_idle()
        project = self.selected_project()
        dem = self.dialog._resolve_dem_path()
        if dem is False:
            raise RuntimeError("请选择有效的 DEM 文件。")
        same = self.image and os.path.normcase(path) == os.path.normcase(self.image["image_path"])
        if same and (dem or None) == self.image["dem_path"]:
            return
        self.save()
        row = self.call(lambda: self._client().request(
            "POST", "/internal/projects/{}/images".format(project["id"]),
            {"name": os.path.basename(path), "image_path": path, "dem_path": dem or None}))
        if same:
            # 更新 DEM 只影响后续推理，保留当前手工编辑与已发布版本。
            self.image["dem_path"] = row["dem_path"]
        else:
            self._activate_image(row, preserve_input=True)
        self.refresh_images()

    def open_image(self):
        row = self.images.currentData()
        if not row:
            return
        if self.image and row["id"] == self.image["id"]:
            return
        self.save()
        rows = self.refresh_images()
        row = next((item for item in rows if item["id"] == row["id"]), None)
        if row is None:
            raise RuntimeError("项目影像已不存在。")
        self._activate_image(row)
        self._select_row(self.images, self.image)

    def _activate_image(self, row, preserve_input=False):
        """恢复文件校验通过后再切换，旧草稿已经保存到服务端。"""
        from .raster_input_adapter import inspect_with_qgis_gdal
        inspect_with_qgis_gdal(row["image_path"])
        if row["dem_path"] and not os.path.isfile(row["dem_path"]):
            raise RuntimeError("当前 QGIS 无法读取项目 DEM。")
        with self.request_directory("lcc_restore_") as directory:
            restored = os.path.join(directory, "draft.gpkg")
            if row["draft_id"]:
                headers = self.call(lambda: self._client().request(
                    "GET", "/internal/projects/{}/images/{}/draft".format(
                        self.project["id"], row["id"]), download=restored))
                row["draft_id"] = headers["X-Draft-ID"]
                self.dialog._validate_draft_generation(restored)
            self.switching = True
            try:
                self.dialog._stop_ai_editing(silent=True)
                self.dialog._discard_draft_session()
                self.image = row
                self.dialog._input_path = None
                if not preserve_input:
                    self._set_inputs(row["image_path"], row["dem_path"])
                self.dialog._initialize_working_image()
                if self.dialog._input_path != row["image_path"]:
                    raise RuntimeError("加载项目影像失败；已保存草稿仍在服务中。")
                if row["draft_id"]:
                    if not self.dialog._ensure_draft_session(row["image_path"]):
                        raise RuntimeError("无法恢复草稿会话。")
                    destination = self.dialog._draft_session.next_generation_path("restored")
                    snapshot_gpkg(restored, destination)
                    self.dialog._publish_draft_generation(destination)
                    self.dialog.exportRasterBtn.setEnabled(True)
            finally:
                self.switching = False
        self.dialog._suggest_output_paths()
        self._freeze_inputs(False)
        self.label.setText("当前影像：{}。切换影像或关闭时自动保存草稿。".format(row["name"]))

    def export_context(self):
        """确保成果属性来自当前草稿所属的项目与影像。"""
        if not self.active or self.image.get("project_id") != self.project["id"]:
            raise ValueError("当前影像未关联到所选项目，无法生成成果属性。")
        session = self.dialog._draft_session
        if (not session.is_active or os.path.normcase(os.path.realpath(session.input_path))
                != os.path.normcase(os.path.realpath(self.image["image_path"]))):
            raise ValueError("当前草稿与项目影像不一致，无法生成成果属性。")
        result_context(self.image["id"], self.project.get("ztz"))
        return {"image_id": self.image["id"], "ztz": self.project.get("ztz")}

    def save(self, exiting=False):
        """提交并快照当前草稿，退出专用传输不影响普通保存的超时。"""
        if not self.active:
            return
        session = self.dialog._draft_session
        if not session.is_active:
            return
        if self.dialog._layer_is_usable(self.dialog._draft_layer):
            if not self.dialog._commit_layer_if_needed(self.dialog._draft_layer):
                raise RuntimeError("请解决草稿编辑提交失败后重试保存。")
        if os.path.normcase(session.input_path) != os.path.normcase(self.image["image_path"]):
            raise RuntimeError("当前草稿与项目影像不一致，保存已拒绝。")
        with self.request_directory("lcc_save_") as directory:
            snapshot = os.path.join(directory, "draft.gpkg")
            snapshot_gpkg(session.generation_path, snapshot)
            path = self._image_path("/draft")
            if self.image["draft_id"]:
                path += "?base_draft_id=" + self.image["draft_id"]
            client = self._client()
            result = (client.request_on_exit(path, snapshot) if exiting else
                      self.call(lambda: client.request("PUT", path, upload=snapshot)))
        self.image["draft_id"] = result["draft_id"]
        if not exiting:
            self.label.setText("项目草稿已保存，可关闭后重新打开。")

    def preview(self):
        """展示当前草稿的标准成果属性，不写入草稿或发布版本。"""
        preview = self._create_result_preview()
        try:
            preview.exec_()
        finally:
            preview.deleteLater()

    def _create_result_preview(self):
        """复用成果字段契约构建只读预览，保留空值及原始测量精度。"""
        layer = self.dialog._draft_layer
        if not self.dialog._layer_is_usable(layer):
            raise RuntimeError("当前没有草稿。")
        context = self.export_context()
        if not layer.crs().isValid():
            raise ValueError("草稿缺少有效 CRS，无法计算成果面积。")
        distance = QgsDistanceArea()
        distance.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
        distance.setEllipsoid("WGS84")
        converter = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem(4326),
                                           QgsProject.instance())
        preview = QtWidgets.QDialog(self)
        preview.setWindowTitle("当前成果属性预览")
        layout = QtWidgets.QVBoxLayout(preview)
        hint = QtWidgets.QLabel(
            "显示当前草稿的 17 个标准成果字段及分类、几何信息；NULL 表示未赋值。\n"
            "向右滚动可查看全部字段。面积按 WGS84 椭球计算；发布时重新校验计算，"
            "Shapefile 导出合并、拆分后可能与此处要素划分不同。", preview)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        columns = [("feature_id", "要素编号")] + [
            (field.name, field.label + (" m²" if field.name == "mj" else ""))
            for field in RESULT_FIELDS
        ] + [("class_id", "类别编号"), ("class_name", "类别名称"),
             ("source_id", "来源编号"), ("perimeter_m", "周长 m"),
             ("center_lon", "中心经度"), ("center_lat", "中心纬度")]
        table = QtWidgets.QTableWidget(0, len(columns), preview)
        table.setObjectName("resultAttributesTable")
        for col, (name, label) in enumerate(columns):
            header = QtWidgets.QTableWidgetItem("{}\n{}".format(label, name))
            header.setData(Qt.UserRole, name)
            table.setHorizontalHeaderItem(col, header)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            if geometry.isEmpty():
                continue
            center = converter.transform(geometry.centroid().asPoint())
            values = build_result_attributes(**context, mj=distance.measureArea(geometry))
            values.update(feature_id=str(feature["feature_uuid"]),
                          **{name: feature[name] for name in ("class_id", "class_name", "source_id")},
                          perimeter_m=distance.measurePerimeter(geometry),
                          center_lon=center.x(), center_lat=center.y())
            row = table.rowCount()
            table.insertRow(row)
            for col, (name, _) in enumerate(columns):
                value = values[name]
                if value is None:
                    text = "NULL"
                elif name in ("mj", "perimeter_m"):
                    text = "{:.3f}".format(value)
                elif name in ("center_lon", "center_lat"):
                    text = "{:.7f}".format(value)
                else:
                    text = str(value)
                item = QtWidgets.QTableWidgetItem(text)
                item.setData(Qt.UserRole, value)
                item.setToolTip("未赋值" if value is None else str(value))
                table.setItem(row, col, item)
        table.resizeColumnsToContents()
        layout.addWidget(table)
        preview.resize(1100, 500)
        return preview

    def publish(self):
        """确认所有已有成果后一次发布，断线时保留整个项目的幂等请求。"""
        project = self.selected_project()
        self.save()
        key = "LandCoverClassification/pending_project_publish/{}".format(project["id"])
        raw = self.settings.value(key, "")
        if raw:
            payload = json.loads(raw)
            if payload.pop("_service", None) != self._client().base_url:
                raise RuntimeError("待确认发布属于另一个服务，请由管理员恢复原配置后重试。")
        else:
            self.refresh()
            rows = self.refresh_images()
            drafts = [{"image_id": row["id"], "draft_id": row["draft_id"]}
                      for row in rows if row["draft_id"]]
            if not drafts:
                raise RuntimeError("项目还没有可发布的成果，请先完成推理或草稿编辑。")
            omitted = len(rows) - len(drafts)
            message = "将把项目内全部 {} 景成果整理为一个新版本。\n请确认已完成几何检查。".format(len(drafts))
            if omitted:
                message += "\n另有 {} 景尚未生成草稿，不纳入本次版本。".format(omitted)
            if QtWidgets.QMessageBox.question(self, "发布项目版本", message) != QtWidgets.QMessageBox.Yes:
                return
            payload = {"drafts": drafts, "base_version": self.project["current_version"],
                       "idempotency_key": str(uuid.uuid4()), "confirmed": True}
            self.settings.setValue(key, json.dumps({**payload, "_service": self._client().base_url}))
            self.settings.sync()
        try:
            result = self.call(lambda: self._client().request(
                "POST", "/internal/projects/{}/publish".format(project["id"]), payload))
        except ProjectClientError as exc:
            if exc.status in (404, 409, 422):
                self.settings.remove(key)
                self.settings.sync()
            raise
        self.settings.remove(key)
        self.settings.sync()
        self.project["current_version"] = result["id"]
        self.label.setText("项目版本已发布：{}（{} 景成果）。".format(result["id"], len(payload["drafts"])))
        rows = self.refresh_images()
        current = {row["id"]: row["draft_id"] for row in rows if row["draft_id"]}
        published = {row["image_id"]: row["draft_id"] for row in payload["drafts"]}
        if current != published:
            self.label.setText("已确认上次版本发布成功；项目还有后续编辑，再次点击发布可生成新版本。")
        return result
