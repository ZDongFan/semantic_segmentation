"""本地影像关联与草稿历史窗口，窗口固定打开时的项目身份。"""

from pathlib import Path

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices

from . import recovery
from .local_project import snapshot_gpkg


class HistoryWindow(QtWidgets.QDialog):
    """固定项目、影像身份，避免旧窗口作用于切换后的对象。"""

    def __init__(self, panel, title):
        super().__init__(panel, Qt.Window)
        self.panel = panel
        self.project_id = panel.selected_project()['id']
        self.image_id = (panel.image or {}).get('id')
        self.setWindowTitle(title)
        self.layout = QtWidgets.QVBoxLayout(self)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.resize(900, 500)
        self._running = self._closing = False
        panel.closing_requested.connect(self.close)
        panel.dialog.finished.connect(self.close)

    def check_context(self, image=False):
        p = self.panel
        if self._closing or p._closed or not p.store or p.project['id'] != self.project_id:
            raise RuntimeError('项目已切换或窗口已关闭，请重新打开。')
        if image and (not p.image or p.image['id'] != self.image_id):
            raise RuntimeError('影像已切换，请重新打开恢复管理。')
        p._check_idle()

    def run(self, action):
        """所有窗口回调统一捕获错误。"""
        if self._running:
            return
        self._running = True
        self.setEnabled(False)
        try:
            self.check_context(isinstance(self, RecoveryBrowser))
            action()
        except Exception as exc:
            if not sip.isdeleted(self) and not self._closing:
                self.status.setText('操作未完成：{}'.format(exc))
        finally:
            self._running = False
            if not sip.isdeleted(self):
                self.setEnabled(True)

    def button(self, layout, title, action):
        button = QtWidgets.QPushButton(title)
        button.clicked.connect(lambda checked=False: self.run(action))
        layout.addWidget(button)
        return button

    def done(self, result):
        self._closing = True
        super().done(result)

    def closeEvent(self, event):
        self._closing = True
        super().closeEvent(event)


class ImageManager(HistoryWindow):
    """失效输入仍可重新定位、更新 DEM、移除或重新加入。"""

    def __init__(self, panel):
        super().__init__(panel, '管理项目影像')
        self.rows = []
        hint = QtWidgets.QLabel('移除只解除工作关联，保留原始文件、草稿与全部手动历史。')
        hint.setWordWrap(True)
        self.layout.addWidget(hint)
        self.table = QtWidgets.QTableWidget(0, 4, self)
        self.table.setObjectName('projectImageManagementTable')
        self.table.setHorizontalHeaderLabels(['影像名称', '影像路径', '草稿', '关联状态'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.layout.addWidget(self.table)
        buttons = QtWidgets.QHBoxLayout()
        self.remove = self.button(buttons, '移除', lambda: self.change(True))
        self.rejoin = self.button(buttons, '重新加入', lambda: self.change(False))
        self.button(buttons, '重新定位影像…', self.relocate)
        self.button(buttons, '更新 DEM…', self.change_dem)
        self.button(buttons, '清除 DEM', lambda: self.change_dem(clear=True))
        self.button(buttons, '重新读取列表', self._load)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.layout.addLayout(buttons)
        self.layout.addWidget(self.status)
        self.update_actions()

    def load(self):
        self.run(self._load)

    def _load(self):
        self.rows = self.panel.store.images(include_removed=True)
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            for column, value in enumerate((row['name'], row['image_path'],
                    '有草稿' if row['recovery_id'] else '尚无草稿', '已移除' if row['removed_at'] else '项目中')):
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(value))
        self.update_actions()
        self.status.setText('共 {} 景影像。'.format(len(self.rows)))

    def selected(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.rows):
            raise ValueError('请先选择影像。')
        return self.rows[index]

    def update_actions(self):
        index = self.table.currentRow()
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        self.remove.setEnabled(bool(row and not row['removed_at']))
        self.rejoin.setEnabled(bool(row and row['removed_at']))

    def change(self, removed):
        if self.panel.change_image_membership(self.selected(), removed):
            self._load()

    def relocate(self):
        from .project_panel import reference_grid
        row = self.selected()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '重新定位同一影像')
        if not path:
            return
        current = self.panel.image and self.panel.image['id'] == row['id']
        if current:
            self.panel.save(manual=False, reason='重新定位前自动保存')
        updated = self.panel.store.relocate(row['id'], path, reference_grid(path))
        if current:
            self.panel._activate_image(updated)
        self.panel.refresh_images()
        self._load()

    def change_dem(self, clear=False):
        row = self.selected()
        path = None
        if not clear:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择 DEM')
            if not path:
                return
        if self.panel.image and self.panel.image['id'] == row['id']:
            self.panel.save(manual=False, reason='更新 DEM 前自动保存')
        updated = self.panel.store.update_dem(row['id'], path)
        if self.panel.image and self.panel.image['id'] == row['id']:
            self.panel.image = updated
            self.panel._set_inputs(updated['image_path'], updated['dem_path'])
        self.panel.refresh_images()
        self._load()


