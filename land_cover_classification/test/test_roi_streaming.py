# -*- coding: utf-8 -*-
"""画布范围 ROI 流式推理边界测试。"""

import os
import tempfile
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window

from land_cover_classification.pytorch_streaming import (
    _expand_window,
    _initialize_roi_outputs,
    _roi_pixel_window,
    _windows_in_window,
)


class _RasterShape:
    """提供 ROI 像素窗口换算所需的最小栅格接口。"""

    width = 100
    height = 80
    transform = from_origin(0, 80, 1, 1)


class RoiWindowTest(unittest.TestCase):
    """验证 canonical ROI 的像素裁剪与 halo 行为。"""

    def test_roi_rounds_outward_and_clips_to_image(self):
        """ROI 应向外取整到完整像素并裁剪到影像边界。"""
        window = _roi_pixel_window(_RasterShape(), {
            "mode": "canvas_intersection",
            "bounds": [20.2, 29.1, 50.7, 60.4],
            "crs_wkt": "test",
        })
        self.assertEqual(
            (window.col_off, window.row_off, window.width, window.height),
            (20, 19, 31, 32),
        )

        clipped = _roi_pixel_window(_RasterShape(), {
            "mode": "canvas_intersection",
            "bounds": [-10, 70, 10, 90],
            "crs_wkt": "test",
        })
        self.assertEqual(
            (clipped.col_off, clipped.row_off,
             clipped.width, clipped.height),
            (0, 0, 10, 10),
        )

    def test_roi_without_pixel_intersection_fails(self):
        """完全位于影像外的 ROI 不得退化为全图推理。"""
        with self.assertRaises(ValueError):
            _roi_pixel_window(_RasterShape(), {
                "mode": "canvas_intersection",
                "bounds": [110, 10, 120, 20],
                "crs_wkt": "test",
            })

    def test_core_windows_stay_in_roi_while_halo_can_leave_it(self):
        """核心窗口只覆盖 ROI，扩展读取可越过 ROI 边界。"""
        roi_window = Window(20, 10, 45, 30)
        cores = list(_windows_in_window(roi_window, 16))
        self.assertEqual(len(cores), 6)
        self.assertEqual(cores[0], Window(20, 10, 16, 16))
        self.assertEqual(cores[-1], Window(52, 26, 13, 14))

        expanded = _expand_window(cores[0], 5, 100, 80)
        self.assertEqual(expanded, Window(15, 5, 26, 26))


class RoiOutputInitializationTest(unittest.TestCase):
    """验证 ROI 外输出符合全尺寸 GeoTIFF 契约。"""

    def test_probability_is_nodata_and_masks_are_invalid(self):
        """未执行模型计算的区域必须显式写为 nodata 和无效。"""
        with tempfile.TemporaryDirectory(prefix="lcc_roi_test_") as directory:
            probability_path = os.path.join(directory, "probability.tif")
            filled_path = os.path.join(directory, "filled.tif")
            valid_path = os.path.join(directory, "valid.tif")
            base = {
                "driver": "GTiff",
                "width": 23,
                "height": 17,
                "count": 1,
                "transform": from_origin(0, 17, 1, 1),
                "crs": "EPSG:3857",
                "tiled": True,
                "blockxsize": 16,
                "blockysize": 16,
            }
            probability_profile = dict(
                base, dtype="float32", nodata=np.nan)
            mask_profile = dict(base, dtype="uint8", nodata=0)
            with rasterio.open(
                    probability_path, "w", **probability_profile) as probability,                     rasterio.open(filled_path, "w", **mask_profile) as filled,                     rasterio.open(valid_path, "w", **mask_profile) as valid:
                _initialize_roi_outputs(
                    probability, filled, valid, base["width"], base["height"])

            with rasterio.open(probability_path) as probability,                     rasterio.open(filled_path) as filled,                     rasterio.open(valid_path) as valid:
                self.assertEqual((probability.width, probability.height), (23, 17))
                self.assertTrue(np.isnan(probability.read(1)).all())
                self.assertFalse(filled.read(1).any())
                self.assertFalse(valid.read(1).any())


if __name__ == "__main__":
    unittest.main()