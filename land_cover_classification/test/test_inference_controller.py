# -*- coding: utf-8 -*-
"""推理 bundle landslide 契约测试。"""

import unittest

from land_cover_classification.inference_controller import (
    BundleContractError,
    InferenceController,
    landslide_class_id,
)


class LandslideBundleContractTest(unittest.TestCase):
    """验证工作区只能映射到唯一的 landslide 类别。"""

    def test_resolves_case_insensitive_landslide(self):
        """大小写差异不影响类别映射。"""
        self.assertEqual(landslide_class_id({
            "class_names": ["background", "LandSlide"],
        }), 1)

    def test_rejects_missing_landslide(self):
        """缺少 landslide 时必须在启动前失败。"""
        with self.assertRaises(BundleContractError):
            landslide_class_id({"class_names": ["background", "road"]})

    def test_rejects_conflicting_configured_index(self):
        """显式索引与名称索引冲突时必须失败。"""
        with self.assertRaises(BundleContractError):
            landslide_class_id({
                "class_names": ["background", "landslide"],
                "landslide_class_id": 0,
            })

    def test_controller_keeps_run_metadata(self):
        """控制器保存经过校验的批次信息。"""
        controller = InferenceController()
        self.assertEqual(controller.prepare({
            "class_names": ["background", "landslide"],
        }, "run-1"), 1)
        self.assertEqual(controller.run_id, "run-1")


if __name__ == "__main__":
    unittest.main()
