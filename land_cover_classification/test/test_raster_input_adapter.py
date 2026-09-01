# -*- coding: utf-8 -*-
"""测试 ENVI、ERDAS 输入定位和波段契约。"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import rasterio
from rasterio.transform import from_origin

from land_cover_classification.pytorch_inference_core import (
    resolve_image_bands,
    resolve_sam_rgb_bands,
)
from land_cover_classification.raster_input_adapter import (
    RasterInputError,
    _runtime_probe_payload,
    resolve_source_path,
    source_fingerprint,
)


class RasterInputAdapterTest(unittest.TestCase):

    def _write_envi(self, path, count=3):
        profile = {
            "driver": "ENVI",
            "height": 7,
            "width": 9,
            "count": count,
            "dtype": "uint16",
            "transform": from_origin(100, 200, 2, 2),
            "crs": "EPSG:32650",
        }
        with rasterio.open(path, "w", **profile) as dst:
            for index in range(1, count + 1):
                dst.write(np.full((7, 9), index, dtype="uint16"), index)

    def test_ige_resolves_same_stem_img_and_fingerprint_tracks_hdr(self):
        with tempfile.TemporaryDirectory(prefix="lcc_ige_") as tmp:
            root = Path(tmp)
            image = root / "中文 scene.img"
            self._write_envi(str(image))
            ige = root / "中文 scene.ige"
            ige.write_text("ERDAS companion", encoding="ascii")
            self.assertEqual(
                str(image.resolve()),
                resolve_source_path(str(ige)))
            first = source_fingerprint(str(ige))
            hdr = root / "中文 scene.hdr"
            hdr.write_text(hdr.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertNotEqual(first, source_fingerprint(str(ige)))

    def test_ige_missing_or_ambiguous_main_file_is_explicit(self):
        with tempfile.TemporaryDirectory(prefix="lcc_ige_") as tmp:
            root = Path(tmp)
            ige = root / "scene.ige"
            ige.write_text("", encoding="ascii")
            with self.assertRaisesRegex(RasterInputError, "未找到"):
                resolve_source_path(str(ige))
            if os.name != "nt":
                (root / "Scene.IMG").write_bytes(b"x")
                (root / "scene.IMG").write_bytes(b"y")
                with self.assertRaisesRegex(RasterInputError, "多个"):
                    resolve_source_path(str(ige))

    def test_runtime_probe_reads_finite_window_and_reports_driver(self):
        with tempfile.TemporaryDirectory(prefix="lcc_probe_") as tmp:
            image = Path(tmp) / "scene.dat"
            self._write_envi(str(image), count=4)
            payload = _runtime_probe_payload(str(image))
            self.assertTrue(payload["ok"])
            self.assertEqual("ENVI", payload["driver"])
            self.assertEqual([3, 7, 9], payload["sample_shape"])

    def test_explicit_image_and_sam_bands_validate_against_dataset(self):
        bundle = SimpleNamespace(
            preprocess={
                "image_bands": [3, 1, 2],
                "image_mean": [0.1, 0.2, 0.3],
                "image_std": [1, 1, 1],
            },
            manifest={},
        )
        self.assertEqual([3, 1, 2], resolve_image_bands(bundle, 4))
        bundle.preprocess["image_bands"] = [1, 1]
        with self.assertRaisesRegex(ValueError, "重复"):
            resolve_image_bands(bundle, 4)
        bundle.preprocess["image_bands"] = [1, 2]
        with self.assertRaisesRegex(ValueError, "长度"):
            resolve_image_bands(bundle, 4)
        self.assertEqual(
            [1, 2, 3],
            resolve_sam_rgb_bands(
                {"sam_rgb_bands": [1, 2, 3]}, 4, []))
        with self.assertRaisesRegex(ValueError, "恰好"):
            resolve_sam_rgb_bands({"sam_rgb_bands": [1, 2]}, 4, [])


if __name__ == "__main__":
    unittest.main()
