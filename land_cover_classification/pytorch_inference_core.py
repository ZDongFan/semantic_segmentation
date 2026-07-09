# -*- coding: utf-8 -*-
"""PyTorch bundle 推理与 DEM 规则后处理核心逻辑。

本模块不依赖 QGIS API，供独立子进程调用。所有 PyTorch、rasterio、cv2 等重依赖都
在函数内部延迟导入，避免 QGIS 主进程加载插件时受到运行环境影响。
"""

import importlib.util
import inspect
import json
import logging
import os
import tempfile
from dataclasses import dataclass


SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_CLASS_LABELS = ["background", "landslide"]
EXPECTED_FACTOR_NAMES = ["slope", "aspect_sin", "aspect_cos", "tpi", "relief"]
EXPECTED_DEM_FACTOR_METHODS = {
    "slope": "gradient",
    "aspect_sin": "aspect_sin",
    "aspect_cos": "aspect_cos",
    "tpi": "center_minus_local_mean",
    "relief": "local_max_minus_min",
}
FACTOR_NAME_KEYS = ("factor_names", "dem_factor_names", "dem_channels", "channels")
DUAL_INPUT_MODES = ("dual", "dual_branch", "image_dem", "two_input", "two_inputs")
CONCAT_INPUT_MODES = ("concat", "stack", "single", "single_tensor")
SUPPORTED_RULE_STATS = ("median", "mean", "min", "max")
SUPPORTED_RULE_OPERATORS = (">=", ">", "<=", "<")
RULE_THRESHOLD_KEYS = {
    "slope": "slope_min_deg",
    "relief": "relief_min_m",
    "tpi": "tpi_max_ridge",
}
METER_UNITS = {"m", "meter", "meters", "metre", "metres"}
LOG = logging.getLogger("LandCoverClassification.pytorch")


@dataclass
class Bundle:
    """保存一个 PyTorch bundle 的运行期元数据。"""

    path: str
    manifest: dict
    preprocess: dict
    postprocess: dict
    arch_module: object
    dem_module: object

    @property
    def labels(self):
        return class_labels_from_manifest(self.manifest)

    @property
    def landslide_class_id(self):
        value = self.manifest.get("landslide_class_id")
        if value is not None:
            return int(value)
        for idx, label in enumerate(self.labels):
            if str(label).strip().lower() == "landslide":
                return idx
        return 1 if len(self.labels) > 1 else 0


def _read_json(path, default=None):
    if not os.path.isfile(path):
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _load_module(module_name, path):
    if not os.path.isfile(path):
        raise FileNotFoundError("缺少 bundle 模块文件: {}".format(path))
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法加载模块: {}".format(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def class_labels_from_manifest(manifest):
    """从 manifest 中读取类别名，缺失时返回默认二分类标签。"""
    for key in ("class_names", "classes", "labels"):
        labels = manifest.get(key)
        if labels:
            return [str(label) for label in labels]
    attrs = manifest.get("_Attributes") or {}
    labels = attrs.get("labels")
    if labels:
        return [str(label) for label in labels]
    return list(DEFAULT_CLASS_LABELS)


def _normalize_unit(value):
    unit = str(value or "").strip().lower()
    return "m" if unit in METER_UNITS else unit


def _is_meter_unit(value):
    return _normalize_unit(value) == "m"


def _require_dict(value, path):
    if not isinstance(value, dict):
        raise ValueError("{} 必须是对象".format(path))
    return value


def _require_number(value, path, positive=False):
    if isinstance(value, bool):
        raise ValueError("{} 必须是数值".format(path))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} 必须是数值".format(path))
    if positive and number <= 0:
        raise ValueError("{} 必须大于 0".format(path))
    return number


def _module_factor_names(dem_module):
    names = getattr(dem_module, "FACTOR_NAMES", None)
    if not names:
        raise ValueError("bundle 的 dem_factors.py 必须声明 FACTOR_NAMES")
    return [str(name) for name in names]


def _validate_dem_factors_contract(config, module_factor_names=None):
    dem_factors = _require_dict(config.get("dem_factors"), "postprocess.dem_factors")
    declared_names = [str(name) for name in dem_factors.keys()]
    if module_factor_names is not None and declared_names != list(module_factor_names):
        raise ValueError(
            "postprocess.dem_factors 必须与 dem_factors.py.FACTOR_NAMES 完全一致: {} != {}".format(
                declared_names, list(module_factor_names)))
    if declared_names != EXPECTED_FACTOR_NAMES:
        raise ValueError("postprocess.dem_factors 必须按 v3 因子顺序声明: {}".format(
            ", ".join(EXPECTED_FACTOR_NAMES)))

    for name in EXPECTED_FACTOR_NAMES:
        factor_cfg = _require_dict(dem_factors.get(name), "postprocess.dem_factors.{}".format(name))
        expected_method = EXPECTED_DEM_FACTOR_METHODS[name]
        if factor_cfg.get("method") != expected_method:
            raise ValueError("postprocess.dem_factors.{}.method 必须是 {}".format(name, expected_method))
        if name in ("tpi", "relief"):
            if factor_cfg.get("scale_mode") != "meters":
                raise ValueError("postprocess.dem_factors.{}.scale_mode 必须是 meters".format(name))
            _require_number(factor_cfg.get("window_m"),
                            "postprocess.dem_factors.{}.window_m".format(name), positive=True)
            if not _is_meter_unit(factor_cfg.get("unit")):
                raise ValueError("postprocess.dem_factors.{}.unit 必须是 m".format(name))
    return dem_factors


