# -*- coding: utf-8 -*-
"""AI 编辑专用 Map Tool。

参考 TerraLab 在 QGIS 中的交互形态:
- 左键添加正样本点(在 mask 内的标注点)
- 右键添加负样本点(应排除的区域)
- 鼠标移动不触发预测,只在新增/撤销/清除点时触发回调
- 通过 QgsRubberBand 在画布上展示当前的 mask 预览

实际的 SAM 推理在 sam_worker 子进程中执行,本工具只负责采集
点提示、维护预览渲染、把点列表回调给主对话框。
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand


class AiSegmentMapTool(QgsMapTool):
    """采集正负点提示并维护 mask 预览的 map tool。"""

    def __init__(self, canvas, on_points_changed):
        super().__init__(canvas)
        self._canvas = canvas
        self._on_points_changed = on_points_changed
        self._positive_points = []
        self._negative_points = []
        self._disposed = False

        self._preview_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._preview_band.setColor(QColor(30, 30, 30, 220))
        self._preview_band.setFillColor(QColor(96, 180, 96, 95))
        self._preview_band.setWidth(1)

        self._positive_band = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._positive_band.setColor(QColor(0, 200, 0, 255))
        self._positive_band.setIconSize(10)
        self._positive_band.setIcon(QgsRubberBand.ICON_CIRCLE)

        self._negative_band = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._negative_band.setColor(QColor(220, 20, 60, 255))
        self._negative_band.setIconSize(10)
        self._negative_band.setIcon(QgsRubberBand.ICON_CROSS)

    def deactivate(self):
        if self._disposed:
            super().deactivate()
            return
        self.clear_points()
        self.clear_preview()
        super().deactivate()

    def canvasPressEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        if event.button() == Qt.LeftButton:
            self._positive_points.append(point)
            self._refresh_point_bands()
            self._emit_points_changed()
        elif event.button() == Qt.RightButton:
            self._negative_points.append(point)
            self._refresh_point_bands()
            self._emit_points_changed()

    def undo_last_point(self):
        # 撤销最后一次添加的点,无论正负
        if self._positive_points and (not self._negative_points or
                                      len(self._positive_points) >=
                                      len(self._negative_points)):
            self._positive_points.pop()
        elif self._negative_points:
            self._negative_points.pop()
        elif self._positive_points:
            self._positive_points.pop()
        self._refresh_point_bands()
        self._emit_points_changed()

    def clear_points(self):
        if not self._positive_points and not self._negative_points:
            self._refresh_point_bands()
            return
        self._positive_points = []
        self._negative_points = []
        self._refresh_point_bands()
        self._emit_points_changed()

    def has_points(self):
        return bool(self._positive_points or self._negative_points)

    def positive_points(self):
        return list(self._positive_points)

    def negative_points(self):
        return list(self._negative_points)

    def show_preview(self, geometry, layer=None):
        if self._preview_band is None:
            return
        self._preview_band.reset(QgsWkbTypes.PolygonGeometry)
        if geometry is not None and not geometry.isEmpty():
            self._preview_band.setToGeometry(geometry, layer)

    def clear_preview(self):
        if self._preview_band is None:
            return
        self._preview_band.reset(QgsWkbTypes.PolygonGeometry)

    def current_preview(self):
        if self._preview_band is None:
            return QgsGeometry()
        return QgsGeometry(self._preview_band.asGeometry())

    def _refresh_point_bands(self):
        if self._positive_band is None or self._negative_band is None:
            return
        self._positive_band.reset(QgsWkbTypes.PointGeometry)
        for point in self._positive_points:
            self._positive_band.addPoint(QgsPointXY(point))
        self._negative_band.reset(QgsWkbTypes.PointGeometry)
        for point in self._negative_points:
            self._negative_band.addPoint(QgsPointXY(point))

    def _emit_points_changed(self):
        if callable(self._on_points_changed):
            self._on_points_changed(
                list(self._positive_points),
                list(self._negative_points),
            )

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        for band in (self._preview_band, self._positive_band,
                     self._negative_band):
            if band is None:
                continue
            scene = band.scene()
            if scene is not None:
                scene.removeItem(band)
        self._preview_band = None
        self._positive_band = None
        self._negative_band = None
