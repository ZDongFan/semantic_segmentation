"""本地项目、影像关联、持久草稿及上传占位入口。"""

import json
import os
from pathlib import Path

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import QEvent, QLockFile, QSettings, QTimer, pyqtSignal
from qgis.core import Qgis, QgsMessageLog

from .local_project import LocalProject, input_fingerprint, snapshot_gpkg
from .result_attributes import normalize_cartographer, result_context


def reference_grid(path):
    """只读取元数据，使用 QGIS 自带 GDAL。"""
    from osgeo import gdal
    from qgis.core import QgsCoordinateReferenceSystem
    from .raster_input_adapter import inspect_with_qgis_gdal
    raster = gdal.Open(inspect_with_qgis_gdal(path)['source_path'])
    if raster is None:
        raise ValueError('无法读取影像。')
    crs = QgsCoordinateReferenceSystem(raster.GetProjection())
    if not crs.isValid():
        raise ValueError('影像缺少有效 CRS。')
    return dict(width=raster.RasterXSize, height=raster.RasterYSize, bands=raster.RasterCount,
                crs=crs.authid() or crs.toWkt(), transform=list(raster.GetGeoTransform()))


class ProjectPanel(QtWidgets.QWidget):
    """项目锁覆盖完整编辑会话；所有日常操作只访问本地文件。"""

    closing_requested = pyqtSignal()

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.store = self.project = self.image = None
        self.settings = QSettings()
        self.buttons = {}
        self.busy = self.switching = self._started = False
        self._close_running = self._host_closing = self._closed = self._save_error = False
        self._close_outcome = self._retry_action = self._opening_revision = None
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel('创建或打开本地项目，然后选择影像和可选 DEM。\n草稿、历史及恢复状态保存在项目目录中。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QtWidgets.QFormLayout()
        self.projects, self.images = QtWidgets.QComboBox(), QtWidgets.QComboBox()
        self.projects.addItem('选择最近项目', None)
        self.images.addItem('选择项目影像', None)
        project_row = QtWidgets.QHBoxLayout()
        project_row.addWidget(self.projects, 1)
        open_project = QtWidgets.QPushButton('…')
        open_project.setFixedWidth(32)
        open_project.setToolTip('选择项目文件夹')
        open_project.setAccessibleName('选择项目文件夹')
        open_project.clicked.connect(lambda: self.invoke(self.open_project))
        self.buttons['open_project'] = open_project
        project_row.addWidget(open_project)
        form.addRow('选择项目', project_row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.images, 1)
        manage = QtWidgets.QPushButton('管理影像')
        manage.setObjectName('manageProjectImages')
        manage.clicked.connect(lambda: self.invoke(self.manage_images))
        self.buttons['manage_images'] = manage
        row.addWidget(manage)
        form.addRow('项目影像', row)
        layout.addLayout(form)
        for title, fn in [('新建项目', self.create_project),
                          ('保存当前草稿', self.save), ('预览当前滑坡列表', self.preview),
                          ('查看单个滑坡属性', self.inspect_feature),
                          ('恢复管理', self.recovery), ('上传成果', self.upload_results)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, action=fn: self.invoke(action))
            layout.addWidget(button)
            self.buttons[fn.__name__] = button
        self.buttons['inspect_feature'].setCheckable(True)
        self.buttons['inspect_feature'].setObjectName('inspectSingleLandslide')
        self.action_hint = QtWidgets.QLabel()
        self.action_hint.setWordWrap(True)
        layout.addWidget(self.action_hint)
        self.label = QtWidgets.QLabel('请创建或打开本地项目。')
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.retry_button = QtWidgets.QPushButton('重试刚才的操作')
        self.retry_button.hide()
        self.retry_button.clicked.connect(lambda: self.invoke(self._retry_action) if self._retry_action else None)
        layout.addWidget(self.retry_button)
        layout.addStretch()
        self.projects.activated.connect(lambda: self.invoke(self.select_project))
        self.images.activated.connect(lambda: self.invoke(self.open_image))
        self.host_window = dialog.iface.mainWindow() if hasattr(dialog.iface, 'mainWindow') else None
        if self.host_window:
            self.host_window.installEventFilter(self)
        self._freeze_inputs(True)

    @property
    def active(self):
        return bool(self.store and self.project and self.image and not self.image.get('removed_at'))

    @property
    def is_saved(self):
        """修订身份持久化，不以本进程路径标记判断保存结果。"""
        session = self.dialog._draft_session
        return bool(self.active and session.is_active and session.revision and not self._save_error
                    and self.image.get('saved_revision') == session.revision)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._started:
            self._started = True
            QTimer.singleShot(0, self._initial_refresh)

    def _initial_refresh(self):
        if not sip.isdeleted(self) and not self._closed:
            self.invoke(self.refresh)

    def eventFilter(self, watched, event):
        """退出确认由 QGIS 处理，保存失败仍保留项目文件。"""
        if watched is getattr(self, 'host_window', None) and event.type() == QEvent.Close:
            self.save_on_exit(host=True)
            QTimer.singleShot(0, self._host_close_settled)
        return False

    def _host_close_settled(self):
        if sip.isdeleted(self) or self._closed:
            return
        if QtWidgets.QApplication.activeModalWidget() is not None:
            QTimer.singleShot(100, self._host_close_settled)
        elif self.host_window and not sip.isdeleted(self.host_window) and self.host_window.isVisible():
            self.reset_close_state()

    def reset_close_state(self):
        """重新打开或取消退出后允许再次保存。"""
        if not self._close_running:
            self._close_outcome = None
            self._host_closing = self._closed = False
            if self.project and self.store is None:
                directory = self.project['directory']
                self.project = self.image = None
                self.open_directory(directory)

    def exit_notice(self, message, success=False):
        """退出诊断不得因界面销毁再抛异常。"""
        try:
            QgsMessageLog.logMessage(message, 'LandCoverClassification', Qgis.Info if success else Qgis.Warning)
            self.dialog.iface.messageBar().pushMessage('地物分类', message,
                level=Qgis.Info if success else Qgis.Warning, duration=5)
        except Exception:
            pass

    def save_on_exit(self, host=False):
        """同轮退出仅尽力保存一次，失败和忙碌均保留文件。"""
        self._host_closing = self._host_closing or host
        if self._close_running or self._close_outcome is not None:
            return self._close_outcome or 'skipped'
        self._close_running = True
        try:
            self._close_outcome = 'skipped'
            ui = getattr(self.dialog, '_workflow_ui', None)
            if self.busy or self.dialog._process is not None or self.dialog._ai_predicting or (ui and ui.busy()):
                self.exit_notice('任务忙碌，退出保存已跳过；已登记草稿仍保存在本地项目。')
            elif not self.dialog._draft_session.is_active:
                self._close_outcome = 'empty'
            elif self.active:
                self.save(exiting=True, manual=False, reason='退出前自动保存')
                self._close_outcome = 'saved'
                self.exit_notice('当前草稿已本地保存。', success=True)
        except Exception as exc:
            self._close_outcome = 'failed'
            self.exit_notice('保存失败：{}；文件保留在 {}。未提交编辑缓冲不承诺恢复。'.format(
                exc, self.dialog._draft_session.directory))
        finally:
            self._close_running = False
        return self._close_outcome

    def release_project(self):
        """图层和写入操作结束后释放锁，不删除持久数据。"""
        if self.store:
            self.store.close()
            self.store = None

    def _check_idle(self):
        ui = getattr(self.dialog, '_workflow_ui', None)
        if self.busy or self.dialog._process is not None or self.dialog._ai_predicting or (ui and ui.busy()):
            raise RuntimeError('请先等待当前操作完成。')

    def invoke(self, action):
        """失败恢复仍然有效的项目、影像选择。"""
        try:
            self._check_idle()
            action()
            self._retry_action = None
            self.retry_button.hide()
        except Exception as exc:
            if self._closed or sip.isdeleted(self):
                return
            self._select_row(self.projects, self.project)
            self._select_row(self.images, self.image)
            self.label.setText('项目操作未完成：{}'.format(exc))
            self._retry_action = action if action == self.save else None
            self.retry_button.setVisible(self._retry_action is not None)
        finally:
            ui = getattr(self.dialog, '_workflow_ui', None)
            if ui:
                ui.schedule()

    @staticmethod
    def _select_row(combo, row):
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(next((i for i in range(combo.count()) if combo.itemData(i) and row
                and combo.itemData(i)['id'] == row['id']), 0))
        finally:
            combo.blockSignals(False)

    def update_actions(self, associated, busy):
        for name in ('save', 'preview', 'inspect_feature'):
            self.buttons[name].setEnabled(associated and not busy)
        self.buttons['recovery'].setEnabled(self.active and not busy)
        uploadable = bool(self.store and any(self.store.has_saved_snapshot(row) for row in self.store.images()))
        self.buttons['upload_results'].setEnabled(uploadable and not busy)
        self.buttons['upload_results'].setToolTip('功能尚未接入' if uploadable else '请先保存项目中的有效草稿。')
        for name in ('create_project', 'open_project'):
            self.buttons[name].setEnabled(not busy)
        self.projects.setEnabled(not busy)
        self.images.setEnabled(bool(self.project) and not busy)
        self.buttons['manage_images'].setEnabled(bool(self.project) and not busy)
        self.retry_button.setEnabled(not busy)
        self.action_hint.setText('请等待当前操作完成。' if busy else
            '请创建或打开本地项目。' if not self.project else '草稿及历史保存在项目目录；导出与本地保存分别操作。')

    def _recent(self):
        try:
            return json.loads(self.settings.value('LandCoverClassification/local_projects', '[]'))
        except (ValueError, TypeError):
            return []

    def refresh(self):
        """最近记录只用于导航，打开及恢复不依赖这些记录。"""
        self.projects.blockSignals(True)
        try:
            self.projects.clear()
            self.projects.addItem('选择最近项目', None)
            for row in self._recent():
                self.projects.addItem(row['name'], row)
        finally:
            self.projects.blockSignals(False)
        self._select_row(self.projects, self.project)
        if self.store:
            self.refresh_images()
            self.label.setText('已读取本地项目：{}。'.format(self.project['name']))

    def selected_project(self):
        if not self.store:
            raise RuntimeError('请先创建或打开本地项目。')
        return self.project

    def create_project(self):
        form = QtWidgets.QDialog(self)
        form.setWindowTitle('创建本地项目')
        layout = QtWidgets.QFormLayout(form)
        name, cartographer = QtWidgets.QLineEdit(form), QtWidgets.QLineEdit(form)
        layout.addRow('项目名称', name)
        layout.addRow('制图者（选填，最多 32 字符）', cartographer)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(form.accept)
        buttons.rejected.connect(form.reject)
        layout.addRow(buttons)
        if form.exec_() != QtWidgets.QDialog.Accepted:
            return
        ztz = normalize_cartographer(cartographer.text())
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, '选择空目录创建本地项目')
        if directory:
            self.open_directory(directory, create=(name.text(), ztz))

    def open_project(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, '打开本地项目目录')
        if directory:
            self.open_directory(directory)

    def select_project(self):
        row = self.projects.currentData()
        if row:
            self.open_directory(row['directory'])

    def open_directory(self, directory, create=None):
        """先锁定并验证目标，失败不解除当前草稿。"""
        self._check_idle()
        directory = str(Path(directory).resolve())
        if self.store and os.path.normcase(str(self.store.root)) == os.path.normcase(directory):
            return
        lock = QLockFile(str(Path(directory) / '.project.lock'))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise RuntimeError('项目已被其他实例使用或目录不可写，未抢占项目锁。')
        target = None
        try:
            target = (LocalProject.create(directory, *create, lock=lock) if create is not None
                      else LocalProject(directory, lock))
            output_directory = target.root / 'output'
            output_directory.mkdir(exist_ok=True)
            self.save(manual=False, reason='切换项目前自动保存')
        except Exception:
            target.close() if target else lock.unlock()
            raise
        self._detach_removed_image()
        self.release_project()
        self.store, self.project = target, target.project
        self.dialog.outputDirWidget.setFilePath(str(output_directory))
        self.destroyed.connect(lambda obj=None, store=target: store.close())
        recent = [row for row in self._recent() if row.get('directory') != directory]
        self.settings.setValue('LandCoverClassification/local_projects', json.dumps([self.project] + recent[:19], ensure_ascii=False))
        self.refresh()
        self._freeze_inputs(False)
        self.label.setText('已打开本地项目：{}。请选择工作影像。'.format(self.project['name']))

    def _set_images(self, rows):
        self.images.blockSignals(True)
        try:
            self.images.clear()
            self.images.addItem('选择项目影像继续编辑', None)
            for row in rows:
                self.images.addItem(row['name'] + ('（有草稿）' if row['recovery_id'] else '（未生成成果）'), row)
        finally:
            self.images.blockSignals(False)
        self._select_row(self.images, self.image)

    def refresh_images(self):
        self.selected_project()
        rows = self.store.images()
        self._set_images(rows)
        return rows

    def manage_images(self):
        from .history_dialogs import ImageManager
        self._show_history_window('_image_manager', ImageManager)

    def change_image_membership(self, row, removed):
        """移除前保存当前影像，仅修改工作关联。"""
        self._check_idle()
        if row['project_id'] != self.selected_project()['id']:
            raise ValueError('影像不属于当前项目。')
        current = bool(self.image and self.image['id'] == row['id'])
        if removed:
            if QtWidgets.QMessageBox.question(self, '移除项目影像',
                    '移除“{}”的工作关联？原始文件、草稿和历史均保留，可重新加入。'.format(row['name'])) != QtWidgets.QMessageBox.Yes:
                return False
            if current:
                self.save(manual=False, reason='移除影像前自动保存')
        self.store.set_removed(row['id'], removed)
        if removed and current:
            self._detach_removed_image()
        self.refresh_images()
        self.label.setText('影像已移除，草稿和历史保留。' if removed else '影像已重新加入，沿用原编号和草稿。')
        return True

    def _detach_removed_image(self):
        """解除工作状态，持久文件由项目独立持有。"""
        for name in ('_recovery_browser', '_attribute_browser'):
            window = getattr(self, name, None)
            if window is not None and not sip.isdeleted(window):
                window.close()
        self.switching = True
        try:
            d = self.dialog
            d._stop_ai_editing(silent=True)
            d._clear_drawn_roi()
            d._discard_draft_session()
            d._input_adapter_request_id += 1
            d._input_adapter.clear()
            d._input_path = d._input_layer = d._label_path = None
            d._invalidate_fusion_undo()
            self.image = None
            self._save_error = False
            self._set_inputs('', None)
            d.exportRasterBtn.setEnabled(False)
        finally:
            self.switching = False

    def _freeze_inputs(self, frozen):
        self.dialog.inputGroup.setEnabled(not frozen)
        self.dialog.mDemFile.setEnabled(not frozen)

    def _set_inputs(self, path, dem):
        widgets = (self.dialog.fileRadio, self.dialog.layerRadio, self.dialog.inputFileWidget, self.dialog.mDemFile)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.dialog.fileRadio.setChecked(True)
            self.dialog.inputFileWidget.setFilePath(path)
            self.dialog.mDemFile.setFilePath(dem or '')
            self.dialog.inputFileWidget.setEnabled(True)
            self.dialog.layerCombo.setEnabled(False)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def bind_input(self, path):
        """同影像无操作绑定不拦截 AI 追加，真实切换仍检查忙碌状态。"""
        self.selected_project()
        dem = self.dialog._resolve_dem_path()
        if dem is False:
            raise RuntimeError('请选择有效 DEM。')
        path = os.path.normcase(os.path.realpath(path))
        same = self.image and path == os.path.normcase(os.path.realpath(self.image['image_path']))
        if same and (dem or None) == self.image['dem_path']:
            return
        self._check_idle()
        self.save(manual=False, reason='切换输入前自动保存')
        row = self.store.register_image(path, dem, reference_grid(path))
        if same:
            self.image = row
        else:
            self._activate_image(row, preserve_input=True)
        self.refresh_images()

    def open_image(self):
        row = self.images.currentData()
        if not row or (self.image and row['id'] == self.image['id']):
            return
        self.save(manual=False, reason='切换影像前自动保存')
        self._activate_image(self.store.image(row['id'], writable=True))

    def _activate_image(self, row, preserve_input=False):
        """先验证输入和快照，再解除旧会话。"""
        if not os.path.isfile(row['image_path']):
            replacement, _ = QtWidgets.QFileDialog.getOpenFileName(self, '影像缺失，请重新定位原文件')
            if not replacement:
                raise RuntimeError('影像尚未重新定位，草稿仍保留。')
            row = self.store.relocate(row['id'], replacement, reference_grid(replacement))
        reference_grid(row['image_path'])
        if input_fingerprint(row['image_path']) != row['fingerprint']:
            raise ValueError('影像内容已改变，不能沿用原草稿身份。')
        if row['dem_path'] and not os.path.isfile(row['dem_path']):
            raise RuntimeError('DEM 路径失效，请在管理影像中更新或清除。')
        source = self.store.snapshot(row['id'], row['recovery_id']) if row['recovery_id'] else None
        if source:
            from .recovery import validate_source
            validate_source(self, source, image=row)
        layer_input = preserve_input and self.dialog.layerRadio.isChecked()
        self._detach_removed_image()
        self.switching = True
        try:
            self.image = row
            self._set_inputs(row['image_path'], row['dem_path'])
            if layer_input:
                self.dialog.fileRadio.blockSignals(True)
                self.dialog.layerRadio.blockSignals(True)
                try:
                    self.dialog.layerRadio.setChecked(True)
                    self.dialog.inputFileWidget.setEnabled(False)
                    self.dialog.layerCombo.setEnabled(True)
                finally:
                    self.dialog.fileRadio.blockSignals(False)
                    self.dialog.layerRadio.blockSignals(False)
            self.dialog._initialize_working_image()
            if os.path.normcase(self.dialog._input_path or '') != os.path.normcase(row['image_path']):
                raise RuntimeError('无法加载影像，项目草稿和历史仍保留。')
            if source:
                if not self.dialog._ensure_draft_session(row['image_path']):
                    raise RuntimeError('无法创建持久草稿工作目录。')
                destination = self.dialog._draft_session.next_generation_path('opened')
                snapshot_gpkg(source, destination)
                self._opening_revision = row['revision']
                try:
                    self.dialog._publish_draft_generation(destination)
                finally:
                    self._opening_revision = None
        finally:
            self.switching = False
        self._select_row(self.images, self.image)
        self.dialog._suggest_output_paths()
        self._freeze_inputs(False)
        if not self._save_error:
            self.label.setText('当前影像：{}；已载入最近已登记草稿。'.format(row['name']))

    def export_context(self):
        """生成或覆盖成果前核对项目、影像与草稿身份。"""
        if not self.active or self.image['project_id'] != self.project['id']:
            raise ValueError('当前影像未关联到所选项目。')
        session = self.dialog._draft_session
        if not session.is_active or os.path.normcase(os.path.realpath(session.input_path)) != os.path.normcase(os.path.realpath(self.image['image_path'])):
            raise ValueError('当前草稿与项目影像不一致。')
        result_context(self.image['id'], self.project.get('ztz'))
        return {'image_id': self.image['id'], 'ztz': self.project.get('ztz')}

    def save(self, exiting=False, manual=True, reason='手动保存'):
        """提交、校验、保存；失败保留旧快照与当前工作文件。"""
        session = self.dialog._draft_session
        if not self.active or not session.is_active:
            return
        try:
            self.export_context()
            layer = self.dialog._draft_layer
            if self.dialog._layer_is_usable(layer) and not self.dialog._commit_layer_if_needed(layer):
                raise RuntimeError('草稿编辑提交失败，请解决后重试。')
            from .recovery import validate_source
            validate_source(self, session.generation_path)
            self.image = self.store.save(self.image['id'], session.generation_path, manual, reason)
            session.revision = self.image['revision']
            self._save_error = False
            self.refresh_images()
            if not exiting:
                self.label.setText('当前草稿已本地保存。')
        except Exception:
            self._save_error = True
            raise

    def record_recovery(self, reason='编辑会话'):
        """登记失败单独显示，不能把新草稿误报为可靠保存。"""
        session = self.dialog._draft_session
        if not self.active or not session.is_active:
            return
        try:
            self.export_context()
            self.image = self.store.register_generation(self.image['id'], session.generation_path, reason, self._opening_revision)
            session.revision = self.image['revision']
            self._save_error = False
        except Exception as exc:
            session.revision = None
            self._save_error = True
            self.label.setText('自动恢复登记失败，工作文件保留：{}'.format(session.generation_path))
            self.exit_notice('自动恢复登记失败：{}；保留文件 {}'.format(exc, session.generation_path))

    def inspect_feature(self):
        """独立启用地图点选，在非模态浮窗展示单个滑坡属性。"""
        self.dialog._workflow_ui.inspector.start_pick()

    def preview(self):
        browser = getattr(self, '_attribute_browser', None)
        if browser is None or sip.isdeleted(browser) or not browser.valid():
            if browser is not None and not sip.isdeleted(browser):
                browser.close()
                browser.deleteLater()
            browser = self._attribute_browser = self._create_result_preview()
        browser.refresh()
        browser.show()
        browser.raise_()
        browser.activateWindow()

    def _create_result_preview(self):
        if not self.dialog._layer_is_usable(self.dialog._draft_layer):
            raise RuntimeError('当前没有草稿。')
        from .attribute_browser import AttributeBrowser
        return AttributeBrowser(self)

    def recovery(self):
        from .history_dialogs import RecoveryBrowser
        self._show_history_window('_recovery_browser', RecoveryBrowser)

    def _show_history_window(self, name, factory):
        previous = getattr(self, name, None)
        if previous is not None and not sip.isdeleted(previous):
            previous.close()
            previous.deleteLater()
        window = factory(self)
        setattr(self, name, window)
        window.show()
        window.raise_()
        window.load()

    def upload_results(self):
        """预留独立入口，不保存、不打包、不请求网络。"""
        self.selected_project()
        saved = [row for row in self.store.images() if row['draft_id']]
        if not saved:
            raise ValueError('项目没有有效已保存草稿。')
        for row in saved:
            self.store.snapshot(row['id'], row['draft_id'])
        self.label.setText('成果上传功能尚未接入，当前成果已保存在本地，可通过导出功能生成文件。')