def _validate_training_data_contract(config):
    training = _require_dict(config.get("training_data"), "postprocess.training_data")
    _require_number(training.get("image_resolution_m"),
                    "postprocess.training_data.image_resolution_m", positive=True)
    _require_number(training.get("dem_resolution_m"),
                    "postprocess.training_data.dem_resolution_m", positive=True)
    if not _is_meter_unit(training.get("crs_unit")):
        raise ValueError("postprocess.training_data.crs_unit 必须为米制")
    return training


def _validate_rules_contract(config, dem_factors):
    rules = _require_dict(config.get("rules"), "postprocess.rules")
    if not rules:
        raise ValueError("postprocess.rules 不能为空")
    rule_order = config.get("rule_order")
    if rule_order is not None:
        if not isinstance(rule_order, list) or not all(isinstance(item, str) for item in rule_order):
            raise ValueError("postprocess.rule_order 必须是规则名数组")
        if len(set(rule_order)) != len(rule_order):
            raise ValueError("postprocess.rule_order 不能包含重复规则")
        if set(rule_order) != set(rules.keys()):
            raise ValueError("postprocess.rule_order 必须覆盖 rules 中的全部规则")

    for name, rule_cfg in rules.items():
        if name not in RULE_THRESHOLD_KEYS:
            raise ValueError("不支持的规则: {}".format(name))
        rule_cfg = _require_dict(rule_cfg, "postprocess.rules.{}".format(name))
        for key in ("enabled", "factor", "stat", "operator"):
            if key not in rule_cfg:
                raise ValueError("postprocess.rules.{}.{} 缺失".format(name, key))
        if not isinstance(rule_cfg.get("enabled"), bool):
            raise ValueError("postprocess.rules.{}.enabled 必须是布尔值".format(name))
        factor_name = str(rule_cfg.get("factor"))
        if factor_name not in dem_factors:
            raise ValueError("postprocess.rules.{}.factor 不存在于 dem_factors: {}".format(name, factor_name))
        if str(rule_cfg.get("stat")) not in SUPPORTED_RULE_STATS:
            raise ValueError("postprocess.rules.{}.stat 不支持: {}".format(name, rule_cfg.get("stat")))
        if str(rule_cfg.get("operator")) not in SUPPORTED_RULE_OPERATORS:
            raise ValueError("postprocess.rules.{}.operator 不支持: {}".format(name, rule_cfg.get("operator")))
        _require_number(rule_cfg.get(RULE_THRESHOLD_KEYS[name]),
                        "postprocess.rules.{}.{}".format(name, RULE_THRESHOLD_KEYS[name]))
    return rules


def validate_postprocess_contract(config, module_factor_names=None):
    """校验 v3 显式米制 DEM 因子与规则契约。"""
    if not isinstance(config, dict):
        raise ValueError("postprocess 配置必须是对象")
    dem_factors = _validate_dem_factors_contract(config, module_factor_names)
    _validate_training_data_contract(config)
    _validate_rules_contract(config, dem_factors)
    return True


def validate_bundle_contract(bundle):
    """校验 bundle 是否满足当前推理侧强制契约。"""
    validate_postprocess_contract(bundle.postprocess, _module_factor_names(bundle.dem_module))
    return True

def is_georeferenced(image_path):
    """判断影像是否带有有效地理参考。"""
    try:
        from osgeo import gdal
    except Exception:
        gdal = None
    if gdal is not None:
        ds = gdal.Open(image_path)
        if ds is None:
            return False
        gt = ds.GetGeoTransform()
        proj = ds.GetProjection()
        ds = None
        if gt and gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
            return True
        return bool(proj)

    try:
        import rasterio
        with rasterio.open(image_path) as src:
            return bool(src.crs) or not src.transform.is_identity
    except Exception:
        return False


def load_bundle(bundle_dir):
    """加载并校验一个 PyTorch 推理 bundle。"""
    bundle_dir = os.path.abspath(bundle_dir)
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    manifest = _read_json(manifest_path)
    if not manifest:
        raise FileNotFoundError("缺少 manifest.json: {}".format(manifest_path))

    if manifest.get("framework") != "pytorch":
        raise ValueError("不支持的 framework: {}".format(
            manifest.get("framework")))
    if manifest.get("task") != "semantic_segmentation":
        raise ValueError("不支持的 task: {}".format(manifest.get("task")))
    arch_module = _load_module(
        "lcc_bundle_arch_{}".format(abs(hash(bundle_dir))),
        os.path.join(bundle_dir, "arch.py"),
    )
    dem_module = _load_module(
        "lcc_bundle_dem_{}".format(abs(hash(bundle_dir))),
        os.path.join(bundle_dir, "dem_factors.py"),
    )
    bundle = Bundle(
        path=bundle_dir,
        manifest=manifest,
        preprocess=_read_json(os.path.join(bundle_dir, "preprocess.json"), {}),
        postprocess=_read_json(os.path.join(bundle_dir, "postprocess.json"), {}),
        arch_module=arch_module,
        dem_module=dem_module,
    )
    validate_bundle_contract(bundle)
    return bundle


def select_device():
    """选择推理设备，并返回与设备匹配的滑窗参数。"""
    import torch

    if torch.cuda.is_available():
        return {
            "device": torch.device("cuda"),
            "name": "cuda",
            "use_amp": True,
            "tile_size": 1024,
            "overlap": 128,
            "batch_size": 1,
        }
    return {
        "device": torch.device("cpu"),
        "name": "cpu",
        "use_amp": False,
        "tile_size": 512,
        "overlap": 64,
        "batch_size": 1,
    }


