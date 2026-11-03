# -*- coding: utf-8 -*-
"""版本对比与草稿恢复窗口，读取与替换操作具有明确边界。"""

import json
import hashlib
import os
from urllib.parse import urlencode

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsJsonUtils, QgsProject, QgsWkbTypes)
from qgis.gui import QgsRubberBand

from . import recovery
from .project_client import snapshot_gpkg


class HistoryWindow(QtWidgets.QDialog):
    """固定打开时的业务身份，旧窗口不能对新项目发起操作。"""

    def __init__(self, panel, title):
        super().__init__(panel, Qt.Window)
        self.panel = panel
        self.project_id = panel.selected_project()['id']
        self.image_id = (panel.image or {}).get('id')
        self.setWindowTitle(title)
        self.layout = QtWidgets.QVBoxLayout(self)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.resize(1000, 550)
        self._running = False
        self._closing = False
        panel.closing_requested.connect(self.close)
        panel.dialog.finished.connect(self.close)

    def check_context(self, image=False):
        p = self.panel
        if self._closing or p._closed or not p.project or p.project['id'] != self.project_id:
            raise RuntimeError('项目已切换或窗口已关闭，请重新打开。')
        if image and (not p.image or p.image['id'] != self.image_id):
            raise RuntimeError('影像已切换，请重新打开恢复管理。')
        p._check_idle()

    def run(self, action):
        """统一捕获 Qt 回调错误，避免异常对话框阻塞 QGIS。"""
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