class RecoveryBrowser(HistoryWindow):
    """只展示项目内已登记的手动历史与最新自动恢复，不扫描未知文件。"""

    def __init__(self, panel):
        super().__init__(panel, '本地草稿恢复管理')
        self.rows = []
        self.layout.addWidget(QtWidgets.QLabel('恢复前保留当前草稿；恢复后的内容需要再次本地保存。'))
        self.list = QtWidgets.QListWidget()
        self.layout.addWidget(self.list)
        buttons = QtWidgets.QHBoxLayout()
        self.button(buttons, '检查所选草稿', self.inspect)
        self.button(buttons, '恢复所选草稿', self.restore)
        self.button(buttons, '导出所选副本', self.export_copy)
        self.button(buttons, '重新读取历史', self._load)
        self.button(buttons, '打开项目目录', self.open_folder)
        self.layout.addLayout(buttons)
        self.layout.addWidget(self.status)

    def load(self):
        self.run(self._load)

    def _load(self):
        self.rows = self.panel.store.history(self.image_id)
        self.list.clear()
        for row in self.rows:
            kind = {'manual': '手动保存', 'automatic': '最近自动恢复', 'restore_backup': '恢复前保留点'}[row['save_kind']]
            self.list.addItem('{} · {} · {}'.format(kind, row['created_at'], row['save_reason']))
        self.status.setText('已读取 {} 条本地记录。'.format(len(self.rows)))

    def selected(self):
        index = self.list.currentRow()
        if not 0 <= index < len(self.rows):
            raise ValueError('请先选择草稿历史。')
        return self.rows[index]

    def source(self):
        self.check_context(True)
        return self.panel.store.snapshot(self.image_id, self.selected()['id'])

    def inspect(self):
        count = recovery.validate_source(self.panel, self.source())
        self.status.setText('草稿检查通过：{} 个要素，身份、完整性和几何有效。'.format(count))

    def restore(self):
        identifier = self.selected()['id']
        count = recovery.validate_source(self.panel, self.source())
        if QtWidgets.QMessageBox.question(self, '确认恢复',
                '用这份含 {} 个要素的草稿替换当前草稿？当前内容将先保留为恢复点。'.format(count)) != QtWidgets.QMessageBox.Yes:
            return
        self.check_context(True)
        recovery.restore_source(self.panel, identifier)
        self._load()
        self.status.setText(self.panel.label.text())

    def export_copy(self):
        source = self.source()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出历史副本', '', 'GeoPackage (*.gpkg)')
        if path:
            if not path.lower().endswith('.gpkg'):
                path += '.gpkg'
            if Path(self.panel.store.root) == Path(path).resolve() or Path(self.panel.store.root) in Path(path).resolve().parents:
                raise ValueError('请导出到项目目录以外，避免覆盖项目内部文件。')
            snapshot_gpkg(source, path)
            self.status.setText('副本已导出：' + path)

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.panel.store.root)))
