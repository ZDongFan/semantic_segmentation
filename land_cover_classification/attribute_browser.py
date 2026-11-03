# -*- coding: utf-8 -*-
"""可并行操作地图的只读成果浏览器，按页计算当前草稿属性。"""

from itertools import islice

from qgis.PyQt import QtWidgets, sip
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSlot
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsDistanceArea, QgsExpression, QgsFeatureRequest, QgsProject)
from qgis.gui import QgsMapToolIdentifyFeature

from .result_attributes import RESULT_FIELDS, build_result_attributes


class AttributeBrowser(QtWidgets.QDialog):
    """表格与地图共享草稿选择，使用有限页避免一次创建全图属性单元格。"""

    PAGE_SIZE = 200

    def __init__(self, panel):
        super().__init__(panel, Qt.Window)
        self.panel = panel
        self.dialog = panel.dialog
        self.layer = self.dialog._draft_layer
        self.context = panel.export_context()
        if not self.layer.crs().isValid():
            raise ValueError("草稿缺少有效 CRS，无法计算成果面积。")
        self.identity = (panel.project['id'], panel.image['id'], self.layer.id())
        self.offset = 0
        self.fids = []
        self._syncing = False
        self.tool = None
        self.previous_tool = None
        self.setWindowTitle("滑坡属性 · 只读")
        self.setModal(False)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("地图点选可查看单个滑坡；单击表格高亮，双击定位。面积按 WGS84 椭球计算。\n"
                                "空白表示未赋值；字段不写回草稿，发布和导出仍按各自规则计算。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QtWidgets.QHBoxLayout()
        self.pick = QtWidgets.QPushButton("地图点选滑坡")
        self.pick.clicked.connect(self.start_pick)
        controls.addWidget(self.pick)
        self.selected = QtWidgets.QCheckBox("仅看选中滑坡")
        self.selected.toggled.connect(self.reset_page)
        controls.addWidget(self.selected)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索要素编号、来源编号或类别，按回车")
        self.search.returnPressed.connect(self.reset_page)
        controls.addWidget(self.search)
        layout.addLayout(controls)
        self.columns = [("feature_id", "要素编号")] + [
            (f.name, f.label + (" m²" if f.name == "mj" else "")) for f in RESULT_FIELDS
        ] + [("class_id", "类别编号"), ("class_name", "类别名称"), ("source_id", "来源编号"),
             ("perimeter_m", "周长 m"), ("center_lon", "中心经度"), ("center_lat", "中心纬度")]
        self.table = QtWidgets.QTableWidget(0, len(self.columns), self)
        self.table.setObjectName("resultAttributesTable")
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for col, (name, label) in enumerate(self.columns):
            item = QtWidgets.QTableWidgetItem(label + "\n" + name)
            item.setData(Qt.UserRole, name)
            self.table.setHorizontalHeaderItem(col, item)
            self.table.setColumnWidth(col, 150)
        self.table.itemSelectionChanged.connect(self.select_row)
        self.table.cellDoubleClicked.connect(self.locate)
        layout.addWidget(self.table)
        footer = QtWidgets.QHBoxLayout()
        self.view = QtWidgets.QComboBox(self)
        self.view.setObjectName("attributeFieldView")
        self.view.addItems(["常用字段", "全部字段（含完整编号）"])
        self.view.currentIndexChanged.connect(self.show_fields)
        footer.addWidget(self.view)
        for text, callback in [("上一页", self.previous_page), ("下一页", self.next_page),
                               ("上一个滑坡", lambda: self.navigate(-1)),
                               ("下一个滑坡", lambda: self.navigate(1)), ("定位", self.locate)]:
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(callback)
            footer.addWidget(button)
            if text == "上一页":
                self.prev = button
            elif text == "下一页":
                self.next = button
        layout.addLayout(footer)
        self.status = QtWidgets.QLabel()
        layout.addWidget(self.status)
        common = ["class_name", "mj", "perimeter_m", "ztz", "source_id"]
        names = [name for name, _ in self.columns]
        for pos, name in enumerate(common):
            header = self.table.horizontalHeader()
            header.moveSection(header.visualIndex(names.index(name)), pos)
        self.show_fields(0)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.layer.selectionChanged.connect(self.map_selection)
        self.layer.dataSourceChanged.connect(self.schedule)
        self.layer.layerModified.connect(self.schedule)
        self.layer.afterCommitChanges.connect(self.schedule)
        self.layer.willBeDeleted.connect(self.invalidate)
        self.dialog.finished.connect(self.close)
        panel.closing_requested.connect(self.close)
        screen = QtWidgets.QApplication.primaryScreen()
        self.resize(min(1100, screen.availableGeometry().width() - 80) if screen else 1100, 560)
        self.refresh()

    def valid(self):
        """旧窗口不能在切换项目、影像或 generation 时显示另一份上下文。"""
        p = self.panel
        return (not sip.isdeleted(self.layer) and self.layer.isValid() and p.project and p.image
                and self.identity == (p.project['id'], p.image['id'], self.layer.id())
                and self.dialog._draft_layer is self.layer)

    @pyqtSlot()
    def invalidate(self):
        self.stop_pick()
        self.timer.stop()
        self.table.setRowCount(0)
        self.fids = []
        self.setEnabled(False)
        self.status.setText("草稿已切换或移除，请重新打开属性窗口。")

    @pyqtSlot()
    def schedule(self):
        if not sip.isdeleted(self.timer):
            self.timer.start(80)

    @pyqtSlot()
    def reset_page(self):
        self.offset = 0
        self.refresh()

    @pyqtSlot()
    def refresh(self):
        if not self.valid():
            self.invalidate()
            return
        try:
            self.context = self.panel.export_context()
            request = QgsFeatureRequest()
            terms = []
            word = self.search.text().strip()
            if word:
                literal = QgsExpression.quotedString(word.lower())
                terms.append("(" + " OR ".join(
                    "strpos(lower(to_string(\"{}\")), {}) > 0".format(name, literal)
                    for name in ('feature_uuid', 'source_id', 'class_name')) + ")")
            if self.selected.isChecked():
                ids = self.layer.selectedFeatureIds()
                terms.append("$id IN ({})".format(','.join(map(str, ids))) if ids else 'FALSE')
            if terms:
                request.setFilterExpression(' AND '.join(terms))
            request.addOrderBy('$id')
            request.setLimit(self.offset + self.PAGE_SIZE + 1)
            iterator = self.layer.getFeatures(request)
            try:
                features = list(islice(iterator, self.offset, self.offset + self.PAGE_SIZE + 1))
            finally:
                iterator.close()
            distance = QgsDistanceArea()
            distance.setSourceCrs(self.layer.crs(), QgsProject.instance().transformContext())
            distance.setEllipsoid('WGS84')
            converter = QgsCoordinateTransform(self.layer.crs(), QgsCoordinateReferenceSystem(4326), QgsProject.instance())
            self._syncing = True
            self.table.setRowCount(0)
            self.fids = []
            for feature in features[:self.PAGE_SIZE]:
                geometry = feature.geometry()
                if geometry.isEmpty():
                    continue
                center = converter.transform(geometry.centroid().asPoint())
                values = build_result_attributes(**self.context, mj=distance.measureArea(geometry))
                values.update(feature_id=str(feature['feature_uuid']),
                              **{name: feature[name] for name in ('class_id', 'class_name', 'source_id')},
                              perimeter_m=distance.measurePerimeter(geometry), center_lon=center.x(), center_lat=center.y())
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.fids.append(feature.id())
                for col, (name, _) in enumerate(self.columns):
                    value = values[name]
                    text = '' if value is None else ('{:.3f}'.format(value) if name in ('mj', 'perimeter_m')
                            else '{:.7f}'.format(value) if name in ('center_lon', 'center_lat') else str(value))
                    item = QtWidgets.QTableWidgetItem(text)
                    item.setData(Qt.UserRole, value)
                    item.setToolTip('未赋值' if value is None else str(value))
                    self.table.setItem(row, col, item)
            self.prev.setEnabled(self.offset > 0)
            self.next.setEnabled(len(features) > self.PAGE_SIZE)
            self.status.setText("第 {} 页 · 本页 {} 个 · 地图选中 {} 个".format(
                self.offset // self.PAGE_SIZE + 1, len(self.fids), self.layer.selectedFeatureCount()))
        except Exception as exc:
            self.table.setRowCount(0)
            self.fids = []
            self.status.setText("属性读取失败：{}".format(exc))
        finally:
            self._syncing = False
        self.highlight_selection()

    @pyqtSlot()
    def map_selection(self):
        if self._syncing:
            return
        if self.selected.isChecked():
            self.reset_page()
        else:
            self.highlight_selection()

    def highlight_selection(self):
        if not self.valid():
            return
        self._syncing = True
        try:
            self.table.clearSelection()
            ids = set(self.layer.selectedFeatureIds())
            for row, fid in enumerate(self.fids):
                if fid in ids:
                    self.table.selectRow(row)
                    break
        finally:
            self._syncing = False

    @pyqtSlot()
    def select_row(self):
        row = self.table.currentRow()
        if not self._syncing and self.valid() and 0 <= row < len(self.fids):
            self._syncing = True
            try:
                self.layer.selectByIds([self.fids[row]])
            finally:
                self._syncing = False

    @pyqtSlot()
    def start_pick(self):
        if not self.valid() or self.dialog._workflow_ui.busy():
            self.status.setText("请等待当前操作完成后点选。")
            return
        canvas = self.dialog.iface.mapCanvas()
        if self.tool is None:
            self.tool = QgsMapToolIdentifyFeature(canvas, self.layer)
            self.tool.featureIdentified.connect(self.picked)
        if canvas.mapTool() is not self.tool:
            self.previous_tool = canvas.mapTool()
        canvas.setMapTool(self.tool)
        self.status.setText("请在地图上点击滑坡；关闭窗口后恢复之前的地图工具。")

    def picked(self, feature):
        if not self.valid():
            return
        self._syncing = True
        self.layer.selectByIds([feature.id()])
        self._syncing = False
        self.selected.setChecked(True)
        self.reset_page()
        self.raise_()

    def stop_pick(self):
        canvas = self.dialog.iface.mapCanvas()
        if self.tool and canvas.mapTool() is self.tool:
            if self.previous_tool and not sip.isdeleted(self.previous_tool):
                canvas.setMapTool(self.previous_tool)
            else:
                canvas.unsetMapTool(self.tool)

    def closeEvent(self, event):
        self.stop_pick()
        self.timer.stop()
        super().closeEvent(event)

    def done(self, result):
        self.stop_pick()
        super().done(result)

    def show_fields(self, index):
        common = {'class_name', 'mj', 'perimeter_m', 'ztz', 'source_id'}
        for col, (name, _) in enumerate(self.columns):
            self.table.setColumnHidden(col, index == 0 and name not in common)

    @pyqtSlot()
    def locate(self):
        if self.valid() and self.layer.selectedFeatureCount():
            self.dialog.iface.mapCanvas().zoomToSelected(self.layer)

    def navigate(self, step):
        row = self.table.currentRow() + step
        if 0 <= row < len(self.fids):
            self.table.selectRow(row)
            self.locate()

    @pyqtSlot()
    def previous_page(self):
        self.offset = max(0, self.offset - self.PAGE_SIZE)
        self.refresh()

    @pyqtSlot()
    def next_page(self):
        if self.next.isEnabled():
            self.offset += self.PAGE_SIZE
            self.refresh()
