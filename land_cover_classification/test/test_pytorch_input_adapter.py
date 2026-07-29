# -*- coding: utf-8 -*-
"""测试 PyTorch 推理输入适配逻辑。"""

import importlib.util
import os
import tempfile
import unittest

import numpy as np
import torch

from pathlib import Path

from land_cover_classification.pytorch_inference_core import (
    sliding_window_predict,
    write_class_geotiff,
)
from land_cover_classification.model_scan import scan as scan_models


DEM_CHANNELS = ["slope", "aspect_sin", "aspect_cos", "tpi", "relief"]
ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = ROOT / "land_cover_classification" / "models" / "semantic_segmentation"


def _decoder_arch_path():
    candidates = []
    external = os.environ.get("LCC_TEST_BUNDLE")
    if external:
        candidates.append(Path(external).expanduser().resolve())
    candidates.extend(Path(entry["path"]) for entry in scan_models(str(MODEL_ROOT)))
    for bundle_path in candidates:
        arch_path = bundle_path / "arch.py"
        if not arch_path.is_file():
            continue
        try:
            source = arch_path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        if "def _call_decoder(" in source:
            return arch_path
    raise unittest.SkipTest("当前项目没有提供包含 _call_decoder 的 PyTorch bundle")


class _Bundle:
    manifest = {
        "class_names": ["background", "landslide"],
        "dem_in_channels": 5,
    }
    preprocess = {"dem_channels": DEM_CHANNELS}
    postprocess = {}
    dem_module = None

    @property
    def landslide_class_id(self):
        return 1


class _DualModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def forward(self, image, dem):
        self.calls += 1
        assert image.shape[1] == 3
        assert dem.shape[1] == 5
        logits = torch.zeros((image.shape[0], 2, image.shape[2], image.shape[3]), device=image.device)
        logits[:, 1, :, :] = 1.0
        return logits


class _ConcatModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def forward(self, tensor):
        self.calls += 1
        assert tensor.shape[1] == 8
        logits = torch.zeros((tensor.shape[0], 2, tensor.shape[2], tensor.shape[3]), device=tensor.device)
        logits[:, 1, :, :] = 1.0
        return logits


class PytorchInputAdapterTest(unittest.TestCase):

    def _image(self):
        return np.zeros((3, 20, 18), dtype="float32")

    def _factors(self):
        return np.stack([
            np.full((20, 18), idx, dtype="float32")
            for idx, _name in enumerate(DEM_CHANNELS)
        ])

    def _device(self):
        return {
            "device": torch.device("cpu"),
            "tile_size": 16,
            "overlap": 4,
            "use_amp": False,
        }

    def test_dual_branch_model_receives_dem_argument(self):
        model = _DualModel()
        prob = sliding_window_predict(model, self._image(), self._factors(), _Bundle(), self._device())
        self.assertEqual((20, 18), prob.shape)
        self.assertGreater(model.calls, 0)

    def test_single_input_model_receives_concatenated_tensor(self):
        model = _ConcatModel()
        bundle = _Bundle()
        bundle.manifest = dict(bundle.manifest, input_mode="concat")
        prob = sliding_window_predict(model, self._image(), self._factors(), bundle, self._device())
        self.assertEqual((20, 18), prob.shape)
        self.assertGreater(model.calls, 0)

    def test_bundle_decoder_adapter_supports_both_smp_conventions(self):
        arch_path = _decoder_arch_path()
        spec = importlib.util.spec_from_file_location("lcc_test_bundle_arch", arch_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class VarargsDecoder:
            def forward(self, *features):
                return len(features)

            def __call__(self, *features):
                return self.forward(*features)

        class ListDecoder:
            def forward(self, features):
                return len(features)

            def __call__(self, features):
                return self.forward(features)

        features = [object(), object(), object()]
        self.assertEqual(3, module._call_decoder(VarargsDecoder(), features))
        self.assertEqual(3, module._call_decoder(ListDecoder(), features))

    def test_write_geotiff_sanitizes_invalid_tile_profile(self):
        import rasterio
        from rasterio.transform import from_origin

        label = np.zeros((512, 512), dtype="uint8")
        profile = {
            "driver": "GTiff",
            "height": 512,
            "width": 512,
            "count": 3,
            "dtype": "uint8",
            "transform": from_origin(0, 0, 1, 1),
            "crs": "EPSG:32643",
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 1,
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = tmp_dir + "/label.tif"
            write_class_geotiff(output, label, profile)
            with open(output, "rb") as handle:
                self.assertIn(
                    handle.read(4),
                    (b"II+\x00", b"MM\x00+"),
                    "生产 GeoTIFF 必须使用 BigTIFF 头",
                )
            with rasterio.open(output) as src:
                self.assertEqual((512, 512), (src.width, src.height))
                self.assertEqual(1, src.count)
                block_y, block_x = src.block_shapes[0]
                self.assertEqual(0, block_x % 16)
                self.assertEqual(0, block_y % 16)


if __name__ == "__main__":
    unittest.main()
