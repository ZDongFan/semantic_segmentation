# -*- coding: utf-8 -*-
"""DEM 后处理规则的合成用例。"""

import unittest

import numpy as np

from land_cover_classification.pytorch_inference_core import apply_postprocess


class _Transform:
    a = 1.0
    b = 0.0
    d = 0.0
    e = -1.0


class DemRuleSyntheticTest(unittest.TestCase):

    def test_morphology_merge_fill_and_area_filter(self):
        prob = np.zeros((80, 80), dtype="float32")
        prob[10:30, 10:30] = 0.9
        prob[10:30, 32:52] = 0.9
        prob[14:18, 14:18] = 0.0
        prob[70:72, 70:72] = 0.9

        factors = {
            "slope": np.full_like(prob, 25.0),
            "relief": np.full_like(prob, 20.0),
            "tpi": np.zeros_like(prob),
        }
        config = {
            "threshold": 0.5,
            "morph_closing": True,
            "morph_close_size": 3,
            "fill_holes": True,
            "max_hole_area_m2": 100,
            "smooth_boundary": False,
            "morph_opening": False,
            "min_area_m2": 10,
            "rules": {
                "slope": {"enabled": False},
                "relief": {"enabled": False},
                "tpi": {"enabled": False},
            },
        }

        label, summary = apply_postprocess(
            prob,
            factors,
            transform=_Transform(),
            postprocess_config=config,
        )

        self.assertEqual(1, int(label[20, 31]))
        self.assertEqual(1, int(label[15, 15]))
        self.assertEqual(0, int(label[70, 70]))
        self.assertEqual(1, summary["kept"])
        self.assertEqual(1, summary["dropped"])
        self.assertGreater(summary["post_fill_holes_pixel_count"],
                           summary["post_closing_pixel_count"])

    def test_slope_relief_tpi_rules(self):
        prob = np.zeros((1024, 1024), dtype="float32")
        slope = np.full_like(prob, 25.0)
        relief = np.full_like(prob, 20.0)
        tpi = np.full_like(prob, -2.0)

        prob[50:100, 50:450] = 0.9
        slope[50:100, 50:450] = 2.0

        prob[150:350, 150:350] = 0.9
        slope[150:350, 150:350] = 30.0
        tpi[150:350, 150:350] = 10.0

        prob[420:720, 420:820] = 0.9

        prob[800:900, 800:900] = 0.9
        slope[800:900, 800:900] = 12.0
        relief[798:902, 798:902] = 2.0

        factors = {
            "slope": slope,
            "relief": relief,
            "tpi": tpi,
        }
        config = {
            "threshold": 0.5,
            "morph_opening": False,
            "min_area_m2": 1,
            "rules": {
                "slope": {"enabled": True, "slope_min_deg": 8.0},
                "relief": {"enabled": True, "relief_min_m": 5.0},
                "tpi": {"enabled": True, "tpi_max_ridge": 4.0},
            },
        }

        label, summary = apply_postprocess(
            prob,
            factors,
            transform=_Transform(),
            postprocess_config=config,
        )

        rules = {item["rule"] for item in summary["components"]
                 if item["decision"] == "drop"}
        self.assertEqual({"slope", "tpi", "relief"}, rules)
        self.assertEqual(1, summary["kept"])
        self.assertEqual(3, summary["dropped"])
        self.assertEqual(1, int(label[500:650, 500:650].max()))
        self.assertEqual(0, int(label[60:90, 60:440].max()))
        self.assertEqual(0, int(label[180:320, 180:320].max()))
        self.assertEqual(0, int(label[820:880, 820:880].max()))


if __name__ == "__main__":
    unittest.main()
