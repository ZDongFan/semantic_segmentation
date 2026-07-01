# -*- coding: utf-8 -*-
"""测试 PyTorch 推理输入适配逻辑。"""

import importlib.util
import tempfile
import unittest

import numpy as np
import torch

from pathlib import Path

from land_cover_classification.pytorch_inference_core import (
    sliding_window_predict,
    write_class_geotiff,
)


DEM_CHANNELS = ["slope", "aspect_sin", "aspect_cos", "tpi", "relief"]


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
        arch_path = Path(
            "land_cover_classification/models/semantic_segmentation/"
            "landslide_mitb2_dem_v1/arch.py")
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
        self.assertEqual(3, module._run_decoder(VarargsDecoder(), features))
        self.assertEqual(3, module._run_decoder(ListDecoder(), features))

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
            with rasterio.open(output) as src:
                self.assertEqual((512, 512), (src.width, src.height))
                self.assertEqual(1, src.count)
                block_y, block_x = src.block_shapes[0]
                self.assertEqual(0, block_x % 16)
                self.assertEqual(0, block_y % 16)


if __name__ == "__main__":
    unittest.main()
