# -*- coding: utf-8 -*-
"""面向超大栅格的 PyTorch 流式推理与后处理。"""

import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass


DEFAULT_BLOCK_SIZE = 512
PROBABILITY_HISTOGRAM_BINS = 10000
POSTPROCESS_PROGRESS_TOTAL = 1000000


class _PostprocessProgress:
    """把后处理各窗口 pass 汇总到统一、单调的 88%–98% 协议区间。"""

    PHASE_LABELS = {
        "threshold": "阈值化",
        "closing": "形态学闭运算",
        "fill_holes": "填洞",
        "smooth_boundary": "边界平滑",
        "opening": "形态学开运算",
        "connected_components": "连通域归并",
        "dem_rules": "DEM 规则统计",
        "component_decisions": "组件规则判定",
        "write_result": "写出类别栅格",
        "audit": "写出后处理审计",
    }

    def __init__(self, callback, config):
        self.callback = callback
        phases = [("threshold", 1.0)]
        if config.get("morph_closing", False):
            phases.append(("closing", 2.0))
        if config.get("fill_holes", False):
            phases.append(("fill_holes", 4.0))
        if config.get("smooth_boundary", False):
            phases.append(("smooth_boundary", 2.0))
        if config.get("morph_opening", True):
            phases.append(("opening", 2.0))
        phases.extend([
            ("connected_components", 2.0),
            ("dem_rules", 1.0),
            ("component_decisions", 0.25),
            ("write_result", 1.0),
            ("audit", 0.25),
        ])
        self._weights = dict(phases)
        self._offsets = {}
        offset = 0.0
        for name, weight in phases:
            self._offsets[name] = offset
            offset += weight
        self._total_weight = offset
        self._last_done = 0
        self._last_phase = None
        self._last_display_bucket = None

    def update(self, phase, done, total, pass_index=0, pass_count=1, **extra):
        if self.callback is None or phase not in self._weights:
            return
        ratio = min(1.0, max(0.0, float(done) / float(max(1, total))))
        pass_count = max(1, int(pass_count))
        pass_ratio = (min(max(0, int(pass_index)), pass_count - 1) + ratio) / pass_count
        completed = self._offsets[phase] + self._weights[phase] * pass_ratio
        scaled = int(round(POSTPROCESS_PROGRESS_TOTAL * completed / self._total_weight))
        previous_done = self._last_done
        scaled = max(self._last_done, min(POSTPROCESS_PROGRESS_TOTAL, scaled))
        self._last_done = scaled
        # QProgressBar 只显示整数百分比；窗口内仍逐次计算进度，但相同显示值不重复
        # 写入 JSON-line，避免超大影像后处理再制造数万条无可见变化的协议事件。
        display_bucket = int(10 * scaled / POSTPROCESS_PROGRESS_TOTAL)
        should_emit = (
            phase != self._last_phase
            or display_bucket != self._last_display_bucket
            or (scaled == POSTPROCESS_PROGRESS_TOTAL
                and previous_done < POSTPROCESS_PROGRESS_TOTAL)
        )
        if not should_emit:
            return
        self._last_phase = phase
        self._last_display_bucket = display_bucket
        payload = {
            "phase": phase,
            "status": self.PHASE_LABELS[phase],
        }
        payload.update(extra)
        self.callback(
            "postprocess", scaled, POSTPROCESS_PROGRESS_TOTAL, **payload)


def _window_count(width, height, block_size):
    return (
        int(math.ceil(float(width) / block_size))
        * int(math.ceil(float(height) / block_size))
    )


@dataclass(frozen=True)
class WindowPlan:
    tile_size: int
    core_size: int
    halo: int
    rows: int
    columns: int
    block_count: int


def _pixel_size_xy(transform):
    return abs(float(transform.a)), abs(float(transform.e))


def _crs_unit(crs):
    if crs is None:
        return None
    value = getattr(crs, "linear_units", None)
    if value:
        value = str(value).strip().lower()
        return "m" if value in {"m", "meter", "meters", "metre", "metres"} else value
    return None