def _model_config(bundle):
    return (
        bundle.manifest.get("model_config")
        or bundle.manifest.get("model")
        or bundle.manifest
    )


def _weights_path(bundle):
    value = bundle.manifest.get("weights") or "weights.pt"
    return value if os.path.isabs(value) else os.path.join(bundle.path, value)


def _state_dict_from_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict):
        return checkpoint
    for key in ("state_dict", "model_state_dict", "model"):
        value = checkpoint.get(key)
        if isinstance(value, dict):
            return value
    return checkpoint


def _strip_module_prefix(state_dict):
    if not isinstance(state_dict, dict):
        return state_dict
    if not any(str(key).startswith("module.") for key in state_dict):
        return state_dict
    return {
        str(key)[7:] if str(key).startswith("module.") else key: value
        for key, value in state_dict.items()
    }


def build_model(bundle, device_cfg):
    """调用 bundle 的 arch.py 构建模型并加载 weights.pt。"""
    import torch

    if not hasattr(bundle.arch_module, "build_model"):
        raise AttributeError("bundle 的 arch.py 缺少 build_model(cfg)")
    model = bundle.arch_module.build_model(_model_config(bundle))
    checkpoint = torch.load(_weights_path(bundle), map_location=device_cfg["device"])
    state_dict = _strip_module_prefix(_state_dict_from_checkpoint(checkpoint))
    strict = bool(bundle.manifest.get("strict_load", True))
    model.load_state_dict(state_dict, strict=strict)
    model.to(device_cfg["device"])
    model.eval()
    return model


def _apply_array_preprocess(image, flags):
    enabled = flags or {}
    if not any(enabled.values()):
        return image
    import cv2
    import numpy as np

    arr = image
    channels_first = arr.ndim == 3 and arr.shape[0] <= 16
    if channels_first:
        arr = np.moveaxis(arr, 0, -1)
    arr = np.ascontiguousarray(arr)

    if enabled.get("clahe"):
        op = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8))
        if arr.ndim == 2:
            arr = op.apply(arr.astype("uint8"))
        else:
            arr = cv2.merge([op.apply(c.astype("uint8")) for c in cv2.split(arr)])
    if enabled.get("sharpen"):
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.int8)
        arr = cv2.filter2D(arr, -1, kernel)
    if enabled.get("median"):
        arr = cv2.medianBlur(arr, 3)
    if enabled.get("gaussian"):
        arr = cv2.GaussianBlur(arr, (3, 3), 0, 0)

    if channels_first and arr.ndim == 3:
        arr = np.moveaxis(arr, -1, 0)
    return arr


def _normalize_image(image, preprocess):
    import numpy as np

    arr = image.astype("float32", copy=False)
    scale = preprocess.get("scale")
    if scale is None:
        scale = 255.0 if arr.size and np.nanmax(arr) > 2.0 else 1.0
    if scale:
        arr = arr / float(scale)

    mean = preprocess.get("mean", preprocess.get("image_mean"))
    std = preprocess.get("std", preprocess.get("image_std"))
    if mean is not None and std is not None:
        mean_arr = np.asarray(mean, dtype="float32").reshape(-1, 1, 1)
        std_arr = np.asarray(std, dtype="float32").reshape(-1, 1, 1)
        std_arr = np.where(std_arr == 0, 1.0, std_arr)
        if mean_arr.shape[0] == arr.shape[0]:
            arr = (arr - mean_arr) / std_arr
    return arr


def read_image(image_path, preprocess_flags=None, preprocess=None):
    """读取输入影像，返回数组、profile、transform 与 CRS。"""
    import rasterio

    with rasterio.open(image_path) as src:
        image = src.read()
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs
    image = _apply_array_preprocess(image, preprocess_flags or {})
    image = _normalize_image(image, preprocess or {})
    return image, profile, transform, crs


def _pixel_size_xy(transform):
    if transform is None:
        return None
    try:
        return abs(float(transform.a)), abs(float(transform.e))
    except AttributeError:
        try:
            return abs(float(transform[0])), abs(float(transform[4]))
        except Exception:
            return None


def _resolution_from_transform(transform):
    size = _pixel_size_xy(transform)
    if not size:
        return None
    return float((size[0] + size[1]) / 2.0)


def _crs_unit(crs):
    if crs is None:
        return None
    for attr in ("linear_units", "linear_unit_name"):
        value = getattr(crs, attr, None)
        if value:
            return _normalize_unit(value)
    try:
        units = crs.to_dict().get("units")
        if units:
            return _normalize_unit(units)
    except Exception:
        pass
    try:
        pyproj_crs = crs.to_pyproj()
        axis_info = getattr(pyproj_crs, "axis_info", None) or []
        if axis_info:
            return _normalize_unit(getattr(axis_info[0], "unit_name", None))
    except Exception:
        pass
    return None


def _require_runtime_meter_crs(dem_factors, crs_unit):
    for name, factor_cfg in dem_factors.items():
        if factor_cfg.get("scale_mode") == "meters" and not _is_meter_unit(crs_unit):
            raise ValueError(
                "postprocess.dem_factors.{} 使用 meters 尺度，运行时影像 CRS 单位必须为米制，当前为 {}".format(
                    name, crs_unit or "unknown"))


