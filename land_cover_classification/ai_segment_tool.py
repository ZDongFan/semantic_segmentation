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
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand


class AiSegmentMapTool(QgsMapTool):
    """采集正负点提示并维护 mask 预览的 map tool。"""

    def __init__(self, canvas, on_points_changed):
        super().__init__(canvas)
        self._canvas = canvas
        self._on_points_changed = on_points_changed
        self._positive_points = []
        self._negative_points = []
        self._point_history = []
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
        super().deactivate()

    def canvasPressEvent(self, event):
        if self._disposed:
            return
        point = self.toMapCoordinates(event.pos())
        if event.button() == Qt.LeftButton:
            self._positive_points.append(point)
            self._point_history.append('positive')
            self._refresh_point_bands()
            self._emit_points_changed()
        elif event.button() == Qt.RightButton:
            self._negative_points.append(point)
            self._point_history.append('negative')
            self._refresh_point_bands()
            self._emit_points_changed()

    def undo_last_point(self):
        if self._disposed:
            return
        # 按真实点击顺序撤销，避免用正负点数量猜测导致撤错点。
        while self._point_history:
            point_type = self._point_history.pop()
            if point_type == 'positive' and self._positive_points:
                self._positive_points.pop()
                break
            if point_type == 'negative' and self._negative_points:
                self._negative_points.pop()
                break
        self._refresh_point_bands()
        self._emit_points_changed()

    def clear_points(self):
        if self._disposed:
            return
        if not self._positive_points and not self._negative_points:
            self._refresh_point_bands()
            return
        self._positive_points = []
        self._negative_points = []
        self._point_history = []
        self._refresh_point_bands()
        self._emit_points_changed()

    def has_points(self):
        return bool(self._positive_points or self._negative_points)

    def positive_points(self):
        return list(self._positive_points)

    def negative_points(self):
        return list(self._negative_points)

    def show_preview(self, geometry, reference=None):
        """显示预览几何，参考对象只能是矢量层或坐标系。"""
        if self._preview_band is None:
            return
        self._preview_band.reset(QgsWkbTypes.PolygonGeometry)
        if geometry is None or geometry.isEmpty():
            return
        if not isinstance(
                reference, (QgsVectorLayer, QgsCoordinateReferenceSystem)):
            reference = self._canvas.mapSettings().destinationCrs()
        self._preview_band.setToGeometry(geometry, reference)

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
        if self._disposed:
            return
        callback = self._on_points_changed
        if not callable(callback):
            return
        try:
            callback(
                list(self._positive_points),
                list(self._negative_points),
            )
        except RuntimeError as exc:
            # 插件重载后旧对话框可能已被 Qt 销毁，不能继续保留其回调。
            if "has been deleted" not in str(exc):
                raise
            self.dispose()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._on_points_changed = None
        canvas = self._canvas
        try:
            if canvas is not None and canvas.mapTool() is self:
                canvas.unsetMapTool(self)
        except RuntimeError:
            pass
        for band in (self._preview_band, self._positive_band,
                     self._negative_band):
            if band is None:
                continue
            try:
                scene = band.scene()
                if scene is not None:
                    scene.removeItem(band)
            except RuntimeError:
                continue
        self._preview_band = None
        self._positive_band = None
        self._negative_band = None
        self._canvas = None