def _factor_radius(postprocess, transform, crs_unit):
    if crs_unit != "m":
        return 1
    x_size, y_size = _pixel_size_xy(transform)
    radius = 1
    for name in ("tpi", "relief"):
        config = (postprocess.get("dem_factors") or {}).get(name) or {}
        window_m = float(config.get("window_m", 0) or 0)
        if window_m <= 0:
            continue
        window_y = max(1, int(round(window_m / y_size)))
        window_x = max(1, int(round(window_m / x_size)))
        if window_y % 2 == 0:
            window_y += 1
        if window_x % 2 == 0:
            window_x += 1
        radius = max(radius, window_y // 2, window_x // 2)
    return radius


def build_window_plan(width, height, tile_size, overlap, factor_radius):
    tile_size = max(32, int(tile_size))
    halo = max(1, int(overlap), int(factor_radius))
    if halo * 2 >= tile_size:
        raise ValueError(
            "tile_size={} 不能容纳两侧 halo={}；请增大推理块尺寸。".format(tile_size, halo))
    core_size = tile_size - halo * 2
    rows = int(math.ceil(float(height) / core_size))
    columns = int(math.ceil(float(width) / core_size))
    return WindowPlan(tile_size, core_size, halo, rows, columns, rows * columns)


def _windows(width, height, block_size):
    from rasterio.windows import Window

    for row in range(0, height, block_size):
        for col in range(0, width, block_size):
            yield Window(col, row, min(block_size, width - col), min(block_size, height - row))


def _windows_in_window(bounds_window, block_size):
    """在指定像素窗口内生成保持影像绝对偏移的核心窗口。"""
    from rasterio.windows import Window

    col_start = int(bounds_window.col_off)
    row_start = int(bounds_window.row_off)
    col_end = col_start + int(bounds_window.width)
    row_end = row_start + int(bounds_window.height)
    for row in range(row_start, row_end, block_size):
        for col in range(col_start, col_end, block_size):
            yield Window(
                col, row,
                min(block_size, col_end - col),
                min(block_size, row_end - row),
            )


def _roi_pixel_window(image_src, roi):
    """把输入影像 CRS 下的 ROI 转为裁剪后的整像素窗口。"""
    if not roi:
        return None
    import numpy as np
    from rasterio.windows import Window, from_bounds

    if str(roi.get("mode", "")) != "canvas_intersection":
        raise ValueError("不支持的 ROI 推理模式。")
    bounds = roi.get("bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        raise ValueError("ROI bounds 必须包含 xmin、ymin、xmax、ymax。")
    values = [float(value) for value in bounds]
    if not all(np.isfinite(values)):
        raise ValueError("ROI bounds 包含非有限数值。")
    xmin, ymin, xmax, ymax = values
    if xmin >= xmax or ymin >= ymax:
        raise ValueError("ROI bounds 不是有效矩形。")
    fractional = from_bounds(
        xmin, ymin, xmax, ymax, transform=image_src.transform)
    col0 = max(0, int(math.floor(float(fractional.col_off))))
    row0 = max(0, int(math.floor(float(fractional.row_off))))
    col1 = min(
        int(image_src.width),
        int(math.ceil(float(fractional.col_off + fractional.width))))
    row1 = min(
        int(image_src.height),
        int(math.ceil(float(fractional.row_off + fractional.height))))
    if col0 >= col1 or row0 >= row1:
        raise ValueError("ROI 与输入影像没有有效像素交集。")
    return Window(col0, row0, col1 - col0, row1 - row0)


def _window_audit(window):
    if window is None:
        return None
    return {
        "col_off": int(window.col_off),
        "row_off": int(window.row_off),
        "width": int(window.width),
        "height": int(window.height),
    }


def _expand_window(window, halo, width, height):
    from rasterio.windows import Window

    col0 = max(0, int(window.col_off) - halo)
    row0 = max(0, int(window.row_off) - halo)
    col1 = min(width, int(window.col_off + window.width) + halo)
    row1 = min(height, int(window.row_off + window.height) + halo)
    return Window(col0, row0, col1 - col0, row1 - row0)


def _crop_to_core(array, expanded, core):
    row = int(core.row_off - expanded.row_off)
    col = int(core.col_off - expanded.col_off)
    return array[..., row:row + int(core.height), col:col + int(core.width)]


def _output_profile(source_profile, dtype, count=1, nodata=0):
    from land_cover_classification.pytorch_inference_core import (
        build_geotiff_profile,
    )

    return build_geotiff_profile(
        source_profile, dtype, count=count, nodata=nodata,
        preferred_block_size=DEFAULT_BLOCK_SIZE)


def _align_dem_window(dem_src, dst_crs, dst_transform, height, width):
    import numpy as np
    from rasterio.windows import Window, from_bounds
    from rasterio.warp import transform_bounds
    from rasterio.warp import Resampling, reproject

    height = int(height)
    width = int(width)
    if height <= 0 or width <= 0 or height * width > 16 * 1024 * 1024:
        raise ValueError("DEM destination 必须是有限的局部窗口。")
    destination = np.full((height, width), np.nan, dtype="float32")
    left = float(dst_transform.c)
    top = float(dst_transform.f)
    right = left + float(dst_transform.a) * width
    bottom = top + float(dst_transform.e) * height
    src_left, src_bottom, src_right, src_top = transform_bounds(
        dst_crs, dem_src.crs,
        min(left, right), min(bottom, top), max(left, right), max(bottom, top),
        densify_pts=21,
    )
    source_window = from_bounds(
        src_left, src_bottom, src_right, src_top, transform=dem_src.transform)
    source_window = Window(
        source_window.col_off - 2,
        source_window.row_off - 2,
        source_window.width + 4,
        source_window.height + 4,
    ).round_offsets().round_lengths()
    try:
        source_window = source_window.intersection(
            Window(0, 0, dem_src.width, dem_src.height))
    except Exception as exc:
        raise ValueError("当前推理块与 DEM 没有空间重叠。") from exc
    source_masked = dem_src.read(1, window=source_window, masked=True)
    # rasterio.warp.reproject 对 MaskedArray 的处理会随 rasterio/GDAL 版本变化；
    # 某些版本会把本来有效的局部 DEM 窗口重投影成全 NoData。统一转换为
    # 带 NaN NoData 的普通 float32 数组，保持局部内存边界且避免版本差异。
    source = source_masked.astype("float32").filled(np.nan)
    reproject(
        source=source,
        destination=destination,
        src_transform=dem_src.window_transform(source_window),
        src_crs=dem_src.crs,
        src_nodata=np.nan,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    filled = ~np.isfinite(destination)
    if filled.all():
        raise ValueError(
            "当前推理块范围内未获得有效 DEM 高程；空间范围可能相交，"
            "但交叠区域可能全为 NoData，或 DEM 重投影失败。")
    if filled.any():
        from scipy import ndimage
        nearest = ndimage.distance_transform_edt(filled, return_distances=False, return_indices=True)
        destination = destination[tuple(nearest)]
    return destination.astype("float32", copy=False), filled


def _predict_probability(model, image, factors, bundle, device_cfg, tile_size):
    import numpy as np
    import torch
    from land_cover_classification.pytorch_inference_core import (
        _apply_active_dem_channels,
        _amp_autocast,
        _build_model_inputs,
        _dem_channel_names,
        _extract_logits,
        _factor_config,
        _factors_to_dict,
        _forward_model,
        _normalize_dem_stack,
        _pad_tile,
        _use_dual_inputs,
    )

    factor_cfg = _factor_config(bundle)
    factor_dict = _factors_to_dict(factors, factor_cfg)
    names = _dem_channel_names(bundle, factor_cfg)
    dem_stack = np.stack([factor_dict[name] for name in names]).astype("float32")
    dem_stack = _normalize_dem_stack(dem_stack, bundle.preprocess)
    dem_stack = _apply_active_dem_channels(dem_stack, names, bundle.preprocess)
    original_height, original_width = image.shape[1:]
    image = _pad_tile(image.astype("float32", copy=False), tile_size)
    dem_stack = _pad_tile(dem_stack, tile_size)
    inputs = _build_model_inputs(
        image, dem_stack,
        _use_dual_inputs(model, bundle), device_cfg["device"])
    if device_cfg["use_amp"]:
        with _amp_autocast(torch):
            logits = _forward_model(model, inputs)
    else:
        logits = _extract_logits(_forward_model(model, inputs))
    probabilities = torch.softmax(logits, dim=1)
    class_id = int(bundle.landslide_class_id)
    if class_id >= probabilities.shape[1]:
        raise ValueError("landslide_class_id 超出模型输出通道数。")
    return probabilities[0, class_id, :original_height, :original_width].detach().cpu().numpy().astype("float32")


def _initialize_roi_outputs(
        probability_dst, dem_filled_dst, valid_data_dst, width, height):
    """以有界磁盘块显式初始化 ROI 外的输出值。"""
    import numpy as np

    for window in _windows(width, height, DEFAULT_BLOCK_SIZE):
        shape = (int(window.height), int(window.width))
        probability_dst.write(
            np.full(shape, np.nan, dtype="float32"), 1, window=window)
        dem_filled_dst.write(
            np.zeros(shape, dtype="uint8"), 1, window=window)
        valid_data_dst.write(
            np.zeros(shape, dtype="uint8"), 1, window=window)


def _write_probability_raster(params, bundle, model, device_cfg, probability_path,
                              dem_filled_path, valid_data_path,
                              progress_callback=None):
    import numpy as np
    import rasterio
    import torch
    from rasterio.windows import Window, transform as window_transform
    from land_cover_classification.pytorch_inference_core import (
        _apply_array_preprocess,
        _crs_unit as core_crs_unit,
        _factor_config,
        _factors_to_dict,
        _merge_postprocess_config,
        _normalize_image,
        compute_dem_factors,
    )

    config = _merge_postprocess_config(bundle, params.get("postprocess_overrides") or {})
    with rasterio.open(params["input_path"]) as image_src, rasterio.open(params["dem_path"]) as dem_src:
        factor_radius = _factor_radius(config, image_src.transform, core_crs_unit(image_src.crs))
        tile_size = int(
            params.get("tile_size")
            or bundle.manifest.get("input_size")
            or device_cfg["tile_size"])
        overlap = int(params.get("overlap") or max(32, tile_size // 8))
        roi = params.get("roi")
        roi_window = _roi_pixel_window(image_src, roi)
        inference_window = roi_window or Window(
            0, 0, image_src.width, image_src.height)
        plan = build_window_plan(
            int(inference_window.width), int(inference_window.height),
            tile_size,
            overlap,
            factor_radius,
        )
        profile = _output_profile(image_src.profile, "float32", nodata=np.nan)
        mask_profile = _output_profile(image_src.profile, "uint8", nodata=0)
        with rasterio.open(probability_path, "w", **profile) as probability_dst, \
                rasterio.open(dem_filled_path, "w", **mask_profile) as dem_filled_dst, \
                rasterio.open(valid_data_path, "w", **mask_profile) as valid_data_dst:
            if roi_window is not None:
                _initialize_roi_outputs(
                    probability_dst, dem_filled_dst, valid_data_dst,
                    image_src.width, image_src.height)
            done = 0
            dem_stage_reported = False
            with torch.no_grad():
                for core in _windows_in_window(
                        inference_window, plan.core_size):
                    expanded = _expand_window(core, plan.halo, image_src.width, image_src.height)
                    image_masked = image_src.read(window=expanded, masked=True)
                    valid = ~np.any(np.ma.getmaskarray(image_masked), axis=0)
                    image = np.asarray(image_masked.filled(0))
                    image = _apply_array_preprocess(image, params.get("preprocess_flags") or {})
                    image = _normalize_image(image, bundle.preprocess)
                    transform = window_transform(expanded, image_src.transform)
                    dem, filled = _align_dem_window(
                        dem_src, image_src.crs, transform,
                        int(expanded.height), int(expanded.width))
                    if not dem_stage_reported and progress_callback is not None:
                        progress_callback("dem", 1, 1)
                        dem_stage_reported = True
                    raw_factors = compute_dem_factors(
                        bundle, dem, transform, config, core_crs_unit(image_src.crs))
                    factors = _factors_to_dict(raw_factors, _factor_config(bundle, config))
                    probability = _predict_probability(
                        model, image, factors, bundle, device_cfg, plan.tile_size)
                    core_probability = _crop_to_core(probability, expanded, core)
                    core_valid = _crop_to_core(valid, expanded, core)
                    core_filled = _crop_to_core(filled, expanded, core)
                    core_probability = np.where(core_valid, core_probability, np.nan).astype("float32")
                    probability_dst.write(core_probability, 1, window=core)
                    dem_filled_dst.write(core_filled.astype("uint8"), 1, window=core)
                    valid_data_dst.write(core_valid.astype("uint8"), 1, window=core)
                    done += 1
                    if progress_callback is not None:
                        progress_callback("predict", done, plan.block_count)
        dem_info = {
            "resolution": float(sum(abs(value) for value in dem_src.res) / 2.0)
            if core_crs_unit(dem_src.crs) == "m" else None,
            "crs_unit": core_crs_unit(dem_src.crs),
        }
        inference_audit = {
            "mode": "canvas_intersection" if roi_window is not None else "full_image",
            "bounds": list(roi.get("bounds") or []) if roi_window is not None else None,
            "crs_wkt": roi.get("crs_wkt") if roi_window is not None else None,
            "pixel_window": _window_audit(roi_window),
            "halo": plan.halo,
        }
        return image_src.profile.copy(), config, dem_info, plan, inference_audit


def _stream_binary_operation(source_path, output_path, operation, halo,
                             valid_data_path=None, progress_callback=None):
    import numpy as np
    import rasterio

    with rasterio.open(source_path) as src:
        total_windows = _window_count(src.width, src.height, DEFAULT_BLOCK_SIZE)
        profile = _output_profile(src.profile, "uint8", nodata=0)
        valid_src = rasterio.open(valid_data_path) if valid_data_path else None
        with rasterio.open(output_path, "w", **profile) as dst:
            for done, core in enumerate(
                    _windows(src.width, src.height, DEFAULT_BLOCK_SIZE), 1):
                expanded = _expand_window(core, halo, src.width, src.height)
                mask = src.read(1, window=expanded).astype(bool)
                processed = operation(mask)
                core_mask = _crop_to_core(processed, expanded, core).astype(bool)
                if valid_src is not None:
                    core_mask &= valid_src.read(1, window=core).astype(bool)
                dst.write(core_mask.astype("uint8"), 1, window=core)
                if progress_callback is not None:
                    progress_callback(done, total_windows)
        if valid_src is not None:
            valid_src.close()


def _threshold_probability(probability_path, mask_path, threshold,
                           progress_callback=None):
    import numpy as np
    import rasterio

    histogram = np.zeros(PROBABILITY_HISTOGRAM_BINS, dtype="int64")
    total = 0
    finite_count = 0
    minimum = None
    maximum = None
    total_probability = 0.0
    with rasterio.open(probability_path) as src:
        total_windows = _window_count(src.width, src.height, DEFAULT_BLOCK_SIZE)
        profile = _output_profile(src.profile, "uint8", nodata=0)
        with rasterio.open(mask_path, "w", **profile) as dst:
            for done, window in enumerate(
                    _windows(src.width, src.height, DEFAULT_BLOCK_SIZE), 1):
                probability = src.read(1, window=window)
                finite = probability[np.isfinite(probability)]
                if finite.size:
                    minimum = float(finite.min()) if minimum is None else min(minimum, float(finite.min()))
                    maximum = float(finite.max()) if maximum is None else max(maximum, float(finite.max()))
                    total_probability += float(finite.sum(dtype="float64"))
                    finite_count += int(finite.size)
                    indexes = np.minimum(
                        PROBABILITY_HISTOGRAM_BINS - 1,
                        np.maximum(0, (finite * PROBABILITY_HISTOGRAM_BINS).astype("int32")))
                    histogram += np.bincount(indexes, minlength=PROBABILITY_HISTOGRAM_BINS)
                mask = np.isfinite(probability) & (probability >= threshold)
                total += int(mask.sum())
                dst.write(mask.astype("uint8"), 1, window=window)
                if progress_callback is not None:
                    progress_callback(done, total_windows)
    def percentile(percent):
        if not finite_count:
            return None
        target = int(math.ceil(finite_count * percent / 100.0))
        index = int(np.searchsorted(np.cumsum(histogram), target, side="left"))
        return float((index + 0.5) / PROBABILITY_HISTOGRAM_BINS)
    stats = {
        "min": minimum,
        "max": maximum,
        "mean": total_probability / finite_count if finite_count else None,
        "p50": percentile(50),
        "p90": percentile(90),
        "p95": percentile(95),
        "p99": percentile(99),
    }
    return total, stats


class _DisjointSet:
    def __init__(self):
        self.parent = {}

    def add(self, value):
        self.parent.setdefault(int(value), int(value))

    def find(self, value):
        value = int(value)
        parent = self.parent[value]
        while parent != value:
            grandparent = self.parent[parent]
            self.parent[value] = grandparent
            value, parent = parent, grandparent
        return value

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            self.parent[right_root] = left_root


def _union_pairs(disjoint, left, right):
    import numpy as np

    valid = (left > 0) & (right > 0) & (left != right)
    if not valid.any():
        return
    pairs = np.unique(np.stack([left[valid], right[valid]], axis=1), axis=0)
    for first, second in pairs:
        disjoint.union(int(first), int(second))


def _label_components_stream(mask_path, labels_path, foreground=True, connectivity=8,
                             progress_callback=None):
    import numpy as np
    import rasterio
    from scipy import ndimage
    from rasterio.windows import Window

    disjoint = _DisjointSet()
    next_id = 1
    with rasterio.open(mask_path) as src:
        total_windows = _window_count(src.width, src.height, DEFAULT_BLOCK_SIZE)
        profile = _output_profile(src.profile, "int32", nodata=0)
        with rasterio.open(labels_path, "w+", **profile) as dst:
            for done, window in enumerate(
                    _windows(src.width, src.height, DEFAULT_BLOCK_SIZE), 1):
                values = src.read(1, window=window)
                target = values.astype(bool) if foreground else ~values.astype(bool)
                structure = np.ones((3, 3), dtype="uint8") if connectivity == 8 else None
                local, count = ndimage.label(target, structure=structure)
                if count:
                    local[local > 0] += next_id - 1
                    for value in range(next_id, next_id + count):
                        disjoint.add(value)
                if window.col_off > 0:
                    left = dst.read(1, window=Window(window.col_off - 1, window.row_off, 1, window.height))[:, 0]
                    _union_pairs(disjoint, local[:, 0], left)
                    if connectivity == 8:
                        _union_pairs(disjoint, local[1:, 0], left[:-1])
                        _union_pairs(disjoint, local[:-1, 0], left[1:])
                if window.row_off > 0:
                    top = dst.read(1, window=Window(window.col_off, window.row_off - 1, window.width, 1))[0]
                    _union_pairs(disjoint, local[0], top)
                    if connectivity == 8:
                        _union_pairs(disjoint, local[0, 1:], top[:-1])
                        _union_pairs(disjoint, local[0, :-1], top[1:])
                if connectivity == 8 and window.col_off > 0 and window.row_off > 0:
                    corner = dst.read(
                        1, window=Window(window.col_off - 1, window.row_off - 1, 1, 1))[0, 0]
                    if local[0, 0] and corner:
                        disjoint.union(int(local[0, 0]), int(corner))
                dst.write(local.astype("int32"), 1, window=window)
                next_id += count
                if progress_callback is not None:
                    progress_callback(done, total_windows)
    roots = {value: disjoint.find(value) for value in disjoint.parent}
    return roots


def _map_roots(labels, roots):
    import numpy as np

    unique, inverse = np.unique(labels, return_inverse=True)
    mapped = np.asarray(
        [0 if value == 0 else roots[int(value)] for value in unique],
        dtype=labels.dtype,
    )
    return mapped[inverse].reshape(labels.shape)


def _component_counts(labels_path, roots, progress_callback=None):
    import numpy as np
    import rasterio

    counts = {}
    border = set()
    with rasterio.open(labels_path) as src:
        total_windows = _window_count(src.width, src.height, DEFAULT_BLOCK_SIZE)
        for done, window in enumerate(
                _windows(src.width, src.height, DEFAULT_BLOCK_SIZE), 1):
            labels = _map_roots(src.read(1, window=window), roots)
            values, value_counts = np.unique(labels[labels > 0], return_counts=True)
            for value, count in zip(values, value_counts):
                value = int(value)
                counts[value] = counts.get(value, 0) + int(count)
            if window.row_off == 0:
                border.update(int(value) for value in labels[0] if value)
            if window.col_off == 0:
                border.update(int(value) for value in labels[:, 0] if value)
            if window.row_off + window.height == src.height:
                border.update(int(value) for value in labels[-1] if value)
            if window.col_off + window.width == src.width:
                border.update(int(value) for value in labels[:, -1] if value)
            if progress_callback is not None:
                progress_callback(done, total_windows)
    return counts, border


def _fill_small_holes(mask_path, output_path, max_pixels, temp_dir,
                      valid_data_path=None, progress_callback=None):
    import rasterio

    labels_path = os.path.join(temp_dir, "hole_labels.tif")
    roots = _label_components_stream(
        mask_path, labels_path, foreground=False, connectivity=4,
        progress_callback=(
            (lambda done, total: progress_callback(done, total, 0, 4))
            if progress_callback is not None else None))
    counts, border = _component_counts(
        labels_path, roots,
        progress_callback=(
            (lambda done, total: progress_callback(done, total, 1, 4))
            if progress_callback is not None else None))
    fill_roots = {root for root, count in counts.items() if root not in border and count <= max_pixels}
    valid_src = rasterio.open(valid_data_path) if valid_data_path else None
    with rasterio.open(mask_path) as src, rasterio.open(labels_path) as labels_src:
        total_windows = _window_count(src.width, src.height, DEFAULT_BLOCK_SIZE)
        profile = _output_profile(src.profile, "uint8", nodata=0)
        with rasterio.open(output_path, "w", **profile) as dst:
            for done, window in enumerate(
                    _windows(src.width, src.height, DEFAULT_BLOCK_SIZE), 1):
                mask = src.read(1, window=window).astype(bool)
                labels = _map_roots(labels_src.read(1, window=window), roots)
                if fill_roots:
                    import numpy as np
                    mask |= np.isin(labels, list(fill_roots))
                if valid_src is not None:
                    mask &= valid_src.read(1, window=window).astype(bool)
                dst.write(mask.astype("uint8"), 1, window=window)
                if progress_callback is not None:
                    progress_callback(done, total_windows, 2, 4)
    if valid_src is not None:
        valid_src.close()
    filled_count = sum(counts[root] for root in fill_roots)
    try:
        os.remove(labels_path)
    except OSError:
        pass
    return filled_count


def _count_mask(path, progress_callback=None):
    import rasterio
    total = 0
    with rasterio.open(path) as src:
        total_windows = _window_count(src.width, src.height, DEFAULT_BLOCK_SIZE)
        for done, window in enumerate(
                _windows(src.width, src.height, DEFAULT_BLOCK_SIZE), 1):
            total += int(src.read(1, window=window).astype(bool).sum())
            if progress_callback is not None:
                progress_callback(done, total_windows)
    return total


def _morphology_pipeline(probability_path, config, temp_dir,
                         valid_data_path=None, progress=None):
    from scipy import ndimage

    current = os.path.join(temp_dir, "threshold.tif")
    if progress is not None:
        progress.update("threshold", 0, 1)
    threshold_count, prob_stats = _threshold_probability(
        probability_path, current, float(config.get("threshold", 0.5)),
        progress_callback=(
            (lambda done, total: progress.update("threshold", done, total))
            if progress is not None else None))
    counts = {"threshold_pixel_count": threshold_count}
    current_count = threshold_count
    def advance(output):
        nonlocal current
        previous = current
        current = output
        if previous != current and os.path.dirname(previous) == os.path.abspath(temp_dir):
            try:
                os.remove(previous)
            except OSError:
                pass

    if config.get("morph_closing", False):
        size = max(1, int(config.get("morph_close_size", 5)))
        structure = __import__("numpy").ones((size, size), dtype=bool)
        output = os.path.join(temp_dir, "closing.tif")
        progress_callback = None
        if progress is not None:
            progress.update("closing", 0, 1, 0, 2)
            progress_callback = lambda done, total: progress.update(
                "closing", done, total, 0, 2)
        _stream_binary_operation(
            current, output,
            lambda mask: ndimage.binary_closing(mask, structure=structure),
            2 * (size // 2), valid_data_path, progress_callback)
        advance(output)
        current_count = _count_mask(
            current,
            progress_callback=(
                (lambda done, total: progress.update("closing", done, total, 1, 2))
                if progress is not None else None))
    counts["post_closing_pixel_count"] = current_count
    if config.get("fill_holes", False):
        import rasterio
        with rasterio.open(current) as src:
            pixel_area = abs(float(src.transform.a * src.transform.e))
        max_pixels = max(1, int(float(config.get("max_hole_area_m2", 0) or 0) / pixel_area))
        output = os.path.join(temp_dir, "filled.tif")
        if progress is not None:
            progress.update("fill_holes", 0, 1, 0, 4)
        _fill_small_holes(
            current, output, max_pixels, temp_dir, valid_data_path,
            progress_callback=(
                (lambda done, total, pass_index, pass_count: progress.update(
                    "fill_holes", done, total, pass_index, pass_count))
                if progress is not None else None))
        advance(output)
        current_count = _count_mask(
            current,
            progress_callback=(
                (lambda done, total: progress.update("fill_holes", done, total, 3, 4))
                if progress is not None else None))
    counts["post_fill_holes_pixel_count"] = current_count
    if config.get("smooth_boundary", False):
        size = max(1, int(config.get("smooth_size", 3)))
        structure = __import__("numpy").ones((size, size), dtype=bool)
        output = os.path.join(temp_dir, "smooth.tif")
        def smooth(mask):
            return ndimage.binary_opening(
                ndimage.binary_closing(mask, structure=structure), structure=structure)
        progress_callback = None
        if progress is not None:
            progress.update("smooth_boundary", 0, 1, 0, 2)
            progress_callback = lambda done, total: progress.update(
                "smooth_boundary", done, total, 0, 2)
        _stream_binary_operation(
            current, output, smooth, 4 * (size // 2), valid_data_path,
            progress_callback)
        advance(output)
        current_count = _count_mask(
            current,
            progress_callback=(
                (lambda done, total: progress.update(
                    "smooth_boundary", done, total, 1, 2))
                if progress is not None else None))
    counts["post_smooth_pixel_count"] = current_count
    if config.get("morph_opening", True):
        size = max(1, int(config.get("morph_open_size", 3)))
        structure = __import__("numpy").ones((size, size), dtype=bool)
        output = os.path.join(temp_dir, "opening.tif")
        progress_callback = None
        if progress is not None:
            progress.update("opening", 0, 1, 0, 2)
            progress_callback = lambda done, total: progress.update(
                "opening", done, total, 0, 2)
        _stream_binary_operation(
            current, output,
            lambda mask: ndimage.binary_opening(mask, structure=structure),
            2 * (size // 2), valid_data_path, progress_callback)
        advance(output)
        current_count = _count_mask(
            current,
            progress_callback=(
                (lambda done, total: progress.update("opening", done, total, 1, 2))
                if progress is not None else None))
    counts["post_opening_pixel_count"] = current_count
    return current, counts, prob_stats


def _write_audit_json(path, summary, components_path, component_count=0,
                      progress_callback=None):
    with open(path, "w", encoding="utf-8") as output:
        output.write("{\n")
        items = list(summary.items())
        for index, (key, value) in enumerate(items):
            output.write(json.dumps(str(key), ensure_ascii=False) + ": ")
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write(",\n")
        output.write('"components": [\n')
        first = True
        with open(components_path, "r", encoding="utf-8") as components:
            for done, line in enumerate(components, 1):
                if not first:
                    output.write(",\n")
                output.write(line.strip())
                first = False
                if progress_callback is not None:
                    progress_callback(done, max(1, component_count))
        output.write("\n]\n}\n")
    if progress_callback is not None:
        progress_callback(max(1, component_count), max(1, component_count))


def _component_rule_statistics(labels_path, roots, dem_filled_path, params, bundle, config,
                               temp_dir, progress_callback=None):
    import numpy as np
    import rasterio
    from rasterio.windows import transform as window_transform
    from land_cover_classification.pytorch_inference_core import (
        _crs_unit,
        _factor_config,
        _factors_to_dict,
        _ordered_rule_items,
        compute_dem_factors,
    )

    ordered_rules = _ordered_rule_items(config)
    enabled_rules = [(name, rule) for name, rule in ordered_rules if rule["enabled"]]
    fill_counts = {}
    pixel_counts = {}
    aggregates = {}
    median_rules = [(name, rule) for name, rule in enabled_rules if rule["stat"] == "median"]
    database = None
    if median_rules:
        database = sqlite3.connect(os.path.join(temp_dir, "component_values.sqlite"))
        database.execute("PRAGMA journal_mode=OFF")
        database.execute("PRAGMA synchronous=OFF")
        database.execute("CREATE TABLE values_table (root INTEGER, rule TEXT, value REAL)")

    with rasterio.open(labels_path) as labels_src, \
            rasterio.open(dem_filled_path) as filled_src, \
            rasterio.open(params["input_path"]) as image_src, \
            rasterio.open(params["dem_path"]) as dem_src:
        factor_radius = _factor_radius(config, image_src.transform, _crs_unit(image_src.crs))
        total_windows = _window_count(
            labels_src.width, labels_src.height, DEFAULT_BLOCK_SIZE)
        stats_pass_count = 2 if median_rules else 1
        for done, core in enumerate(
                _windows(labels_src.width, labels_src.height, DEFAULT_BLOCK_SIZE), 1):
            labels = _map_roots(labels_src.read(1, window=core), roots)
            component_roots = np.unique(labels[labels > 0])
            if not component_roots.size:
                if progress_callback is not None:
                    progress_callback(done, total_windows, 0, stats_pass_count)
                continue
            filled = filled_src.read(1, window=core).astype(bool)
            for root in component_roots:
                component_mask = labels == root
                root = int(root)
                count = int(component_mask.sum())
                pixel_counts[root] = pixel_counts.get(root, 0) + count
                fill_counts[root] = fill_counts.get(root, 0) + int(filled[component_mask].sum())
            if not enabled_rules:
                if progress_callback is not None:
                    progress_callback(done, total_windows, 0, stats_pass_count)
                continue
            expanded = _expand_window(core, factor_radius, image_src.width, image_src.height)
            transform = window_transform(expanded, image_src.transform)
            dem, _unused_filled = _align_dem_window(
                dem_src, image_src.crs, transform,
                int(expanded.height), int(expanded.width))
            raw_factors = compute_dem_factors(
                bundle, dem, transform, config, _crs_unit(image_src.crs))
            factors = _factors_to_dict(raw_factors, _factor_config(bundle, config))
            core_factors = {
                name: _crop_to_core(array, expanded, core)
                for name, array in factors.items()
            }
            for root_value in component_roots:
                root = int(root_value)
                component_mask = labels == root
                for rule_name, rule in enabled_rules:
                    values = np.asarray(core_factors[str(rule["factor"])][component_mask])
                    values = values[np.isfinite(values)]
                    if not values.size:
                        continue
                    stat = str(rule["stat"])
                    key = (root, rule_name)
                    if stat == "mean":
                        item = aggregates.setdefault(key, {"sum": 0.0, "count": 0})
                        item["sum"] += float(values.sum(dtype="float64"))
                        item["count"] += int(values.size)
                    elif stat == "min":
                        value = float(values.min())
                        aggregates[key] = value if key not in aggregates else min(aggregates[key], value)
                    elif stat == "max":
                        value = float(values.max())
                        aggregates[key] = value if key not in aggregates else max(aggregates[key], value)
                    elif stat == "median":
                        database.executemany(
                            "INSERT INTO values_table(root, rule, value) VALUES (?, ?, ?)",
                            ((root, rule_name, float(value)) for value in values),
                        )
            if progress_callback is not None:
                progress_callback(done, total_windows, 0, stats_pass_count)
    observed = {}
    for key, aggregate in aggregates.items():
        if isinstance(aggregate, dict):
            observed[key] = aggregate["sum"] / aggregate["count"] if aggregate["count"] else None
        else:
            observed[key] = aggregate
    if database is not None:
        database.execute("CREATE INDEX values_lookup ON values_table(root, rule, value)")
        total_roots = max(1, len(pixel_counts))
        for done, root in enumerate(pixel_counts, 1):
            for rule_name, _rule in median_rules:
                count = database.execute(
                    "SELECT COUNT(*) FROM values_table WHERE root=? AND rule=?",
                    (root, rule_name)).fetchone()[0]
                if not count:
                    observed[(root, rule_name)] = None
                    continue
                offset = (count - 1) // 2
                limit = 2 if count % 2 == 0 else 1
                rows = database.execute(
                    "SELECT value FROM values_table WHERE root=? AND rule=? "
                    "ORDER BY value LIMIT ? OFFSET ?",
                    (root, rule_name, limit, offset)).fetchall()
                observed[(root, rule_name)] = sum(row[0] for row in rows) / len(rows)
            if progress_callback is not None:
                progress_callback(done, total_roots, 1, 2)
        if progress_callback is not None and not pixel_counts:
            progress_callback(1, 1, 1, 2)
        database.close()
    fill_fraction = {
        root: float(fill_counts.get(root, 0)) / max(1, count)
        for root, count in pixel_counts.items()
    }
    return fill_fraction, observed


def _component_decisions(counts, pixel_area, config, fill_fraction, observed,
                         progress_callback=None):
    from land_cover_classification.pytorch_inference_core import (
        _compare_rule,
        _ordered_rule_items,
        _rule_threshold_value,
    )

    min_area = float(config.get("min_area_m2", 500.0))
    decisions = {}
    records = {}
    ordered_rules = _ordered_rule_items(config)
    total_components = max(1, len(counts))
    for done, (root, count) in enumerate(counts.items(), 1):
        area = float(count * pixel_area)
        evaluations = []
        keep = True
        decision_rule = None
        if area < min_area:
            keep = False
            decision_rule = "min_area"
        elif fill_fraction.get(root, 0.0) <= 0.5:
            for rule_name, rule in ordered_rules:
                if not rule["enabled"]:
                    continue
                threshold = float(_rule_threshold_value(rule_name, rule))
                value = observed.get((root, rule_name))
                passed = _compare_rule(value, str(rule["operator"]), threshold)
                evaluation = {
                    "rule": rule_name,
                    "factor": str(rule["factor"]),
                    "stat": str(rule["stat"]),
                    "operator": str(rule["operator"]),
                    "threshold_value": threshold,
                    "observed_value": value,
                    "passed": bool(passed),
                }
                evaluations.append(evaluation)
                if not passed:
                    keep = False
                    decision_rule = rule_name
                    break
        decisions[root] = keep
        records[root] = {
            "area_m2": area,
            "fill_fraction": fill_fraction.get(root, 0.0),
            "rule_evaluations": evaluations,
            "rule": decision_rule,
        }
        if progress_callback is not None:
            progress_callback(done, total_components)
    if progress_callback is not None and not counts:
        progress_callback(1, 1)
    return decisions, records, min_area


def _finalize_components(mask_path, output_path, dem_filled_path, params, bundle, config,
                         temp_dir, probability_stats,
                         stage_counts, runtime_metadata, inference_audit,
                         progress=None):
    import numpy as np
    import rasterio

    labels_path = os.path.join(temp_dir, "foreground_labels.tif")
    if progress is not None:
        progress.update("connected_components", 0, 1, 0, 2)
    roots = _label_components_stream(
        mask_path, labels_path, foreground=True, connectivity=8,
        progress_callback=(
            (lambda done, total: progress.update(
                "connected_components", done, total, 0, 2))
            if progress is not None else None))
    counts, _border = _component_counts(
        labels_path, roots,
        progress_callback=(
            (lambda done, total: progress.update(
                "connected_components", done, total, 1, 2))
            if progress is not None else None))
    with rasterio.open(mask_path) as src:
        pixel_area = abs(float(src.transform.a * src.transform.e))
        profile = _output_profile(src.profile, "uint8", nodata=0)
    if progress is not None:
        progress.update("dem_rules", 0, 1)
    fill_fraction, observed = _component_rule_statistics(
        labels_path, roots, dem_filled_path, params, bundle, config, temp_dir,
        progress_callback=(
            (lambda done, total, pass_index, pass_count: progress.update(
                "dem_rules", done, total, pass_index, pass_count))
            if progress is not None else None))
    if progress is not None:
        progress.update("component_decisions", 0, 1, 0, 2)
    decisions, records, min_area = _component_decisions(
        counts, pixel_area, config, fill_fraction, observed,
        progress_callback=(
            (lambda done, total: progress.update(
                "component_decisions", done, total, 0, 2))
            if progress is not None else None))
    kept = sum(1 for keep in decisions.values() if keep)
    dropped = len(decisions) - kept
    components_path = os.path.join(temp_dir, "components.jsonl")
    with open(components_path, "w", encoding="utf-8") as handle:
        total_components = max(1, len(counts))
        for comp_id, root in enumerate(sorted(counts), 1):
            area = float(counts[root] * pixel_area)
            component = {
                "comp_id": comp_id,
                "pixel_count": counts[root],
                "area_m2": area,
                "threshold": float(config.get("threshold", 0.5)),
                "rule_evaluations": records[root]["rule_evaluations"],
                "dem_fill_fraction": records[root]["fill_fraction"],
                "decision": "keep" if decisions[root] else "drop",
            }
            if not decisions[root]:
                rule_name = records[root]["rule"]
                component["rule"] = rule_name
                if rule_name == "min_area":
                    component["threshold"] = min_area
                elif records[root]["rule_evaluations"]:
                    component.update(records[root]["rule_evaluations"][-1])
            elif records[root]["fill_fraction"] > 0.5:
                component["rules_skipped"] = "dem_fill_fraction_gt_0.5"
            handle.write(json.dumps(component, ensure_ascii=False) + "\n")
            if progress is not None:
                progress.update(
                    "component_decisions", comp_id, total_components, 1, 2)
    if progress is not None and not counts:
        progress.update("component_decisions", 1, 1, 1, 2)
    if progress is not None:
        progress.update("write_result", 0, 1)
    with rasterio.open(labels_path) as labels_src, rasterio.open(output_path, "w", **profile) as dst:
        total_windows = _window_count(
            labels_src.width, labels_src.height, DEFAULT_BLOCK_SIZE)
        for done, window in enumerate(
                _windows(labels_src.width, labels_src.height, DEFAULT_BLOCK_SIZE), 1):
            labels = _map_roots(labels_src.read(1, window=window), roots)
            unique, inverse = np.unique(labels, return_inverse=True)
            values = np.asarray(
                [1 if root and decisions[int(root)] else 0 for root in unique],
                dtype="uint8",
            )
            output = values[inverse].reshape(labels.shape)
            dst.write(output, 1, window=window)
            if progress is not None:
                progress.update("write_result", done, total_windows)
    summary = {
        "schema_version": int(config.get("schema_version", 1)),
        "contract": "explicit_dem_factors_v3",
        "dem_factors": config["dem_factors"],
        "training_data": config["training_data"],
        "runtime_resolution": runtime_metadata.get("runtime_resolution", {}),
        "resolution_warnings": runtime_metadata.get("resolution_warnings", []),
        "inference": inference_audit,
        "rules": config.get("rules", {}),
        "rule_order": config.get("rule_order", list((config.get("rules") or {}).keys())),
        "threshold": float(config.get("threshold", 0.5)),
        "prob_stats": probability_stats,
        "prob_stats_method": "streaming_histogram_10000_bins",
        **stage_counts,
        "min_area_m2": min_area,
        "component_count": len(counts),
        "kept": kept,
        "dropped": dropped,
    }
    audit_path = output_path + ".postprocess.json"
    if progress is not None:
        progress.update("audit", 0, 1, kept=kept, dropped=dropped)
    _write_audit_json(
        audit_path, summary, components_path, len(counts),
        progress_callback=(
            (lambda done, total: progress.update(
                "audit", done, total, kept=kept, dropped=dropped))
            if progress is not None else None))
    summary["postprocess_path"] = audit_path
    return summary


def run_streaming_inference(params, progress_callback=None):
    """执行不持有整图数组的推理、后处理与 GeoTIFF 写出。"""
    from land_cover_classification.pytorch_inference_core import (
        _crs_unit,
        _runtime_metadata,
        build_model,
        load_bundle,
        select_device,
    )
    import rasterio

    bundle = load_bundle(params["model_path"])
    device_cfg = select_device()
    if progress_callback is not None:
        progress_callback("load", 1, 1, device=device_cfg["name"])
    model = build_model(bundle, device_cfg)
    output_path = os.path.abspath(params["output_path"])
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lcc_stream_", dir=output_dir) as temp_dir:
        probability_path = os.path.join(temp_dir, "probability.tif")
        dem_filled_path = os.path.join(temp_dir, "dem_filled.tif")
        valid_data_path = os.path.join(temp_dir, "valid_data.tif")
        image_profile, config, dem_info, plan, inference_audit = _write_probability_raster(
            params, bundle, model, device_cfg, probability_path, dem_filled_path,
            valid_data_path,
            progress_callback)
        with rasterio.open(params["input_path"]) as image_src:
            runtime_metadata = _runtime_metadata(
                config, image_src.transform, _crs_unit(image_src.crs), dem_info)
        postprocess_progress = _PostprocessProgress(progress_callback, config)
        mask_path, stage_counts, probability_stats = _morphology_pipeline(
            probability_path, config, temp_dir, valid_data_path,
            postprocess_progress)
        summary = _finalize_components(
            mask_path, output_path, dem_filled_path, params, bundle, config,
            temp_dir, probability_stats,
            stage_counts, runtime_metadata, inference_audit,
            postprocess_progress)
    if progress_callback is not None:
        progress_callback("write", 1, 1)
    return {
        "label_path": output_path,
        "postprocess_path": summary["postprocess_path"],
        "device": device_cfg["name"],
        "kept": summary["kept"],
        "dropped": summary["dropped"],
        "streaming": True,
        "tile_size": plan.tile_size,
        "core_size": plan.core_size,
        "halo": plan.halo,
        "roi": inference_audit if params.get("roi") else None,
    }
