# -*- coding: utf-8 -*-
"""独立地图点选与单个滑坡属性浮窗，不占用插件面板布局。"""

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QPoint, QTimer, pyqtSignal, pyqtSlot
from qgis.gui import QgsMapToolIdentifyFeature

from .feature_attributes import COMMON_FIELDS, FeatureAttributes, format_attribute


class FeaturePickTool(QgsMapToolIdentifyFeature):
    """沿用 QGIS 识别行为，仅在左键松开时查询，Esc 退出。"""

    cancelled = pyqtSignal()

    def __init__(self, inspector, layer):
        super().__init__(inspector.canvas, layer)
        self.setParent(inspector)
        self.inspector = inspector

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.inspector.ready():
            super().canvasReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class FeatureInspector(QtWidgets.QWidget):
    """非模态详情浮窗，绑定项目、影像、图层和 generation 身份。"""

    def __init__(self, panel):
        super().__init__(panel.dialog, Qt.Tool)
        self.setObjectName('singleLandslideDetails')
        self.setWindowTitle('滑坡详情')
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMinimumWidth(340)
        self.panel = panel
        self.dialog = panel.dialog
        self.canvas = self.dialog.iface.mapCanvas()
        self.layer = self.identity = self.tool = self.previous_tool = None
        self.fid = self.feature_uuid = None
        self._layer_connections = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.body = QtWidgets.QWidget(self)
        self.grid = QtWidgets.QGridLayout(self.body)
        self.grid.setContentsMargins(4, 0, 4, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        positions = {'class_name': (0, 0), 'source_id': (0, 2),
                     'mj': (1, 0), 'perimeter_m': (1, 2),
                     'center_lon': (2, 0), 'center_lat': (2, 2), 'ztz': (3, 0)}
        self.values = {}
        for name, title in COMMON_FIELDS:
            value = QtWidgets.QLabel('—', self.body)
            value.setObjectName('singleAttribute_' + name)
            value.setTextFormat(Qt.PlainText)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            value.setWordWrap(True)
            value.setMinimumWidth(0)
            value.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            row, column = positions[name]
            self.grid.addWidget(QtWidgets.QLabel(title, self.body), row, column)
            self.grid.addWidget(value, row, column + 1, 1, 3 if name == 'ztz' else 1)
            self.values[name] = value
        self.status = QtWidgets.QLabel('单击滑坡查看属性，Esc 关闭详情并退出点选。', self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(self.body)
        layout.addWidget(self.status)
        self.resize(430, self.sizeHint().height())
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.canvas.mapToolSet.connect(self.map_tool_changed)
        panel.closing_requested.connect(self.reset)
        self.dialog.finished.connect(self.reset)

    def show_details(self):
        """默认位于 QGIS 右上区域，连续点选保留用户拖动位置。"""
        opening = not self.isVisible()
        self.show()
        if opening:
            window = self.dialog.iface.mainWindow()
            anchor = window.mapToGlobal(QPoint(round(window.width() * 0.94), 0))
            anchor.setY(self.canvas.mapToGlobal(QPoint(0, round(self.canvas.height() * 0.12))).y())
            screen = QtWidgets.QApplication.screenAt(anchor) or self.screen()
            available = screen.availableGeometry()
            frame = self.frameGeometry()
            x = max(available.left(), min(anchor.x() - frame.width() - 12,
                                         available.right() - frame.width() + 1))
            y = max(available.top(), min(anchor.y(),
                                        available.bottom() - frame.height() + 1))
            self.move(x, y)
        self.raise_()

    def closeEvent(self, event):
        self.stop_pick()
        self.timer.stop()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            self.canvas.setFocus(Qt.OtherFocusReason)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _identity(self):
        p, layer = self.panel, self.dialog._draft_layer
        if p.active and self.dialog._layer_is_usable(layer):
            return (p.project['id'], p.image['id'], layer.id(), layer.source())
        return None

    def ready(self):
        """事件到达时重新检查身份与忙碌状态，拒绝旧工具回调。"""
        return bool(self.identity and self.identity == self._identity()
                    and self.layer is self.dialog._draft_layer
                    and not self.panel._closed and not self.dialog._workflow_ui.busy())

    def sync_context(self, associated, busy):
        """工作区刷新时绑定新草稿，换代后不沿用旧 FID。"""
        identity = self._identity() if associated else None
        if identity != self.identity:
            self.reset()
            if identity:
                self.layer = self.dialog._draft_layer
                self.identity = identity
                for signal, slot in ((self.layer.dataSourceChanged, self.invalidate),
                                     (self.layer.willBeDeleted, self.invalidate),
                                     (self.layer.layerModified, self.schedule),
                                     (self.layer.afterCommitChanges, self.schedule),
                                     (self.layer.geometryChanged, self.schedule),
                                     (self.layer.attributeValueChanged, self.schedule)):
                    signal.connect(slot)
                    self._layer_connections.append((signal, slot))
        if busy:
            self.stop_pick()

    @pyqtSlot()
    def start_pick(self):
        self.dialog._workflow_ui.refresh()
        if not self.ready():
            raise RuntimeError('当前没有可查询的关联草稿，请先打开项目影像。')
        if self.tool and self.canvas.mapTool() is self.tool:
            self.stop_pick()
            return
        if self.tool is None:
            self.tool = FeaturePickTool(self, self.layer)
            self.tool.featureIdentified.connect(self.picked)
            self.tool.cancelled.connect(self.close)
        self.previous_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self.tool)
        self.show_details()
        self.status.setText('单击滑坡查看属性，Esc 关闭详情并退出点选。')
        self.canvas.setFocus(Qt.OtherFocusReason)

    @pyqtSlot()
    def stop_pick(self):
        if self.tool and self.canvas.mapTool() is self.tool:
            if (self.previous_tool and not sip.isdeleted(self.previous_tool)
                    and not getattr(self.previous_tool, '_disposed', False)):
                self.canvas.setMapTool(self.previous_tool)
            else:
                self.canvas.unsetMapTool(self.tool)
        self.previous_tool = None
        self.panel.buttons['inspect_feature'].setChecked(False)
        if self.fid is not None:
            self.status.setText('点选已结束，点击按钮可继续。')

    @pyqtSlot()
    def map_tool_changed(self):
        active = bool(self.tool and self.canvas.mapTool() is self.tool)
        self.panel.buttons['inspect_feature'].setChecked(active)
        if not active and self.tool:
            self.previous_tool = None
            self.status.setText('点选已结束，点击按钮可继续。')

    def picked(self, feature):
        if not self.ready() or self.canvas.mapTool() is not self.tool:
            return
        self.fid = feature.id()
        self.feature_uuid = str(feature['feature_uuid'])
        self.layer.selectByIds([self.fid])
        self.refresh()
        self.show_details()

    @pyqtSlot()
    def schedule(self):
        if not sip.isdeleted(self.timer):
            self.timer.start(80)

    @pyqtSlot()
    def refresh(self):
        if self.identity != self._identity():
            self.reset()
            return
        if self.fid is None:
            return
        try:
            feature = self.layer.getFeature(self.fid)
            if not feature.isValid() or str(feature['feature_uuid']) != self.feature_uuid:
                self.clear_values()
                self.status.setText('该滑坡已删除，请重新点选。')
                return
            values = FeatureAttributes(self.layer, self.panel.export_context()).values(feature)
            for name, label in self.values.items():
                label.setText(format_attribute(name, values[name], detail=True))
                label.setToolTip(label.text())
            self.status.setText('可连续点选，Esc 关闭详情并退出点选。' if self.canvas.mapTool() is self.tool
                                else '点击按钮可继续查看。')
        except Exception as exc:
            self.clear_values()
            self.status.setText('属性读取失败：{}'.format(exc))

    def clear_values(self):
        self.fid = self.feature_uuid = None
        for label in self.values.values():
            label.setText('—')
            label.setToolTip('')

    @pyqtSlot()
    def invalidate(self):
        self.reset()
        self.dialog._workflow_ui.schedule()

    @pyqtSlot()
    def reset(self):
        self.stop_pick()
        self.timer.stop()
        for signal, slot in self._layer_connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._layer_connections = []
        if self.tool:
            self.tool.deleteLater()
            self.tool = None
        self.layer = self.identity = None
        self.clear_values()
        self.hide()
        self.status.setText('单击滑坡查看属性，Esc 关闭详情并退出点选。')
