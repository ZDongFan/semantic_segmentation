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
DEFAULT_FACTOR_NAMES = ["elevation", "slope", "relief", "tpi", "aspect"]
FACTOR_NAME_KEYS = ("factor_names", "dem_factor_names", "dem_channels", "channels")
DUAL_INPUT_MODES = ("dual", "dual_branch", "image_dem", "two_input", "two_inputs")
CONCAT_INPUT_MODES = ("concat", "stack", "single", "single_tensor")
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
    schema_version = int(manifest.get("schema_version", 0))
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            "不兼容的 bundle schema_version={}，当前插件仅支持 {}。请重新导出 bundle。"
            .format(schema_version, SUPPORTED_SCHEMA_VERSION))

    arch_module = _load_module(
        "lcc_bundle_arch_{}".format(abs(hash(bundle_dir))),
        os.path.join(bundle_dir, "arch.py"),
    )
    dem_module = _load_module(
        "lcc_bundle_dem_{}".format(abs(hash(bundle_dir))),
        os.path.join(bundle_dir, "dem_factors.py"),
    )
    return Bundle(
        path=bundle_dir,
        manifest=manifest,
        preprocess=_read_json(os.path.join(bundle_dir, "preprocess.json"), {}),
        postprocess=_read_json(os.path.join(bundle_dir, "postprocess.json"), {}),
        arch_module=arch_module,
        dem_module=dem_module,
    )


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


def align_dem_to_image(dem_path, image_profile):
    """把 DEM 重投影到输入影像格网，返回填补后的 DEM 和填补掩膜。"""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    height = int(image_profile["height"])
    width = int(image_profile["width"])
    dst_transform = image_profile["transform"]
    dst_crs = image_profile.get("crs")
    destination = np.full((height, width), np.nan, dtype="float32")

    with rasterio.open(dem_path) as dem:
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
    return destination, filled_mask


def compute_dem_factors(bundle, dem_array, transform):
    """调用 bundle 内 dem_factors.py 计算派生因子。"""
    if not hasattr(bundle.dem_module, "compute_factors"):
        raise AttributeError("bundle 的 dem_factors.py 缺少 compute_factors(dem_array, transform)")
    factors = bundle.dem_module.compute_factors(dem_array, transform)
    return factors


def _factor_names(*configs):
    for config in configs:
        if not config:
            continue
        for key in FACTOR_NAME_KEYS:
            names = config.get(key)
            if names:
                return [str(name) for name in names]
    return list(DEFAULT_FACTOR_NAMES)


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
    if arr.shape[0] > len(names):
        raise ValueError("DEM 因子通道数 {} 超过已声明通道名数量 {}。".format(arr.shape[0], len(names)))
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
    names = _factor_names(bundle.preprocess, factor_cfg, bundle.postprocess, bundle.manifest)
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


def _rule_config(config, name):
    rules = config.get("rules") or {}
    return rules.get(name) or config.get(name) or {}


def _postprocess_threshold(config):
    return float(config.get("threshold", config.get("prob_threshold", 0.5)))


def _min_area_m2(config):
    return float(config.get("min_area_m2", 500.0))


def _pixel_area(transform):
    try:
        return abs(transform.a * transform.e - transform.b * transform.d)
    except AttributeError:
        try:
            return abs(transform[0] * transform[4] - transform[1] * transform[3])
        except Exception:
            return 1.0


def _binary_opening(mask):
    try:
        from scipy import ndimage
        return ndimage.binary_opening(mask, structure=[[1, 1, 1]] * 3)
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


def _bbox(mask):
    import numpy as np

    ys, xs = np.where(mask)
    return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1


def _expanded_bbox(mask, pad):
    y0, y1, x0, x1 = _bbox(mask)
    height, width = mask.shape
    return max(0, y0 - pad), min(height, y1 + pad), max(0, x0 - pad), min(width, x1 + pad)


def _finite_median(values, default=None):
    import numpy as np

    values = np.asarray(values)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return default
    return float(np.median(values))


def _finite_mean(values, default=None):
    import numpy as np

    values = np.asarray(values)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return default
    return float(np.mean(values))


def _record_decision(component, decision, rule=None, threshold=None):
    component["decision"] = decision
    if rule:
        component["rule"] = rule
    if threshold is not None:
        component["threshold"] = threshold


