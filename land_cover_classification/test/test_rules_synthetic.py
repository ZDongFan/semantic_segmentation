# -*- coding: utf-8 -*-
"""DEM 后处理契约与规则执行器的合成用例。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np

from land_cover_classification.pytorch_inference_core import (
    apply_postprocess,
    load_bundle,
    validate_postprocess_contract,
)


class _Transform:
    a = 1.0
    b = 0.0
    d = 0.0
    e = -1.0


def _dem_factors_contract():
    return {
        "slope": {"method": "gradient", "unit": "degree"},
        "aspect_sin": {"method": "aspect_sin", "unit": "ratio"},
        "aspect_cos": {"method": "aspect_cos", "unit": "ratio"},
        "tpi": {
            "method": "center_minus_local_mean",
            "scale_mode": "meters",
            "window_m": 50.0,
            "unit": "m",
        },
        "relief": {
            "method": "local_max_minus_min",
            "scale_mode": "meters",
            "window_m": 50.0,
            "unit": "m",
        },
    }


def _rules(enabled=False):
    return {
        "slope": {
            "enabled": enabled,
            "slope_min_deg": 8.0,
            "factor": "slope",
            "stat": "median",
            "operator": ">=",
        },
        "relief": {
            "enabled": enabled,
            "relief_min_m": 5.0,
            "factor": "relief",
            "stat": "median",
            "operator": ">=",
        },
        "tpi": {
            "enabled": enabled,
            "tpi_max_ridge": 4.0,
            "factor": "tpi",
            "stat": "mean",
            "operator": "<=",
        },
    }


def _config(enabled=False):
    return {
        "threshold": 0.5,
        "dem_factors": _dem_factors_contract(),
        "training_data": {
            "image_resolution_m": 1.0,
            "dem_resolution_m": 1.0,
            "crs_unit": "m",
        },
        "morph_opening": False,
        "min_area_m2": 1,
        "rules": _rules(enabled),
        "rule_order": ["slope", "relief", "tpi"],
    }


def _factors(shape, slope=20.0, relief=10.0, tpi=0.0):
    return {
        "slope": np.full(shape, slope, dtype="float32"),
        "aspect_sin": np.zeros(shape, dtype="float32"),
        "aspect_cos": np.ones(shape, dtype="float32"),
        "tpi": np.full(shape, tpi, dtype="float32"),
        "relief": np.full(shape, relief, dtype="float32"),
    }


class DemContractTest(unittest.TestCase):

    def test_v3_current_structure_passes_contract(self):
        model_dir = Path("D:/模型/landslide_mitb2_dem_v3")
        if not model_dir.is_dir():
            self.skipTest("本机未提供 v3 bundle")
        bundle = load_bundle(str(model_dir))
        self.assertEqual(
            ["slope", "aspect_sin", "aspect_cos", "tpi", "relief"],
            list(bundle.postprocess["dem_factors"].keys()),
        )

    def test_missing_meter_window_contract_fields_fail(self):
        for key in ("scale_mode", "window_m", "unit"):
            config = _config()
            del config["dem_factors"]["relief"][key]
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    validate_postprocess_contract(config)

    def test_factor_names_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "manifest.json").write_text(json.dumps({
                "schema_version": 999,
                "framework": "pytorch",
                "task": "semantic_segmentation",
            }), encoding="utf-8")
            (root / "preprocess.json").write_text("{}", encoding="utf-8")
            (root / "postprocess.json").write_text(
                json.dumps(_config(), ensure_ascii=False), encoding="utf-8")
            (root / "arch.py").write_text(
                "def build_model(cfg):\n    return None\n", encoding="utf-8")
            (root / "dem_factors.py").write_text(
                "FACTOR_NAMES = ['slope', 'relief']\n"
                "def compute_factors(*args, **kwargs):\n    return None\n",
                encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bundle(str(root))

    def test_legacy_rule_shape_fails(self):
        config = _config()
        config["rules"]["relief"] = {"enabled": True, "relief_min_m": 5.0}
        with self.assertRaises(ValueError):
            validate_postprocess_contract(config)

    def test_required_semantic_thresholds_must_be_numeric(self):
        cases = (
            ("slope", "slope_min_deg"),
            ("relief", "relief_min_m"),
            ("tpi", "tpi_max_ridge"),
        )
        for rule_name, threshold_key in cases:
            config = _config()
            config["rules"][rule_name][threshold_key] = "bad"
            with self.subTest(rule=rule_name):
                with self.assertRaises(ValueError):
                    validate_postprocess_contract(config)


class DemRuleSyntheticTest(unittest.TestCase):

    def test_morphology_merge_fill_and_area_filter(self):
        prob = np.zeros((80, 80), dtype="float32")
        prob[10:30, 10:30] = 0.9
        prob[10:30, 32:52] = 0.9
        prob[14:18, 14:18] = 0.0
        prob[70:72, 70:72] = 0.9

        config = _config(enabled=False)
        config.update({
            "morph_closing": True,
            "morph_close_size": 3,
            "fill_holes": True,
            "max_hole_area_m2": 100,
            "smooth_boundary": False,
            "morph_opening": False,
            "min_area_m2": 10,
        })

        label, summary = apply_postprocess(
            prob,
            _factors(prob.shape),
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
        self.assertEqual("explicit_dem_factors_v3", summary["contract"])

    def _run_single_component_rule(self, rule_name, factor_values):
        prob = np.zeros((20, 20), dtype="float32")
        prob[4:14, 4:14] = 0.9
        config = _config(enabled=False)
        config["rules"][rule_name]["enabled"] = True
        label, summary = apply_postprocess(
            prob,
            _factors(prob.shape, **factor_values),
            transform=_Transform(),
            postprocess_config=config,
        )
        return label, summary

    def test_rule_keep_and_drop_cases(self):
        cases = (
            ("slope", {"slope": 9.0}, {"slope": 2.0}),
            ("relief", {"relief": 6.0}, {"relief": 2.0}),
            ("tpi", {"tpi": 3.0}, {"tpi": 9.0}),
        )
        for rule_name, keep_values, drop_values in cases:
            with self.subTest(rule=rule_name, decision="keep"):
                label, summary = self._run_single_component_rule(rule_name, keep_values)
                self.assertEqual(1, summary["kept"])
                self.assertEqual(0, summary["dropped"])
                self.assertEqual(1, int(label.max()))
            with self.subTest(rule=rule_name, decision="drop"):
                label, summary = self._run_single_component_rule(rule_name, drop_values)
                self.assertEqual(0, summary["kept"])
                self.assertEqual(1, summary["dropped"])
                self.assertEqual(0, int(label.max()))
                component = summary["components"][0]
                self.assertEqual(rule_name, component["rule"])
                self.assertIn("observed_value", component)
                self.assertIn("threshold_value", component)

    def test_disabled_rule_is_not_executed_but_bad_structure_fails(self):
        prob = np.zeros((20, 20), dtype="float32")
        prob[4:14, 4:14] = 0.9
        config = _config(enabled=False)
        config["rules"]["slope"].pop("factor")
        with self.assertRaises(ValueError):
            apply_postprocess(
                prob,
                _factors(prob.shape, slope=0.0),
                transform=_Transform(),
                postprocess_config=config,
            )

    def test_rule_order_controls_first_recorded_drop(self):
        prob = np.zeros((20, 20), dtype="float32")
        prob[4:14, 4:14] = 0.9
        config = _config(enabled=False)
        config["rules"]["slope"]["enabled"] = True
        config["rules"]["tpi"]["enabled"] = True
        config["rule_order"] = ["tpi", "slope", "relief"]

        _label, summary = apply_postprocess(
            prob,
            _factors(prob.shape, slope=1.0, tpi=9.0),
            transform=_Transform(),
            postprocess_config=config,
        )

        component = summary["components"][0]
        self.assertEqual("tpi", component["rule"])
        self.assertEqual("tpi", component["factor"])
        self.assertEqual(["tpi"], [item["rule"] for item in component["rule_evaluations"]])


if __name__ == "__main__":
    unittest.main()
