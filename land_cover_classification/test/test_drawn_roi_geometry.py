# -*- coding: utf-8 -*-
"""绘制范围融合几何回归测试。"""

import unittest

from qgis.core import QgsGeometry

from land_cover_classification.land_cover_classification_dialog import (
    LandCoverClassificationDialog,
)


class DrawnRoiGeometryTest(unittest.TestCase):
    """验证绘制范围融合不会退化为外包矩形。"""

    def test_drawn_polygon_uses_real_geometry(self):
        dialog = object.__new__(LandCoverClassificationDialog)
        dialog._active_inference_roi = {
            "mode": "drawn_polygon",
            "bounds": [0.0, 0.0, 10.0, 10.0],
            "crs_wkt": "",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [0.0, 0.0],
                ]],
            },
        }

        geometry = dialog._active_roi_geometry()

        self.assertIsInstance(geometry, QgsGeometry)
        self.assertTrue(geometry.isGeosValid())
        self.assertAlmostEqual(geometry.area(), 50.0)
        self.assertNotAlmostEqual(
            geometry.area(), geometry.boundingBox().width()
            * geometry.boundingBox().height())


if __name__ == "__main__":
    unittest.main()