def align_dem_to_image(dem_path, image_profile):
    """把 DEM 重投影到输入影像格网，返回 DEM、填补掩膜与源 DEM 元数据。"""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    height = int(image_profile["height"])
    width = int(image_profile["width"])
    dst_transform = image_profile["transform"]
    dst_crs = image_profile.get("crs")
    destination = np.full((height, width), np.nan, dtype="float32")
    dem_info = {
        "path": dem_path,
        "resolution": None,
        "crs_unit": None,
    }

    with rasterio.open(dem_path) as dem:
        dem_info["crs_unit"] = _crs_unit(dem.crs)
        if dem.res and dem_info["crs_unit"] == "m":
            dem_info["resolution"] = float((abs(float(dem.res[0])) + abs(float(dem.res[1]))) / 2.0)
        source = dem.read(1).astype("float32")
        src_nodata = dem.nodata
        if src_nodata is not None:
            source[source == src_nodata] = np.nan
        reproject(
            source=source,
            destination=destination,
            src_transform=dem.transform,
            src_crs=dem.crs,
            src_nodata=np.nan,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

    filled_mask = ~np.isfinite(destination)
    if filled_mask.all():
        raise ValueError("DEM 与输入影像没有有效重叠区域。")
    fill_value = float(np.nanmean(destination))
    destination[filled_mask] = fill_value
    return destination, filled_mask, dem_info


def compute_dem_factors(bundle, dem_array, transform, postprocess_config, crs_unit):
    """按显式 DEM 因子契约调用 bundle 内 dem_factors.py。"""
    if not hasattr(bundle.dem_module, "compute_factors"):
        raise AttributeError("bundle 的 dem_factors.py 缺少 compute_factors(dem_array, transform, dem_factors=..., crs_unit=...)")
    validate_postprocess_contract(postprocess_config, _module_factor_names(bundle.dem_module))
    dem_factors = postprocess_config["dem_factors"]
    _require_runtime_meter_crs(dem_factors, crs_unit)
    return bundle.dem_module.compute_factors(
        dem_array,
        transform,
        dem_factors=dem_factors,
        crs_unit=crs_unit,
    )


def _factor_names(*configs):
    for config in configs:
        if not config:
            continue
        dem_factors = config.get("dem_factors") if isinstance(config, dict) else None
        if isinstance(dem_factors, dict) and dem_factors:
            return [str(name) for name in dem_factors.keys()]
        for key in FACTOR_NAME_KEYS:
            names = config.get(key)
            if names:
                return [str(name) for name in names]
    raise ValueError("DEM 因子缺少显式通道名声明")


def _factor_config(bundle, *configs):
    merged = {}
    module_names = getattr(bundle.dem_module, "FACTOR_NAMES", None)
    if module_names:
        merged["factor_names"] = [str(name) for name in module_names]
    for config in (bundle.manifest, bundle.preprocess, bundle.postprocess) + configs:
        if isinstance(config, dict):
            merged.update(config)
    return merged


def _factors_to_dict(factors, config):
    import numpy as np

    if isinstance(factors, dict):
        return {str(key): np.asarray(value) for key, value in factors.items()}
    arr = np.asarray(factors)
    if arr.ndim != 3:
        raise ValueError("DEM 因子必须是 dict 或 [C,H,W] 数组。")
    names = _factor_names(config)
    if arr.shape[0] != len(names):
        raise ValueError("DEM 因子通道数 {} 必须等于显式声明通道数 {}。".format(arr.shape[0], len(names)))
    result = {}
    for idx in range(arr.shape[0]):
        result[names[idx]] = arr[idx]
    return result

def _window_starts(length, tile_size, overlap):
    stride = max(1, tile_size - overlap)
    starts = list(range(0, max(1, length - tile_size + 1), stride))
    last = max(0, length - tile_size)
    if not starts or starts[-1] != last:
        starts.append(last)
    return starts


def _hann_weight(height, width):
    import numpy as np

    if height <= 1 or width <= 1:
        return np.ones((height, width), dtype="float32")
    wy = np.hanning(height).astype("float32")
    wx = np.hanning(width).astype("float32")
    weight = np.outer(wy, wx)
    weight = np.maximum(weight, 1e-3)
    return weight.astype("float32")


def _pad_tile(tile, tile_size):
    import numpy as np

    _, height, width = tile.shape
    pad_h = max(0, tile_size - height)
    pad_w = max(0, tile_size - width)
    if pad_h == 0 and pad_w == 0:
        return tile
    return np.pad(tile, ((0, 0), (0, pad_h), (0, pad_w)), mode="edge")


def _extract_logits(output):
    if isinstance(output, dict):
        for key in ("logits", "out", "pred", "prediction"):
            if key in output:
                return output[key]
    if isinstance(output, (list, tuple)):
        return output[0]
    return output


def _declared_input_mode(bundle):
    for config in (bundle.manifest, bundle.preprocess):
        value = config.get("input_mode") or config.get("model_input")
        if value:
            mode = str(value).strip().lower()
            if mode in DUAL_INPUT_MODES:
                return "dual"
            if mode in CONCAT_INPUT_MODES:
                return "concat"
    return None


def _model_expects_dem(model):
    try:
        signature = inspect.signature(model.forward)
    except (TypeError, ValueError):
        return False
    required = [
        param for param in signature.parameters.values()
        if param.default is inspect.Parameter.empty
        and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
    ]
    return len(required) >= 2


def _use_dual_inputs(model, bundle):
    mode = _declared_input_mode(bundle)
    if mode == "dual":
        return True
    if mode == "concat":
        return False
    return _model_expects_dem(model)


def _dem_channel_names(bundle, factor_cfg):
    model_cfg = _model_config(bundle)
    dem_channels = int(model_cfg.get("dem_in_channels", 0) or 0)
    names = _factor_names(factor_cfg, bundle.postprocess, bundle.preprocess, bundle.manifest)
    if dem_channels > len(names):
        raise ValueError("模型声明需要 {} 个 DEM 通道，但 bundle 只声明了 {} 个。".format(dem_channels, len(names)))
    if dem_channels > 0:
        return names[:dem_channels]
    return names


def _factor_arrays(factors, bundle, factor_cfg):
    import numpy as np

    factor_dict = _factors_to_dict(factors, _factor_config(bundle, factor_cfg))
    names = _dem_channel_names(bundle, factor_cfg)
    arrays = []
    missing = []
    for name in names:
        if name in factor_dict:
            arrays.append(np.asarray(factor_dict[name], dtype="float32"))
        else:
            missing.append(name)
    if missing:
        raise ValueError("DEM 因子缺少模型需要的通道: {}".format(", ".join(missing)))
    return arrays


def _normalize_dem_stack(dem_stack, preprocess):
    import numpy as np

    arr = dem_stack.astype("float32", copy=False)
    mean = preprocess.get("dem_mean") or preprocess.get("dem_factor_mean")
    std = preprocess.get("dem_std") or preprocess.get("dem_factor_std")
    if mean is not None and std is not None:
        mean_arr = np.asarray(mean, dtype="float32").reshape(-1, 1, 1)
        std_arr = np.asarray(std, dtype="float32").reshape(-1, 1, 1)
        std_arr = np.where(std_arr == 0, 1.0, std_arr)
        if mean_arr.shape[0] == arr.shape[0]:
            arr = (arr - mean_arr) / std_arr
    return arr


def _apply_active_dem_channels(dem_stack, names, preprocess):
    import numpy as np

    active = preprocess.get("active_dem_channels") or preprocess.get("enabled_dem_channels")
    if not active:
        return dem_stack

    active_names = {str(name) for name in active}
    arr = np.asarray(dem_stack, dtype="float32").copy()
    for idx, name in enumerate(names):
        if str(name) not in active_names:
            arr[idx] = 0.0
    return arr


def _build_model_inputs(image_tile, dem_tile, use_dual_inputs, device):
    import numpy as np
    import torch

    image_tensor = torch.from_numpy(image_tile[None, ...]).to(device)
    if use_dual_inputs:
        dem_tensor = torch.from_numpy(dem_tile[None, ...]).to(device)
        return image_tensor, dem_tensor
    stacked = np.concatenate([image_tile, dem_tile], axis=0)
    return (torch.from_numpy(stacked[None, ...]).to(device),)


def _forward_model(model, inputs):
    return _extract_logits(model(*inputs))


def sliding_window_predict(model, image, factors, bundle, device_cfg,
                           progress_callback=None):
    """执行滑窗推理并返回 landslide 概率图。"""
    import numpy as np
    import torch

    factor_cfg = _factor_config(bundle)
    use_dual_inputs = _use_dual_inputs(model, bundle)
    use_dem_factors = bool(bundle.manifest.get("use_dem_factors", True)) or use_dual_inputs
    factor_arrays = _factor_arrays(factors, bundle, factor_cfg) if use_dem_factors else []
    if not factor_arrays and not use_dual_inputs:
        dem_stack = np.zeros((0, image.shape[1], image.shape[2]), dtype="float32")
    elif factor_arrays:
        dem_stack = np.stack(factor_arrays, axis=0).astype("float32")
        dem_stack = _normalize_dem_stack(dem_stack, bundle.preprocess)
        dem_stack = _apply_active_dem_channels(
            dem_stack, _dem_channel_names(bundle, factor_cfg), bundle.preprocess)
    else:
        raise ValueError("当前模型需要 DEM 输入，但 bundle 没有提供 DEM 因子通道。")

    image = image.astype("float32", copy=False)
    _, height, width = image.shape
    if dem_stack.shape[1:] != (height, width):
        raise ValueError("DEM 因子尺寸 {} 与影像尺寸 {} 不一致。".format(dem_stack.shape[1:], (height, width)))

    tile_size = int(device_cfg["tile_size"])
    overlap = int(device_cfg["overlap"])
    y_starts = _window_starts(height, tile_size, overlap)
    x_starts = _window_starts(width, tile_size, overlap)
    total = max(1, len(y_starts) * len(x_starts))
    prob_sum = np.zeros((height, width), dtype="float32")
    weight_sum = np.zeros((height, width), dtype="float32")
    class_id = int(bundle.landslide_class_id)

    done = 0
    with torch.no_grad():
        for y0 in y_starts:
            for x0 in x_starts:
                y1 = min(height, y0 + tile_size)
                x1 = min(width, x0 + tile_size)
                image_tile = _pad_tile(image[:, y0:y1, x0:x1], tile_size)
                dem_tile = _pad_tile(dem_stack[:, y0:y1, x0:x1], tile_size)
                inputs = _build_model_inputs(
                    image_tile, dem_tile, use_dual_inputs, device_cfg["device"])
                if device_cfg["use_amp"]:
                    with torch.cuda.amp.autocast():
                        logits = _forward_model(model, inputs)
                else:
                    logits = _forward_model(model, inputs)
                logits = logits[:, :, :image_tile.shape[1], :image_tile.shape[2]]
                logits = logits[:, :, :y1 - y0, :x1 - x0]
                probs = torch.softmax(logits, dim=1)
                if class_id >= probs.shape[1]:
                    raise ValueError("landslide_class_id 超出模型输出通道数。")
                prob = probs[0, class_id].detach().cpu().numpy().astype("float32")
                weight = _hann_weight(y1 - y0, x1 - x0)
                prob_sum[y0:y1, x0:x1] += prob * weight
                weight_sum[y0:y1, x0:x1] += weight
                done += 1
                if progress_callback is not None:
                    progress_callback("predict", done, total)

    weight_sum[weight_sum == 0] = 1.0
    return prob_sum / weight_sum


def _postprocess_threshold(config):
    return float(config.get("threshold", config.get("prob_threshold", 0.5)))


def _min_area_m2(config):
    return float(config.get("min_area_m2", 500.0))


def _config_int(config, keys, default):
    for key in keys:
        if key in config:
            try:
                return max(1, int(config.get(key)))
            except (TypeError, ValueError):
                return default
    return default


def _pixel_area(transform):
    try:
        return abs(transform.a * transform.e - transform.b * transform.d)
    except AttributeError:
        try:
            return abs(transform[0] * transform[4] - transform[1] * transform[3])
        except Exception:
            return 1.0


def _morph_structure(size):
    import numpy as np

    return np.ones((max(1, int(size)), max(1, int(size))), dtype=bool)


def _binary_opening(mask, size=3):
    try:
        from scipy import ndimage
        return ndimage.binary_opening(mask, structure=_morph_structure(size))
    except Exception:
        return mask


def _binary_closing(mask, size=3):
    try:
        from scipy import ndimage
        return ndimage.binary_closing(mask, structure=_morph_structure(size))
    except Exception:
        return mask


def _smooth_binary_mask(mask, size=3):
    try:
        from scipy import ndimage
        structure = _morph_structure(size)
        smoothed = ndimage.binary_closing(mask, structure=structure)
        return ndimage.binary_opening(smoothed, structure=structure)
    except Exception:
        return mask


def _fill_holes(mask, max_hole_area_m2=0, pixel_area=1.0):
    try:
        from scipy import ndimage
        filled = ndimage.binary_fill_holes(mask)
        holes = filled & ~mask
        if not holes.any() or not max_hole_area_m2:
            return filled

        labels, count = _label_components(holes)
        output = mask.copy()
        max_pixels = max(1, int(float(max_hole_area_m2) / max(pixel_area, 1e-9)))
        for comp_id in range(1, count + 1):
            hole_mask = labels == comp_id
            if int(hole_mask.sum()) <= max_pixels:
                output[hole_mask] = True
        return output
    except Exception:
        return mask


def _label_components(mask):
    try:
        from scipy import ndimage
        structure = [[1, 1, 1], [1, 1, 1], [1, 1, 1]]
        return ndimage.label(mask, structure=structure)
    except Exception:
        return _label_components_fallback(mask)


def _label_components_fallback(mask):
    import numpy as np

    labels = np.zeros(mask.shape, dtype="int32")
    height, width = mask.shape
    comp_id = 0
    for y in range(height):
        for x in range(width):
            if not mask[y, x] or labels[y, x] != 0:
                continue
            comp_id += 1
            stack = [(y, x)]
            labels[y, x] = comp_id
            while stack:
                cy, cx = stack.pop()
                for ny in range(max(0, cy - 1), min(height, cy + 2)):
                    for nx in range(max(0, cx - 1), min(width, cx + 2)):
                        if mask[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = comp_id
                            stack.append((ny, nx))
    return labels, comp_id


def _finite_stat(values, stat):
    import numpy as np

    values = np.asarray(values)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    if stat == "median":
        return float(np.median(values))
    if stat == "mean":
        return float(np.mean(values))
    if stat == "min":
        return float(np.min(values))
    if stat == "max":
        return float(np.max(values))
    raise ValueError("不支持的规则统计量: {}".format(stat))


def _compare_rule(observed, operator, threshold):
    if observed is None:
        return False
    if operator == ">=":
        return observed >= threshold
    if operator == ">":
        return observed > threshold
    if operator == "<=":
        return observed <= threshold
    if operator == "<":
        return observed < threshold
    raise ValueError("不支持的规则比较符: {}".format(operator))


def _ordered_rule_items(config):
    rules = config["rules"]
    rule_order = config.get("rule_order")
    names = list(rule_order) if rule_order else list(rules.keys())
    return [(name, rules[name]) for name in names]


def _rule_threshold_value(name, rule_cfg):
    return _require_number(rule_cfg.get(RULE_THRESHOLD_KEYS[name]),
                           "postprocess.rules.{}.{}".format(name, RULE_THRESHOLD_KEYS[name]))


def _rules_contract_summary(config):
    return {
        name: {
            "enabled": bool(rule_cfg["enabled"]),
            "factor": str(rule_cfg["factor"]),
            "stat": str(rule_cfg["stat"]),
            "operator": str(rule_cfg["operator"]),
            "threshold_key": RULE_THRESHOLD_KEYS[name],
            "threshold_value": float(_rule_threshold_value(name, rule_cfg)),
        }
        for name, rule_cfg in config["rules"].items()
    }


def _record_decision(component, decision, rule=None, threshold=None):
    component["decision"] = decision
    if rule:
        component["rule"] = rule
    if threshold is not None:
        component["threshold"] = threshold


def _drop_by_rule(component, evaluation):
    component.update({
        "decision": "drop",
        "rule": evaluation["rule"],
        "factor": evaluation["factor"],
        "stat": evaluation["stat"],
        "operator": evaluation["operator"],
        "threshold_value": evaluation["threshold_value"],
        "observed_value": evaluation["observed_value"],
    })


def apply_postprocess(prob_map, factors, transform=None, filled_mask=None,
                      postprocess_config=None, output_path=None,
                      progress_callback=None, runtime_metadata=None):
    """按显式规则契约处理概率图并生成审计摘要。"""
    import numpy as np

    config = dict(postprocess_config or {})
    validate_postprocess_contract(config)
    factor_dict = _factors_to_dict(factors, config)
    threshold = _postprocess_threshold(config)
    prob_arr = np.asarray(prob_map, dtype="float32")
    finite_prob = prob_arr[np.isfinite(prob_arr)]
    if finite_prob.size:
        prob_stats = {
            "min": float(np.min(finite_prob)),
            "max": float(np.max(finite_prob)),
            "mean": float(np.mean(finite_prob)),
            "p50": float(np.percentile(finite_prob, 50)),
            "p90": float(np.percentile(finite_prob, 90)),
            "p95": float(np.percentile(finite_prob, 95)),
            "p99": float(np.percentile(finite_prob, 99)),
        }
    else:
        prob_stats = {
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
        }
    mask = prob_arr >= threshold
    threshold_pixel_count = int(mask.sum())
    pixel_area = _pixel_area(transform)
    if config.get("morph_closing", False):
        close_size = _config_int(config, ("morph_close_size", "closing_size"), 5)
        mask = _binary_closing(mask, close_size)
    post_closing_pixel_count = int(mask.sum())
    if config.get("fill_holes", False):
        max_hole_area = float(config.get("max_hole_area_m2", 0) or 0)
        mask = _fill_holes(mask, max_hole_area, pixel_area)
    post_fill_holes_pixel_count = int(mask.sum())
    if config.get("smooth_boundary", False):
        smooth_size = _config_int(config, ("smooth_size", "smooth_boundary_size"), 3)
        mask = _smooth_binary_mask(mask, smooth_size)
    post_smooth_pixel_count = int(mask.sum())
    if config.get("morph_opening", True):
        open_size = _config_int(config, ("morph_open_size", "opening_size"), 3)
        mask = _binary_opening(mask, open_size)
    post_opening_pixel_count = int(mask.sum())
    labels, count = _label_components(mask)
    min_area = _min_area_m2(config)
    filled_mask = np.asarray(filled_mask, dtype=bool) if filled_mask is not None else None

    output = np.zeros(prob_map.shape, dtype="uint8")
    components = []
    kept = 0
    dropped = 0
    ordered_rules = _ordered_rule_items(config)

    for comp_id in range(1, count + 1):
        comp_mask = labels == comp_id
        pixel_count = int(comp_mask.sum())
        area_m2 = float(pixel_count * pixel_area)
        component = {
            "comp_id": comp_id,
            "pixel_count": pixel_count,
            "area_m2": area_m2,
            "threshold": threshold,
            "rule_evaluations": [],
        }

        if area_m2 < min_area:
            _record_decision(component, "drop", "min_area", min_area)
            dropped += 1
            components.append(component)
            LOG.info("DROP comp_id=%s area_m2=%.3f rule=min_area threshold=%.3f",
                     comp_id, area_m2, min_area)
            continue

        if filled_mask is not None:
            fill_fraction = float(filled_mask[comp_mask].mean())
            component["dem_fill_fraction"] = fill_fraction
            if fill_fraction > 0.5:
                component["rules_skipped"] = "dem_fill_fraction_gt_0.5"
                output[comp_mask] = 1
                kept += 1
                _record_decision(component, "keep")
                components.append(component)
                continue

        dropped_by_rule = False
        for rule_name, rule_cfg in ordered_rules:
            if not rule_cfg["enabled"]:
                continue
            factor_name = str(rule_cfg["factor"])
            if factor_name not in factor_dict:
                raise ValueError("规则 {} 需要的 DEM 因子不存在: {}".format(rule_name, factor_name))
            stat = str(rule_cfg["stat"])
            operator = str(rule_cfg["operator"])
            threshold_value = float(_rule_threshold_value(rule_name, rule_cfg))
            observed_value = _finite_stat(factor_dict[factor_name][comp_mask], stat)
            passed = _compare_rule(observed_value, operator, threshold_value)
            evaluation = {
                "rule": rule_name,
                "factor": factor_name,
                "stat": stat,
                "operator": operator,
                "threshold_value": threshold_value,
                "observed_value": observed_value,
                "passed": bool(passed),
            }
            component["rule_evaluations"].append(evaluation)
            if not passed:
                _drop_by_rule(component, evaluation)
                dropped += 1
                components.append(component)
                dropped_by_rule = True
                LOG.info(
                    "DROP comp_id=%s area_m2=%.3f rule=%s factor=%s stat=%s observed=%s operator=%s threshold=%.3f",
                    comp_id, area_m2, rule_name, factor_name, stat,
                    observed_value, operator, threshold_value)
                break

        if dropped_by_rule:
            continue

        output[comp_mask] = 1
        kept += 1
        _record_decision(component, "keep")
        components.append(component)
        LOG.info("KEEP comp_id=%s area_m2=%.3f", comp_id, area_m2)

    runtime_metadata = dict(runtime_metadata or {})
    summary = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "contract": "explicit_dem_factors_v3",
        "dem_factors": config["dem_factors"],
        "training_data": config["training_data"],
        "runtime_resolution": runtime_metadata.get("runtime_resolution", {}),
        "resolution_warnings": runtime_metadata.get("resolution_warnings", []),
        "rules": _rules_contract_summary(config),
        "rule_order": [name for name, _rule_cfg in ordered_rules],
        "threshold": threshold,
        "prob_stats": prob_stats,
        "threshold_pixel_count": threshold_pixel_count,
        "post_closing_pixel_count": post_closing_pixel_count,
        "post_fill_holes_pixel_count": post_fill_holes_pixel_count,
        "post_smooth_pixel_count": post_smooth_pixel_count,
        "post_opening_pixel_count": post_opening_pixel_count,
        "min_area_m2": min_area,
        "component_count": count,
        "kept": kept,
        "dropped": dropped,
        "components": components,
    }
    if output_path:
        postprocess_path = output_path + ".postprocess.json"
        with open(postprocess_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        summary["postprocess_path"] = postprocess_path
    if progress_callback is not None:
        progress_callback("postprocess", kept, max(1, kept + dropped), kept=kept, dropped=dropped)
    return output, summary

def _tiff_block_size(length, preferred=256):
    if length < 16:
        return None
    size = min(int(preferred), int(length))
    size = (size // 16) * 16
    return max(16, size)


def write_class_geotiff(output_path, label_map, image_profile):
    """写出单波段类别 GeoTIFF。"""
    import rasterio

    profile = image_profile.copy()
    width = int(profile.get("width", label_map.shape[1]))
    height = int(profile.get("height", label_map.shape[0]))
    block_x = _tiff_block_size(width)
    block_y = _tiff_block_size(height)
    profile.update(
        driver="GTiff",
        count=1,
        dtype="uint8",
        nodata=0,
        compress="lzw",
    )
    for key in ("blockxsize", "blockysize", "interleave"):
        profile.pop(key, None)
    if block_x is not None and block_y is not None:
        profile.update(tiled=True, blockxsize=block_x, blockysize=block_y)
    else:
        profile.update(tiled=False)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(label_map.astype("uint8"), 1)


def _merge_postprocess_config(bundle, overrides):
    config = dict(bundle.postprocess or {})
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            merged = dict(config[key])
            merged.update(value)
            config[key] = merged
        else:
            config[key] = value
    return config


def _resolution_warning(kind, runtime_resolution, training_resolution):
    if runtime_resolution is None:
        return None
    training_resolution = float(training_resolution)
    if training_resolution <= 0:
        return None
    relative_difference = abs(float(runtime_resolution) - training_resolution) / training_resolution
    if relative_difference <= 0.25:
        return None
    return {
        "kind": kind,
        "runtime_resolution_m": float(runtime_resolution),
        "training_resolution_m": training_resolution,
        "relative_difference": float(relative_difference),
    }


def _runtime_metadata(config, image_transform, image_crs_unit, dem_info):
    training = config["training_data"]
    image_resolution = _resolution_from_transform(image_transform) if _is_meter_unit(image_crs_unit) else None
    dem_resolution = dem_info.get("resolution") if dem_info else None
    warnings = []
    image_warning = _resolution_warning(
        "image_resolution_mismatch",
        image_resolution,
        training["image_resolution_m"],
    )
    if image_warning:
        warnings.append(image_warning)
    dem_warning = _resolution_warning(
        "dem_resolution_mismatch",
        dem_resolution,
        training["dem_resolution_m"],
    )
    if dem_warning:
        warnings.append(dem_warning)
    return {
        "runtime_resolution": {
            "image_resolution_m": image_resolution,
            "image_crs_unit": image_crs_unit,
            "dem_resolution_m": dem_resolution,
            "dem_crs_unit": (dem_info or {}).get("crs_unit"),
        },
        "resolution_warnings": warnings,
    }


def run_inference(params, progress_callback=None):
    """执行完整 PyTorch 推理流程。"""
    bundle = load_bundle(params["model_path"])
    config = _merge_postprocess_config(
        bundle, params.get("postprocess_overrides") or {})
    validate_postprocess_contract(config, _module_factor_names(bundle.dem_module))
    device_cfg = select_device()
    if progress_callback is not None:
        progress_callback("load", 1, 1, device=device_cfg["name"])

    model = build_model(bundle, device_cfg)
    image, profile, transform, crs = read_image(
        params["input_path"],
        preprocess_flags=params.get("preprocess_flags") or {},
        preprocess=bundle.preprocess,
    )
    runtime_crs_unit = _crs_unit(crs)
    if progress_callback is not None:
        progress_callback("dem", 0, 1)
    dem, filled_mask, dem_info = align_dem_to_image(params["dem_path"], profile)
    raw_factors = compute_dem_factors(bundle, dem, transform, config, runtime_crs_unit)
    factors = _factors_to_dict(raw_factors, _factor_config(bundle, config))
    runtime_metadata = _runtime_metadata(config, transform, runtime_crs_unit, dem_info)
    if progress_callback is not None:
        progress_callback("dem", 1, 1)

    prob_map = sliding_window_predict(
        model,
        image,
        factors,
        bundle,
        device_cfg,
        progress_callback=progress_callback,
    )
    label_map, summary = apply_postprocess(
        prob_map,
        factors,
        transform=transform,
        filled_mask=filled_mask,
        postprocess_config=config,
        output_path=params["output_path"],
        progress_callback=progress_callback,
        runtime_metadata=runtime_metadata,
    )
    write_class_geotiff(params["output_path"], label_map, profile)
    return {
        "label_path": params["output_path"],
        "postprocess_path": summary.get("postprocess_path"),
        "device": device_cfg["name"],
        "kept": summary.get("kept", 0),
        "dropped": summary.get("dropped", 0),
    }

def run_inference_from_file(params_path, progress_callback=None):
    """从 JSON 参数文件读取配置并执行推理。"""
    with open(params_path, "r", encoding="utf-8-sig") as handle:
        params = json.load(handle)
    with tempfile.TemporaryDirectory(prefix="lcc_pytorch_"):
        return run_inference(params, progress_callback=progress_callback)