def apply_postprocess(prob_map, factors, transform=None, filled_mask=None,
                      postprocess_config=None, output_path=None,
                      progress_callback=None):
    """按 slope、relief、TPI 规则处理概率图并生成审计摘要。"""
    import numpy as np

    config = dict(postprocess_config or {})
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
    if config.get("morph_opening", True):
        mask = _binary_opening(mask)
    post_opening_pixel_count = int(mask.sum())
    labels, count = _label_components(mask)
    pixel_area = _pixel_area(transform)
    min_area = _min_area_m2(config)
    filled_mask = np.asarray(filled_mask, dtype=bool) if filled_mask is not None else None

    output = np.zeros(prob_map.shape, dtype="uint8")
    components = []
    kept = 0
    dropped = 0

    slope_cfg = _rule_config(config, "slope")
    relief_cfg = _rule_config(config, "relief")
    tpi_cfg = _rule_config(config, "tpi")
    slope_min = float(slope_cfg.get("slope_min_deg", slope_cfg.get("min_deg", 8.0)))
    relief_min = float(relief_cfg.get("relief_min_m", relief_cfg.get("min_m", 5.0)))
    tpi_max = float(tpi_cfg.get("tpi_max_ridge", tpi_cfg.get("max_ridge", 4.0)))

    for comp_id in range(1, count + 1):
        comp_mask = labels == comp_id
        pixel_count = int(comp_mask.sum())
        area_m2 = float(pixel_count * pixel_area)
        component = {
            "comp_id": comp_id,
            "pixel_count": pixel_count,
            "area_m2": area_m2,
            "threshold": threshold,
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

        slope = factor_dict.get("slope")
        if slope_cfg.get("enabled", True) and slope is not None:
            median_slope = _finite_median(slope[comp_mask])
            component["median_slope"] = median_slope
            if median_slope is not None and median_slope < slope_min:
                _record_decision(component, "drop", "slope", slope_min)
                dropped += 1
                components.append(component)
                LOG.info(
                    "DROP comp_id=%s area_m2=%.3f rule=slope median_slope=%.3f threshold=%.3f",
                    comp_id, area_m2, median_slope, slope_min)
                continue

        relief = factor_dict.get("relief")
        if relief_cfg.get("enabled", True) and relief is not None:
            y0, y1, x0, x1 = _expanded_bbox(comp_mask, 2)
            median_relief = _finite_median(relief[y0:y1, x0:x1])
            component["median_relief"] = median_relief
            if median_relief is not None and median_relief < relief_min:
                _record_decision(component, "drop", "relief", relief_min)
                dropped += 1
                components.append(component)
                LOG.info(
                    "DROP comp_id=%s area_m2=%.3f rule=relief median_relief=%.3f threshold=%.3f",
                    comp_id, area_m2, median_relief, relief_min)
                continue

        tpi = factor_dict.get("tpi")
        if tpi_cfg.get("enabled", True) and tpi is not None:
            mean_tpi = _finite_mean(tpi[comp_mask])
            component["mean_tpi"] = mean_tpi
            if mean_tpi is not None and mean_tpi > tpi_max:
                _record_decision(component, "drop", "tpi", tpi_max)
                dropped += 1
                components.append(component)
                LOG.info(
                    "DROP comp_id=%s area_m2=%.3f rule=tpi mean_tpi=%.3f threshold=%.3f",
                    comp_id, area_m2, mean_tpi, tpi_max)
                continue

        output[comp_mask] = 1
        kept += 1
        _record_decision(component, "keep")
        components.append(component)
        LOG.info("KEEP comp_id=%s area_m2=%.3f median_slope=%s median_relief=%s mean_tpi=%s",
                 comp_id, area_m2, component.get("median_slope"),
                 component.get("median_relief"), component.get("mean_tpi"))

    summary = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "threshold": threshold,
        "prob_stats": prob_stats,
        "threshold_pixel_count": threshold_pixel_count,
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
    """???????????????? GeoTIFF?"""
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


def run_inference(params, progress_callback=None):
    """执行完整 PyTorch 推理流程。"""
    bundle = load_bundle(params["model_path"])
    device_cfg = select_device()
    if progress_callback is not None:
        progress_callback("load", 1, 1, device=device_cfg["name"])

    model = build_model(bundle, device_cfg)
    image, profile, transform, _crs = read_image(
        params["input_path"],
        preprocess_flags=params.get("preprocess_flags") or {},
        preprocess=bundle.preprocess,
    )
    if progress_callback is not None:
        progress_callback("dem", 0, 1)
    dem, filled_mask = align_dem_to_image(params["dem_path"], profile)
    raw_factors = compute_dem_factors(bundle, dem, transform)
    factors = _factors_to_dict(raw_factors, _factor_config(bundle))
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
    config = _merge_postprocess_config(
        bundle, params.get("postprocess_overrides") or {})
    label_map, summary = apply_postprocess(
        prob_map,
        factors,
        transform=transform,
        filled_mask=filled_mask,
        postprocess_config=config,
        output_path=params["output_path"],
        progress_callback=progress_callback,
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
