# -*- coding: utf-8 -*-
"""闭合曲线推理范围地图工具。"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand


class InferenceRoiMapTool(QgsMapTool):
    """在画布 CRS 中采集点选多边形。"""

    def __init__(self, canvas, on_finished=None, on_cancelled=None):
        super().__init__(canvas)
        self._canvas = canvas
        self._on_finished = on_finished
        self._on_cancelled = on_cancelled
        self._points = []
        self._disposed = False
        self._working_band = QgsRubberBand(
            canvas, QgsWkbTypes.PolygonGeometry)
        self._working_band.setColor(QColor(30, 100, 220, 230))
        self._working_band.setFillColor(QColor(30, 100, 220, 55))
        self._working_band.setWidth(2)
        self._working_band.setLineStyle(Qt.DashLine)

    def _refresh_working_band(self):
        if self._working_band is None:
            return
        self._working_band.reset(QgsWkbTypes.PolygonGeometry)
        if len(self._points) < 2:
            return
        points = list(self._points)
        if len(points) >= 3:
            points.append(points[0])
        self._working_band.setToGeometry(
            QgsGeometry.fromPolygonXY([points]),
            self._canvas.mapSettings().destinationCrs())

    def canvasPressEvent(self, event):
        if self._disposed:
            return
        if event.button() == Qt.LeftButton:
            self._points.append(self.toMapCoordinates(event.pos()))
            self._refresh_working_band()
        elif event.button() == Qt.RightButton:
            self.finish()

    def canvasDoubleClickEvent(self, event):
        if not self._disposed and event.button() == Qt.LeftButton:
            self.finish()

    def keyPressEvent(self, event):
        if self._disposed:
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()
        elif (event.key() == Qt.Key_Backspace
              or (event.key() == Qt.Key_Z
                  and event.modifiers() & Qt.ControlModifier)):
            if self._points:
                self._points.pop()
                self._refresh_working_band()
        elif event.key() == Qt.Key_Escape:
            self.cancel()

    def _closed_geometry(self):
        if len(self._points) < 3:
            return QgsGeometry()
        points = list(self._points)
        if points[0] != points[-1]:
            points.append(points[0])
        return QgsGeometry.fromPolygonXY([points])

    def finish(self):
        if self._disposed:
            return
        geometry = self._closed_geometry()
        accepted = False
        if not geometry.isEmpty() and callable(self._on_finished):
            accepted = self._on_finished(geometry) is not False
        if not accepted:
            self.show_invalid(geometry)

    def show_invalid(self, geometry=None):
        """将本次非法范围短暂标红，由对话框决定何时结束工具。"""
        if self._working_band is None:
            return
        self._working_band.setColor(QColor(220, 30, 50, 240))
        self._working_band.setFillColor(QColor(220, 30, 50, 65))
        if geometry is not None and not geometry.isEmpty():
            self._working_band.setToGeometry(
                geometry, self._canvas.mapSettings().destinationCrs())

    def cancel(self):
        if not self._disposed and callable(self._on_cancelled):
            self._on_cancelled()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._on_finished = None
        self._on_cancelled = None
        canvas = self._canvas
        try:
            if canvas is not None and canvas.mapTool() is self:
                canvas.unsetMapTool(self)
        except RuntimeError:
            pass
        try:
            if (self._working_band is not None
                    and self._working_band.scene() is not None):
                self._working_band.scene().removeItem(self._working_band)
        except RuntimeError:
            pass
        self._working_band = None
        self._canvas = None