class VersionBrowser(HistoryWindow):
    """仅比较已发布版本，地图展示当前所选变化的前后边界。"""

    def __init__(self, panel):
        super().__init__(panel, '已发布版本对比 · 只读')
        self.offset = 0
        self.next_offset = None
        self.rows = []
        self.bands = []
        self.pair = None
        hint = QtWidgets.QLabel('按同一影像内的要素编号比较；重新推理产生新编号时显示为删除和新增。\n'
                                '红色为旧边界，绿色为新边界；对比不会改变草稿或当前发布版本。')
        hint.setWordWrap(True)
        self.layout.addWidget(hint)
        controls = QtWidgets.QHBoxLayout()
        self.before = QtWidgets.QComboBox()
        self.after = QtWidgets.QComboBox()
        controls.addWidget(QtWidgets.QLabel('旧版本'))
        controls.addWidget(self.before)
        controls.addWidget(QtWidgets.QLabel('新版本'))
        controls.addWidget(self.after)
        self.button(controls, '比较', self.compare)
        self.layout.addLayout(controls)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['变化', '影像编号', '滑坡编号', '旧面积 m²', '新面积 m²'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(lambda: self.run(self.select_change))
        self.layout.addWidget(self.table)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(130)
        self.layout.addWidget(self.details)
        navigation = QtWidgets.QHBoxLayout()
        self.prev = self.button(navigation, '上一页', self.previous)
        self.next = self.button(navigation, '下一页', self.following)
        self.button(navigation, '定位变化', self.locate)
        self.layout.addLayout(navigation)
        self.layout.addWidget(self.status)

    def load(self):
        self.run(self._load)

    def _load(self):
        rows = self.panel.call(lambda: self.panel._client().request(
            'GET', '/internal/projects/{}/versions'.format(self.project_id)))
        self.check_context()
        for row in rows:
            label = '{} · {} · {} 个{}'.format(row['created_at'], str(row['id'])[:8],
                                               row['feature_count'], '（当前）' if row['is_current'] else '')
            self.before.addItem(label, row['id'])
            self.after.addItem(label, row['id'])
        if len(rows) > 1:
            self.before.setCurrentIndex(1)
        self.status.setText('请选择两个版本比较。' if rows else '当前项目尚无已发布版本。')
        self.prev.setEnabled(False)
        self.next.setEnabled(False)

    def compare(self):
        self.clear_bands()
        self.rows = []
        self.table.setRowCount(0)
        self.details.clear()
        self.prev.setEnabled(False)
        self.next.setEnabled(False)
        self.pair = (self.before.currentData(), self.after.currentData())
        if not all(self.pair):
            raise ValueError('请选择两个已发布版本。')
        self.offset = 0
        self.read_page()

    def read_page(self):
        before, after = self.pair
        path = '/internal/projects/{}/compare?{}'.format(self.project_id, urlencode(
            dict(before=before, after=after, offset=self.offset, limit=100)))
        response = self.panel.call(lambda: self.panel._client().request('GET', path))
        self.check_context()
        if (response['before'], response['after']) != self.pair:
            raise ValueError('版本对比响应与所选版本不一致。')
        self.clear_bands()
        self.rows = response['items']
        self.next_offset = response['next_offset']
        self.table.setRowCount(0)
        labels = dict(added='新增', removed='删除', modified='修改')
        for row in self.rows:
            index = self.table.rowCount()
            self.table.insertRow(index)
            values = [labels[row['change']], row['image_id'], row['feature_id'],
                      (row['before_properties'] or {}).get('mj'), (row['after_properties'] or {}).get('mj')]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem('' if value is None else str(value))
                item.setToolTip(item.text())
                self.table.setItem(index, col, item)
        self.details.clear()
        self.table.resizeColumnsToContents()
        summary = response['summary']
        self.status.setText('新增 {} · 删除 {} · 修改 {} · 未变 {}；第 {} 页。'.format(
            *[summary.get(key, 0) for key in ('added', 'removed', 'modified', 'unchanged')], self.offset // 100 + 1))
        self.prev.setEnabled(self.offset > 0)
        self.next.setEnabled(self.next_offset is not None)

    def previous(self):
        if self.pair:
            self.offset = max(0, self.offset - 100)
            self.read_page()

    def following(self):
        if self.next_offset is not None:
            self.offset = self.next_offset
            self.read_page()

    def clear_bands(self):
        for band in self.bands:
            band.hide()
            band.reset(QgsWkbTypes.PolygonGeometry)
            # QgsRubberBand 是图形项，不是 QObject，必须从场景移除后释放。
            if band.scene() is not None:
                band.scene().removeItem(band)
            sip.delete(band)
        self.bands = []

    def select_change(self):
        self.clear_bands()
        index = self.table.currentRow()
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        old, new = row['before_properties'] or {}, row['after_properties'] or {}
        changes = ['{}：{} → {}'.format(key, old.get(key), new.get(key)) for key in sorted(set(old) | set(new))
                   if key not in ('version_id', 'created_at') and old.get(key) != new.get(key)]
        self.details.setPlainText('\n'.join(changes) or '属性未变化；请对照地图检查几何边界。')
        for key, color in [('before_geometry', '#e53935'), ('after_geometry', '#00a65a')]:
            if not row.get(key):
                continue
            feature = QgsJsonUtils.stringToFeatureList(json.dumps(
                {'type': 'Feature', 'geometry': row[key], 'properties': {}}))[0]
            band = QgsRubberBand(self.panel.dialog.iface.mapCanvas(), QgsWkbTypes.PolygonGeometry)
            band.setColor(QColor(color))
            band.setFillColor(QColor(0, 0, 0, 0))
            band.setWidth(3)
            band.setToGeometry(feature.geometry(), QgsCoordinateReferenceSystem(4326))
            self.bands.append(band)

    def locate(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.rows):
            return
        canvas = self.panel.dialog.iface.mapCanvas()
        bounds = None
        for key in ('before_geometry', 'after_geometry'):
            geometry = self.rows[index].get(key)
            if geometry:
                feature = QgsJsonUtils.stringToFeatureList(json.dumps(
                    {'type': 'Feature', 'geometry': geometry, 'properties': {}}))[0]
                box = feature.geometry().boundingBox()
                if bounds is None:
                    bounds = box
                else:
                    bounds.combineExtentWith(box)
        if bounds is not None:
            transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(4326),
                                               canvas.mapSettings().destinationCrs(), QgsProject.instance())
            bounds = transform.transformBoundingBox(bounds)
            bounds.scale(1.2)
            canvas.setExtent(bounds)
            canvas.refresh()

    def closeEvent(self, event):
        self.clear_bands()
        super().closeEvent(event)

    def done(self, result):
        self.clear_bands()
        self._closing = True
        super().done(result)


class RecoveryBrowser(HistoryWindow):
    """显式浏览、检查和恢复；不自动删除任何历史文件。"""

    def __init__(self, panel):
        super().__init__(panel, '草稿恢复管理')
        self.rows = []
        self.offset = 0
        self.next_offset = None
        self.layout.addWidget(QtWidgets.QLabel('恢复到当前影像的本地草稿；恢复前自动备份，不自动保存或发布。'))
        self.list = QtWidgets.QListWidget()
        self.layout.addWidget(self.list)
        buttons = QtWidgets.QHBoxLayout()
        self.button(buttons, '检查所选草稿', self.inspect)
        self.button(buttons, '恢复所选草稿', self.restore)
        self.button(buttons, '导出所选副本', self.export_copy)
        self.button(buttons, '导入旧草稿文件…', self.import_legacy)
        self.layout.addLayout(buttons)
        navigation = QtWidgets.QHBoxLayout()
        self.button(navigation, '重新读取记录', self._load)
        self.next = self.button(navigation, '更多服务端快照', self.more)
        self.button(navigation, '打开本地备份目录', self.open_folder)
        self.layout.addLayout(navigation)
        self.layout.addWidget(self.status)

    def load(self):
        self.run(self._load)

    def append(self, rows):
        for row in rows:
            self.rows.append(row)
            self.list.addItem('{} · {} · {}{}'.format(
                '本地' if row['kind'] == 'local' else '服务端', row['created_at'],
                row.get('reason', row.get('id', '')), '（当前保存）' if row.get('is_current') else ''))

    def _load(self):
        self.rows = []
        self.list.clear()
        self.offset = 0
        self.append(recovery.local_records(self.panel))
        self.status.setText('已读取本地恢复记录。')
        self.next.setEnabled(False)
        self.read_server()

    def read_server(self):
        try:
            result = self.panel.call(lambda: self.panel._client().request(
                'GET', self.panel._image_path('/drafts') + '?offset={}&limit=100'.format(self.offset)))
            self.check_context(True)
            self.append([{**row, 'kind': 'server'} for row in result['items']])
            self.next_offset = result['next_offset']
            self.next.setEnabled(self.next_offset is not None)
            self.status.setText('已读取 {} 条记录。旧版未登记的保留文件可通过“导入旧草稿文件”选择。'.format(len(self.rows)))
        except Exception as exc:
            if not sip.isdeleted(self) and not self._closing:
                self.status.setText('本地记录仍可使用；服务端历史读取失败：{}'.format(exc))

    def more(self):
        if self.next_offset is not None:
            self.offset = self.next_offset
            self.read_server()

    def selected(self):
        index = self.list.currentRow()
        if not 0 <= index < len(self.rows):
            raise ValueError('请先选择一条恢复记录。')
        return self.rows[index]

    def with_source(self, action):
        row = self.selected()
        if row['kind'] == 'local':
            row = recovery.read_record(row['record'], self.panel)
            action(row['path'])
        else:
            with self.panel.request_directory('lcc_history_') as directory:
                destination = os.path.join(directory, 'draft.gpkg')
                headers = self.panel.call(lambda: self.panel._client().request(
                    'GET', self.panel._image_path('/draft') + '?draft_id=' + row['id'], download=destination))
                self.check_context(True)
                if headers.get('X-Draft-ID') != row['id']:
                    raise ValueError('下载的草稿编号与所选记录不符。')
                digest_builder = hashlib.sha256()
                with open(destination, 'rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest_builder.update(chunk)
                digest = digest_builder.hexdigest()
                if digest != row['sha256']:
                    raise ValueError('下载草稿的完整性校验失败，未执行恢复。')
                action(destination)

    def inspect(self):
        self.with_source(lambda path: self.status.setText(
            '草稿检查通过：{} 个滑坡；CRS、影像边界、字段及要素编号符合要求。'.format(
                recovery.validate_source(self.panel, path))))

    def confirm_restore(self, path):
        count = recovery.validate_source(self.panel, path)
        message = ('将用这份含 {} 个滑坡的草稿替换当前本地草稿。\n'
                   '当前编辑先提交到本地并自动备份；服务端草稿和发布版本不变。\n是否继续？').format(count)
        if QtWidgets.QMessageBox.question(self, '确认恢复', message) != QtWidgets.QMessageBox.Yes:
            return
        self.check_context(True)
        backup = recovery.restore_source(self.panel, path)
        self.status.setText('已恢复到本地，待您保存。恢复前备份：' + backup)

    def restore(self):
        self.with_source(self.confirm_restore)

    def export_copy(self):
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出恢复副本', '', 'GeoPackage (*.gpkg)')
        if destination:
            if not destination.lower().endswith('.gpkg'):
                destination += '.gpkg'
            source_paths = {os.path.normcase(os.path.realpath(row['path'])) for row in self.rows if row.get('path')}
            source_paths.add(os.path.normcase(os.path.realpath(self.panel.dialog._draft_session.generation_path or '.')))
            if os.path.normcase(os.path.realpath(destination)) in source_paths:
                raise ValueError('请选择其他路径，不能覆盖当前草稿或恢复源。')
            self.with_source(lambda source: snapshot_gpkg(source, destination))
            self.status.setText('恢复副本已导出：' + destination)

    def import_legacy(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择旧版保留的草稿', '', 'GeoPackage (*.gpkg)')
        if path:
            self.check_context(True)
            self.confirm_restore(path)

    def open_folder(self):
        root = recovery.recovery_root()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))
