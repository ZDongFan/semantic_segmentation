# -*- coding: utf-8 -*-
"""SAM AI 编辑推理子进程入口。

该脚本运行在插件专用的独立 Python venv 中，负责加载 SAM 后端、读取当前影像、
接收正负点提示并返回经轮廓化后的 mask 几何。QGIS 主进程只通过 stdin/stdout 上的
JSON line 协议和本脚本通信，不在主进程内导入 torch、sam2 或 segment_anything。

支持的 op:
- init      : 加载模型，参数 backend、model_path、config_path、model_type、device
- set_image : 设置当前推理影像，参数 image_path
- predict   : 输入正负点，返回 score 与 polygons
- reset     : 清空当前影像缓存
- quit      : 优雅退出
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import traceback


DEFAULT_BACKEND = "sam2"
SAM1_BACKEND = "sam1"
SAM2_BACKEND = "sam2"

MAX_MASK_POLYGONS = 1
MAX_MASK_RING_POINTS = 320
MAX_MASK_TOTAL_POINTS = 6000
MIN_MASK_CONTOUR_AREA = 64.0
MASK_SIMPLIFY_RATIO = 0.003
PROMPT_POINT_RADIUS = 2
NEGATIVE_ERASE_RADIUS = 4
SAM_CROP_SIZE = 1024
CROP_POINT_MARGIN_RATIO = 1.4
MIN_CROP_SCALE_FACTOR = 0.25
MAX_CROP_SCALE_FACTOR = 8.0
WHOLE_CROP_AREA_RATIO = 0.8
FALLBACK_LARGE_AREA_RATIO = 0.45
MAX_REFINED_AREA_GROWTH = 2.2
MIN_REFINED_AREA_KEEP_RATIO = 0.08


def _emit(message):
    """向 stdout 写入一行 JSON。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(req_id, **payload):
    payload["ok"] = True
    payload["id"] = req_id
    _emit(payload)


def _err(req_id, message, **extra):
    payload = {"ok": False, "id": req_id, "error": message}
    payload.update(extra)
    _emit(payload)


def _simplified_contour_points(cv2, contour):
    """简化单个外轮廓，避免复杂 mask 把 QGIS 主进程拖垮。"""
    perimeter = cv2.arcLength(contour, True)
    epsilon = max(1.0, perimeter * MASK_SIMPLIFY_RATIO)
    simplified = contour
    for _ in range(8):
        candidate = cv2.approxPolyDP(contour, epsilon, True)
        if len(candidate) >= 3:
            simplified = candidate
        if len(simplified) <= MAX_MASK_RING_POINTS:
            break
        epsilon *= 1.6

    pts = simplified.reshape(-1, 2)
    if len(pts) > MAX_MASK_RING_POINTS:
        step = max(1, int(round(float(len(pts)) / MAX_MASK_RING_POINTS)))
        pts = pts[::step]
    if len(pts) < 3:
        return []
    return [[int(point[0]), int(point[1])] for point in pts]


def _mask_to_polygons(mask):
    """把二值 mask 转换为像素坐标 polygon，只保留最大外轮廓。"""
    import cv2
    import numpy as np

    mask = np.asarray(mask)
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    if mask.ndim > 2:
        mask = mask.squeeze()
    if mask.size and mask.max() > 1:
        binary = mask
    else:
        binary = mask * 255

    contour_mode = (cv2.CHAIN_APPROX_TL89_KCOS
                    if hasattr(cv2, "CHAIN_APPROX_TL89_KCOS")
                    else cv2.CHAIN_APPROX_SIMPLE)
    contours, _hierarchy = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, contour_mode)
    if not contours:
        return []

    min_area = max(MIN_MASK_CONTOUR_AREA, float(mask.size) * 0.000002)
    candidates = []
    for idx, contour in enumerate(contours):
        area = abs(cv2.contourArea(contour))
        if area >= min_area:
            candidates.append((idx, area, contour))
    candidates.sort(key=lambda item: item[1], reverse=True)

    polygons = []
    total_points = 0
    for _idx, _area, contour in candidates[:MAX_MASK_POLYGONS]:
        pts = _simplified_contour_points(cv2, contour)
        if len(pts) < 3:
            continue
        if total_points + len(pts) > MAX_MASK_TOTAL_POINTS:
            break
        polygons.append({"shell": pts, "holes": []})
        total_points += len(pts)
    return polygons


def _read_image(image_path):
    """读取本地影像，优先兼容非 ASCII 路径。"""
    import cv2
    import numpy as np

    try:
        buf = np.fromfile(image_path, dtype=np.uint8)
        image = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if image is not None:
            return image
    except Exception:
        pass
    try:
        image = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
        if image is not None:
            return image
    except Exception:
        pass
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            array = np.asarray(image)
        if array.ndim == 3 and array.shape[2] == 4:
            return cv2.cvtColor(array, cv2.COLOR_RGBA2BGRA)
        if array.ndim == 3 and array.shape[2] == 3:
            return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
        return array
    except Exception:
        return None


def _ensure_uint8_image(image):
    """把遥感影像常见的高位深数组拉伸到 SAM 期望的 uint8。"""
    import numpy as np

    image = np.asarray(image)
    if image.dtype == np.uint8:
        return image

    array = image.astype(np.float32, copy=False)
    if array.size == 0:
        return image.astype(np.uint8)

    if array.ndim == 2:
        channels = [array]
    else:
        channels = [array[..., idx] for idx in range(array.shape[2])]

    stretched = []
    for channel in channels:
        finite = channel[np.isfinite(channel)]
        if finite.size == 0:
            stretched.append(np.zeros(channel.shape, dtype=np.uint8))
            continue
        low, high = np.percentile(finite, (2, 98))
        if high <= low:
            low, high = float(np.min(finite)), float(np.max(finite))
        if high <= low:
            stretched.append(np.zeros(channel.shape, dtype=np.uint8))
            continue
        scaled = (channel - low) * (255.0 / (high - low))
        stretched.append(np.clip(scaled, 0, 255).astype(np.uint8))

    if array.ndim == 2:
        return stretched[0]
    return np.stack(stretched, axis=2)


def _image_to_rgb(image):
    """把 OpenCV/Pillow 读取结果转换成 RGB 三通道数组。"""
    import cv2
    import numpy as np

    image = _ensure_uint8_image(np.asarray(image))
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    if image.ndim != 3:
        raise ValueError("不支持的影像维度: {}".format(image.shape))
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
    if image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    raise ValueError("不支持的影像通道数: {}".format(image.shape[2]))


def _points_to_arrays(positive_points, negative_points):
    """把协议中的正负点转换为 predictor 需要的 numpy 数组。"""
    import numpy as np

    points = []
    labels = []
    for point in positive_points or []:
        points.append([float(point[0]), float(point[1])])
        labels.append(1)
    for point in negative_points or []:
        points.append([float(point[0]), float(point[1])])
        labels.append(0)
    if not points:
        raise ValueError("至少需要一个提示点。")
    return (np.asarray(points, dtype=np.float32),
            np.asarray(labels, dtype=np.int32))


def _normalise_masks(masks):
    """统一不同 SAM predictor 的 mask 维度，返回二维 mask 列表。"""
    import numpy as np

    masks = np.asarray(masks)
    if masks.ndim == 2:
        return [masks]
    if masks.ndim == 3:
        return [masks[idx] for idx in range(masks.shape[0])]
    if masks.ndim == 4 and masks.shape[1] == 1:
        return [masks[idx, 0] for idx in range(masks.shape[0])]
    raise ValueError("不支持的 SAM mask 维度: {}".format(masks.shape))


def _point_hits_mask(mask, point):
    """用小窗口判断提示点是否落在 mask 内，降低像素取整误差影响。"""
    import numpy as np

    height, width = mask.shape[:2]
    x = int(round(float(point[0])))
    y = int(round(float(point[1])))
    if x < 0 or y < 0 or x >= width or y >= height:
        return False
    x0 = max(0, x - PROMPT_POINT_RADIUS)
    x1 = min(width, x + PROMPT_POINT_RADIUS + 1)
    y0 = max(0, y - PROMPT_POINT_RADIUS)
    y1 = min(height, y + PROMPT_POINT_RADIUS + 1)
    return bool(np.any(mask[y0:y1, x0:x1]))


def _mask_prompt_counts(mask, point_coords, point_labels):
    """统计候选 mask 对正负点的违背次数。"""
    positive_miss = 0
    negative_hit = 0
    for point, label in zip(point_coords, point_labels):
        hits = _point_hits_mask(mask, point)
        if int(label) == 1 and not hits:
            positive_miss += 1
        elif int(label) == 0 and hits:
            negative_hit += 1
    return positive_miss, negative_hit


def _mask_prompt_error(mask, point_coords, point_labels):
    """统计候选 mask 对正负点的违背程度。"""
    positive_miss, negative_hit = _mask_prompt_counts(
        mask, point_coords, point_labels)
    return positive_miss * 2 + negative_hit * 3


def _mask_area_ratio(mask):
    """返回 mask 占当前 crop 的面积比例。"""
    import numpy as np

    binary = np.asarray(mask) > 0
    if binary.size == 0:
        return 0.0
    return float(np.count_nonzero(binary)) / float(binary.size)


def _mask_boundary_ratio(mask):
    """估算 mask 贴住 crop 边界的程度，用于压低整片区域候选。"""
    import numpy as np

    binary = np.asarray(mask) > 0
    if binary.ndim != 2 or not np.any(binary):
        return 0.0
    boundary_count = (
        np.count_nonzero(binary[0, :])
        + np.count_nonzero(binary[-1, :])
        + np.count_nonzero(binary[:, 0])
        + np.count_nonzero(binary[:, -1])
    )
    edge_len = max(1, binary.shape[0] * 2 + binary.shape[1] * 2)
    return float(boundary_count) / float(edge_len)


def _split_prompt_points(point_coords, point_labels):
    """按标签拆分正负点坐标。"""
    positive_points = []
    negative_points = []
    for point, label in zip(point_coords, point_labels):
        if int(label) == 1:
            positive_points.append(point)
        else:
            negative_points.append(point)
    return positive_points, negative_points


def _component_ids_for_points(labels, points):
    """返回提示点小窗口命中的连通域编号。"""
    import numpy as np

    height, width = labels.shape[:2]
    comp_ids = set()
    for point in points:
        x = int(round(float(point[0])))
        y = int(round(float(point[1])))
        if x < 0 or y < 0 or x >= width or y >= height:
            continue
        x0 = max(0, x - PROMPT_POINT_RADIUS)
        x1 = min(width, x + PROMPT_POINT_RADIUS + 1)
        y0 = max(0, y - PROMPT_POINT_RADIUS)
        y1 = min(height, y + PROMPT_POINT_RADIUS + 1)
        for comp_id in np.unique(labels[y0:y1, x0:x1]):
            if int(comp_id) > 0:
                comp_ids.add(int(comp_id))
    return comp_ids


def _keep_positive_components(mask, positive_points):
    """只保留正点命中的连通域。"""
    import cv2
    import numpy as np

    binary = (np.asarray(mask) > 0).astype(np.uint8)
    if not positive_points or not np.any(binary):
        return binary
    _count, labels = cv2.connectedComponents(binary, connectivity=8)
    keep_ids = _component_ids_for_points(labels, positive_points)
    if not keep_ids:
        return np.zeros(binary.shape, dtype=np.uint8)
    return np.isin(labels, list(keep_ids)).astype(np.uint8)


def _apply_negative_hard_constraints(mask, positive_points, negative_points):
    """把负点作为硬约束处理，只清理负点附近的小范围残留。"""
    import cv2
    import numpy as np

    binary = (np.asarray(mask) > 0).astype(np.uint8)
    if not negative_points or not np.any(binary):
        return binary

    height, width = binary.shape[:2]

    for negative in negative_points:
        if not _point_hits_mask(binary, negative):
            continue
        x = int(round(float(negative[0])))
        y = int(round(float(negative[1])))
        if x < 0 or y < 0 or x >= width or y >= height:
            continue

        cv2.circle(binary, (x, y), NEGATIVE_ERASE_RADIUS, 0, thickness=-1)

    if positive_points:
        binary = _keep_positive_components(binary, positive_points)
    return binary.astype(np.uint8)


def _mask_result_rank(mask, score, point_coords, point_labels,
                      previous_area_ratio=None):
    """为候选结果排序，优先满足点约束，再考虑面积稳定性。"""
    area_ratio = _mask_area_ratio(mask)
    boundary_ratio = _mask_boundary_ratio(mask)
    positive_miss, negative_hit = _mask_prompt_counts(
        mask, point_coords, point_labels)
    has_negative = any(int(label) == 0 for label in point_labels)
    whole_crop = 1 if area_ratio >= WHOLE_CROP_AREA_RATIO else 0
    large_edge = 1 if (area_ratio >= FALLBACK_LARGE_AREA_RATIO
                       and boundary_ratio > 0.08) else 0
    shrink = 0
    growth = 0
    retention = 0.0
    if previous_area_ratio is not None and previous_area_ratio > 0:
        if has_negative:
            retention = min(area_ratio / previous_area_ratio, 1.0)
        if area_ratio > previous_area_ratio * MAX_REFINED_AREA_GROWTH:
            growth = 1
        if has_negative and area_ratio < (
                previous_area_ratio * MIN_REFINED_AREA_KEEP_RATIO):
            shrink = 1
    return (
        -negative_hit,
        -positive_miss,
        -shrink,
        -growth,
        -whole_crop,
        -large_edge,
        retention,
        float(score),
        -boundary_ratio,
        -area_ratio,
    )


def _candidate_rank(raw_mask, cleaned_mask, score, point_coords, point_labels,
                    previous_area_ratio=None):
    """候选排序同时参考 SAM 原始结果和最终清理结果。"""
    raw_positive_miss, raw_negative_hit = _mask_prompt_counts(
        raw_mask, point_coords, point_labels)
    return (
        -raw_negative_hit,
        -raw_positive_miss,
    ) + _mask_result_rank(
        cleaned_mask, score, point_coords, point_labels, previous_area_ratio)


def _select_initial_mask(masks, scores):
    """选择第一点的候选 mask，避免整幅 crop 被误选。"""
    import numpy as np

    candidates = _normalise_masks(masks)
    scores = np.asarray(scores).reshape(-1)
    if not candidates:
        raise ValueError("SAM 未返回有效 mask。")

    total_pixels = float(candidates[0].size)
    valid_indexes = [
        idx for idx, mask in enumerate(candidates)
        if float(np.count_nonzero(mask)) < WHOLE_CROP_AREA_RATIO * total_pixels
    ]
    if valid_indexes:
        best_idx = max(
            valid_indexes,
            key=lambda idx: float(scores[idx]) if idx < len(scores) else 0.0)
    else:
        best_idx = min(
            range(len(candidates)),
            key=lambda idx: int(np.count_nonzero(candidates[idx])))
    score = float(scores[best_idx]) if best_idx < len(scores) else 0.0
    return (np.asarray(candidates[best_idx]) > 0).astype(np.uint8), score, best_idx


def _select_prompt_mask(masks, scores, point_coords, point_labels,
                        previous_area_ratio=None):
    """按点提示一致性选择候选 mask，负点命中优先级高于 score。"""
    import numpy as np

    candidates = _normalise_masks(masks)
    scores = np.asarray(scores).reshape(-1)
    best = None

    for idx, mask in enumerate(candidates):
        raw_binary = (np.asarray(mask) > 0).astype(np.uint8)
        binary = _keep_prompt_components(mask, point_coords, point_labels)
        score = float(scores[idx]) if idx < len(scores) else 0.0
        rank = _candidate_rank(
            raw_binary, binary, score, point_coords, point_labels,
            previous_area_ratio)
        if best is None or rank > best[0]:
            best = (rank, binary, score, idx)

    if best is None:
        raise ValueError("SAM 未返回有效 mask。")
    return best[1].astype(np.uint8), float(best[2]), int(best[3])


def _prompt_signature(positive_points, negative_points):
    """生成点提示签名，用于判断是否能复用上一轮 logits。"""
    def _round_point(point):
        return (round(float(point[0]), 3), round(float(point[1]), 3))

    return (
        tuple(_round_point(point) for point in positive_points or []),
        tuple(_round_point(point) for point in negative_points or []),
    )


def _signature_extends(previous, current):
    """判断当前点集是否是在上一轮基础上继续追加。"""
    if previous is None:
        return False
    prev_pos, prev_neg = previous
    cur_pos, cur_neg = current
    if len(cur_pos) < len(prev_pos) or len(cur_neg) < len(prev_neg):
        return False
    return cur_pos[:len(prev_pos)] == prev_pos and \
        cur_neg[:len(prev_neg)] == prev_neg


def _resize_nearest(image, width, height):
    """用最近邻缩放二值 mask 或 crop，避免引入边界灰度。"""
    import cv2

    return cv2.resize(image, (int(width), int(height)),
                      interpolation=cv2.INTER_NEAREST)


def _clamp_crop_scale(scale_factor):
    """限制 crop 尺度，避免过度放大或缩小导致 SAM 上下文失真。"""
    try:
        scale = float(scale_factor)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    return max(MIN_CROP_SCALE_FACTOR, min(MAX_CROP_SCALE_FACTOR, scale))


def _keep_prompt_components(mask, point_coords, point_labels):
    """保留正点区域，并把负点作为最终 mask 的硬约束。"""
    import cv2
    import numpy as np

    binary = (np.asarray(mask) > 0).astype(np.uint8)
    positive_points, negative_points = _split_prompt_points(
        point_coords, point_labels)
    if not np.any(binary):
        return binary

    if positive_points:
        binary = _keep_positive_components(binary, positive_points)
        if not np.any(binary):
            return binary

    if negative_points:
        _count, labels = cv2.connectedComponents(binary, connectivity=8)
        remove_ids = _component_ids_for_points(labels, negative_points)
        keep_ids = _component_ids_for_points(labels, positive_points)
        for comp_id in remove_ids - keep_ids:
            binary[labels == comp_id] = 0
        binary = _apply_negative_hard_constraints(
            binary, positive_points, negative_points)
    return binary.astype(np.uint8)


def _select_low_res_mask(low_res_masks, best_idx):
    """按候选索引保留 predictor 原始 low_res_masks 维度。"""
    import numpy as np

    low_res_masks = np.asarray(low_res_masks)
    if low_res_masks.ndim == 2:
        return low_res_masks[None, :, :]
    if low_res_masks.ndim >= 3 and best_idx < low_res_masks.shape[0]:
        return low_res_masks[best_idx:best_idx + 1]
    if low_res_masks.ndim >= 3:
        return low_res_masks[:1]
    return low_res_masks


class BaseSamBackend(object):
    """SAM 后端基类，保存 predictor 与当前影像状态。"""

    backend_name = ""

    def __init__(self):
        self._predictor = None
        self._image_path = None
        self._image_shape = None
        self._image_rgb = None
        self._device = None
        self._model_type = None
        self._crop_bounds = None
        self._crop_shape = None
        self._crop_key = None
        self._last_low_res_mask = None
        self._last_prompt_signature = None
        self._last_crop_key = None
        self._last_mask_area_ratio = None

    def init(self, model_path, model_type=None, config_path=None,
             device=None):
        raise NotImplementedError

    def set_image(self, image_path):
        if self._predictor is None:
            raise RuntimeError("SAM 模型尚未初始化。")
        if not image_path or not os.path.isfile(image_path):
            raise IOError("影像文件不存在: {}".format(image_path))

        image = _read_image(image_path)
        if image is None:
            raise IOError("无法读取影像: {}".format(image_path))
        image_rgb = _image_to_rgb(image)

        self._image_path = image_path
        self._image_shape = (image_rgb.shape[0], image_rgb.shape[1])
        self._image_rgb = image_rgb
        self._reset_prompt_context(reset_crop=True)
        return {
            "image_path": image_path,
            "height": int(image_rgb.shape[0]),
            "width": int(image_rgb.shape[1]),
        }

    def predict(self, positive_points, negative_points,
                multimask_output=False, scale_factor=1.0):
        if self._predictor is None:
            raise RuntimeError("SAM 模型尚未初始化。")
        if self._image_shape is None or self._image_rgb is None:
            raise RuntimeError("尚未设置推理影像。")

        image_coords, point_labels = _points_to_arrays(
            positive_points, negative_points)
        self._ensure_crop(image_coords, point_labels, scale_factor)
        crop_coords, point_labels = self._filter_crop_points(
            image_coords, point_labels)
        signature = _prompt_signature(positive_points, negative_points)
        can_refine = (
            self._last_low_res_mask is not None
            and self._last_crop_key == self._crop_key
            and _signature_extends(self._last_prompt_signature, signature)
        )
        mask_input = self._last_low_res_mask if can_refine else None
        use_multimask = self._use_multimask(
            positive_points, negative_points, mask_input, multimask_output)

        previous_area_ratio = self._last_mask_area_ratio \
            if mask_input is not None else None

        masks, scores, low_res_masks = self._predict_masks(
            crop_coords, point_labels, mask_input, use_multimask)
        mask, score, best_idx = self._select_mask_result(
            masks, scores, crop_coords, point_labels, use_multimask,
            previous_area_ratio)
        selected_low_res_source = low_res_masks

        if self._needs_prompt_fallback(
                mask, crop_coords, point_labels, previous_area_ratio,
                mask_input):
            current_cleaned = _keep_prompt_components(
                mask, crop_coords, point_labels)
            current_rank = _candidate_rank(
                mask, current_cleaned, score, crop_coords, point_labels,
                previous_area_ratio)
            fallback_masks, fallback_scores, fallback_low_res_masks = \
                self._predict_masks(
                crop_coords, point_labels, None, True)
            fallback_mask, fallback_score, fallback_idx = \
                self._select_mask_result(
                    fallback_masks, fallback_scores, crop_coords, point_labels,
                    True,
                    previous_area_ratio)
            fallback_mask = _keep_prompt_components(
                fallback_mask, crop_coords, point_labels)
            fallback_rank = _candidate_rank(
                fallback_mask, fallback_mask, fallback_score, crop_coords,
                point_labels, previous_area_ratio)
            if fallback_rank > current_rank:
                mask = fallback_mask
                score = fallback_score
                best_idx = fallback_idx
                selected_low_res_source = fallback_low_res_masks
        mask = _keep_prompt_components(mask, crop_coords, point_labels)
        selected_low_res_mask = _select_low_res_mask(
            selected_low_res_source, best_idx)
        if _mask_area_ratio(mask) > 0:
            self._last_low_res_mask = selected_low_res_mask
            self._last_mask_area_ratio = _mask_area_ratio(mask)
        else:
            self._last_low_res_mask = None
            self._last_mask_area_ratio = None
        self._last_prompt_signature = signature
        self._last_crop_key = self._crop_key

        polygons = self._crop_polygons_to_image(_mask_to_polygons(mask))
        return {
            "score": float(score),
            "image_height": int(self._image_shape[0]),
            "image_width": int(self._image_shape[1]),
            "polygons": polygons,
            "backend": self.backend_name,
        }

    def _predict_masks(self, point_coords, point_labels, mask_input,
                       multimask_output):
        raise NotImplementedError

    def _use_multimask(self, positive_points, negative_points, mask_input,
                       multimask_output):
        del positive_points
        del negative_points
        return bool(multimask_output) or mask_input is None

    def _select_mask_result(self, masks, scores, point_coords, point_labels,
                            use_multimask, previous_area_ratio=None):
        import numpy as np

        if use_multimask:
            if any(int(label) == 0 for label in point_labels):
                return _select_prompt_mask(
                    masks, scores, point_coords, point_labels,
                    previous_area_ratio)
            return _select_initial_mask(masks, scores)
        if len(_normalise_masks(masks)) == 1:
            score = float(scores[0]) if len(scores) else 0.0
            return (np.asarray(_normalise_masks(masks)[0]) > 0).astype(
                np.uint8), score, 0
        return _select_prompt_mask(
            masks, scores, point_coords, point_labels, previous_area_ratio)

    def _needs_prompt_fallback(self, mask, point_coords, point_labels,
                               previous_area_ratio, mask_input):
        if mask_input is None:
            return False
        positive_miss, negative_hit = _mask_prompt_counts(
            mask, point_coords, point_labels)
        if positive_miss or negative_hit:
            return True
        area_ratio = _mask_area_ratio(mask)
        if area_ratio >= WHOLE_CROP_AREA_RATIO:
            return True
        if area_ratio >= FALLBACK_LARGE_AREA_RATIO and \
                _mask_boundary_ratio(mask) > 0.08:
            return True
        if previous_area_ratio is not None and previous_area_ratio > 0:
            if area_ratio > previous_area_ratio * MAX_REFINED_AREA_GROWTH:
                return True
            if any(int(label) == 0 for label in point_labels):
                if area_ratio < previous_area_ratio * MIN_REFINED_AREA_KEEP_RATIO:
                    return True
        return False

    def _reset_prompt_context(self, reset_crop=False):
        self._last_low_res_mask = None
        self._last_prompt_signature = None
        self._last_crop_key = None
        self._last_mask_area_ratio = None
        if reset_crop:
            self._crop_bounds = None
            self._crop_shape = None
            self._crop_key = None

    def _ensure_crop(self, image_coords, point_labels, scale_factor):
        if self._crop_can_accept_points(image_coords, point_labels):
            return self._image_points_to_crop(image_coords)
        crop = self._extract_crop(image_coords, scale_factor)
        self._predictor.set_image(crop)
        self._reset_prompt_context(reset_crop=False)
        return self._image_points_to_crop(image_coords)

    def _crop_can_accept_points(self, image_coords, point_labels):
        if self._crop_bounds is None:
            return False
        positive_coords = [
            point for point, label in zip(image_coords, point_labels)
            if int(label) == 1
        ]
        if positive_coords and self._crop_contains_points(positive_coords):
            return True
        return self._crop_contains_points(image_coords)

    def _crop_contains_points(self, image_coords):
        if self._crop_bounds is None:
            return False
        left, top, right, bottom = self._crop_bounds
        for x, y in image_coords:
            if x < left or x > right or y < top or y > bottom:
                return False
        return True

    def _filter_crop_points(self, image_coords, point_labels):
        import numpy as np

        crop_coords = self._image_points_to_crop(image_coords)
        crop_height, crop_width = self._crop_shape
        kept_coords = []
        kept_labels = []
        positive_count = 0
        for point, label in zip(crop_coords, point_labels):
            x, y = float(point[0]), float(point[1])
            inside = (0 <= x < crop_width and 0 <= y < crop_height)
            if int(label) == 1:
                if not inside:
                    raise ValueError("正点超出当前 SAM crop,请重新启动 AI 编辑。")
                positive_count += 1
                kept_coords.append([x, y])
                kept_labels.append(1)
            elif inside:
                kept_coords.append([x, y])
                kept_labels.append(0)
        if positive_count == 0:
            raise ValueError("至少需要一个位于当前 SAM crop 内的正点。")
        return (
            np.asarray(kept_coords, dtype=np.float32),
            np.asarray(kept_labels, dtype=np.int32),
        )

    def _extract_crop(self, image_coords, scale_factor=1.0):
        import math
        import numpy as np

        height, width = self._image_shape
        xs = [float(point[0]) for point in image_coords]
        ys = [float(point[1]) for point in image_coords]
        center_x = (min(xs) + max(xs)) / 2.0
        center_y = (min(ys) + max(ys)) / 2.0
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        base_size = float(SAM_CROP_SIZE) * _clamp_crop_scale(scale_factor)
        crop_size = max(base_size, span * CROP_POINT_MARGIN_RATIO)
        crop_size = min(crop_size, float(max(width, height)))

        left = center_x - crop_size / 2.0
        top = center_y - crop_size / 2.0
        right = left + crop_size
        bottom = top + crop_size
        if left < 0:
            right -= left
            left = 0.0
        if top < 0:
            bottom -= top
            top = 0.0
        if right > width:
            left = max(0.0, left - (right - width))
            right = float(width)
        if bottom > height:
            top = max(0.0, top - (bottom - height))
            bottom = float(height)

        x0 = max(0, int(math.floor(left)))
        y0 = max(0, int(math.floor(top)))
        x1 = min(width, int(math.ceil(right)))
        y1 = min(height, int(math.ceil(bottom)))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("无法根据提示点裁剪有效影像。")

        crop = self._image_rgb[y0:y1, x0:x1]
        if crop.size == 0:
            raise ValueError("裁剪影像为空。")
        crop = _resize_nearest(crop, SAM_CROP_SIZE, SAM_CROP_SIZE)
        self._crop_bounds = (float(x0), float(y0), float(x1), float(y1))
        self._crop_shape = (SAM_CROP_SIZE, SAM_CROP_SIZE)
        self._crop_key = tuple(round(value, 3) for value in self._crop_bounds)
        return np.ascontiguousarray(crop)

    def _image_points_to_crop(self, image_coords):
        import numpy as np

        if self._crop_bounds is None:
            raise RuntimeError("尚未建立 SAM crop。")
        left, top, right, bottom = self._crop_bounds
        crop_height, crop_width = self._crop_shape
        scale_x = float(crop_width - 1) / max(1e-6, right - left)
        scale_y = float(crop_height - 1) / max(1e-6, bottom - top)
        mapped = []
        for x, y in image_coords:
            mapped.append([
                (float(x) - left) * scale_x,
                (float(y) - top) * scale_y,
            ])
        return np.asarray(mapped, dtype=np.float32)

    def _crop_polygons_to_image(self, polygons):
        if self._crop_bounds is None or not polygons:
            return polygons
        left, top, right, bottom = self._crop_bounds
        crop_height, crop_width = self._crop_shape
        scale_x = (right - left) / max(1.0, float(crop_width - 1))
        scale_y = (bottom - top) / max(1.0, float(crop_height - 1))

        def _map_ring(ring):
            return [
                [int(round(left + float(point[0]) * scale_x)),
                 int(round(top + float(point[1]) * scale_y))]
                for point in ring
            ]

        mapped = []
        for polygon in polygons:
            mapped.append({
                "shell": _map_ring(polygon.get("shell") or []),
                "holes": [_map_ring(ring)
                          for ring in polygon.get("holes") or []],
            })
        return mapped

    def reset(self):
        self._image_path = None
        self._image_shape = None
        self._image_rgb = None
        self._reset_prompt_context(reset_crop=True)
        if self._predictor is not None:
            try:
                self._predictor.reset_image()
            except Exception:
                # 不同 SAM predictor 的 reset 行为不完全一致，这里只隔离异常。
                pass
        return {}

    def clear_context(self):
        """清空点提示上下文，但保留已加载影像，便于下一次按画布尺度重裁剪。"""
        self._reset_prompt_context(reset_crop=True)
        return {}


class Sam1Backend(BaseSamBackend):
    """SAM1 ViT 后端，用于回退兼容。"""

    backend_name = SAM1_BACKEND

    def init(self, model_path, model_type="vit_b", config_path=None,
             device=None):
        import torch
        from segment_anything import SamPredictor, sam_model_registry

        if model_type not in sam_model_registry:
            raise ValueError("不支持的 SAM1 模型类型: {}".format(model_type))
        if not model_path or not os.path.isfile(model_path):
            raise IOError("SAM1 模型权重不存在: {}".format(model_path))

        sam = sam_model_registry[model_type](checkpoint=model_path)
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        sam.to(device=device)
        self._predictor = SamPredictor(sam)
        self._device = device
        self._model_type = model_type
        self._image_path = None
        self._image_shape = None
        return {
            "backend": self.backend_name,
            "device": device,
            "model_type": model_type,
        }

    def _predict_masks(self, point_coords, point_labels, mask_input,
                       multimask_output):
        masks, scores, low_res_masks = self._predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            mask_input=mask_input,
            multimask_output=bool(multimask_output),
        )
        return masks, scores, low_res_masks


class Sam2Backend(BaseSamBackend):
    """SAM2/SAM2.1 后端，默认用于 AI 编辑。"""

    backend_name = SAM2_BACKEND

    def init(self, model_path, model_type="sam2.1_hiera_base_plus",
             config_path=None, device=None):
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        if not model_path or not os.path.isfile(model_path):
            raise IOError("SAM2 模型权重不存在: {}".format(model_path))
        config_path = config_path or self._default_config_path(model_type)
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        model = build_sam2(config_path, model_path, device=device)
        self._predictor = SAM2ImagePredictor(model)
        self._device = device
        self._model_type = model_type
        self._image_path = None
        self._image_shape = None
        return {
            "backend": self.backend_name,
            "device": device,
            "model_type": model_type,
            "config_path": config_path,
        }

    @staticmethod
    def _default_config_path(model_type):
        mapping = {
            "sam2.1_hiera_tiny": "configs/sam2.1/sam2.1_hiera_t.yaml",
            "sam2.1_hiera_small": "configs/sam2.1/sam2.1_hiera_s.yaml",
            "sam2.1_hiera_base_plus": "configs/sam2.1/sam2.1_hiera_b+.yaml",
            "sam2.1_hiera_large": "configs/sam2.1/sam2.1_hiera_l.yaml",
        }
        return mapping.get(model_type, "configs/sam2.1/sam2.1_hiera_b+.yaml")

    def _predict_masks(self, point_coords, point_labels, mask_input,
                       multimask_output):
        masks, scores, low_res_masks = self._predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            mask_input=mask_input,
            multimask_output=bool(multimask_output),
            normalize_coords=True,
        )
        return masks, scores, low_res_masks


class SamSession(object):
    """根据协议选择 SAM 后端，并转发后续请求。"""

    def __init__(self):
        self._backend = None

    def init(self, backend=DEFAULT_BACKEND, model_path=None, model_type=None,
             config_path=None, device=None):
        backend = (backend or DEFAULT_BACKEND).lower()
        if backend == SAM1_BACKEND:
            selected = Sam1Backend()
            model_type = model_type or "vit_b"
        elif backend == SAM2_BACKEND:
            selected = Sam2Backend()
            model_type = model_type or "sam2.1_hiera_base_plus"
        else:
            raise ValueError("不支持的 SAM 后端: {}".format(backend))

        result = selected.init(
            model_path=model_path,
            model_type=model_type,
            config_path=config_path,
            device=device,
        )
        self._backend = selected
        return result

    def _require_backend(self):
        if self._backend is None:
            raise RuntimeError("SAM 后端尚未初始化。")
        return self._backend

    def set_image(self, image_path):
        return self._require_backend().set_image(image_path)

    def predict(self, positive_points, negative_points,
                multimask_output=False, scale_factor=1.0):
        return self._require_backend().predict(
            positive_points, negative_points, multimask_output, scale_factor)

    def reset(self):
        return self._require_backend().reset()

    def clear_context(self):
        return self._require_backend().clear_context()


def _dispatch(session, message):
    op = message.get("op")
    req_id = message.get("id")
    if op == "init":
        result = session.init(
            backend=message.get("backend", DEFAULT_BACKEND),
            model_path=message.get("model_path"),
            model_type=message.get("model_type"),
            config_path=message.get("config_path"),
            device=message.get("device"),
        )
    elif op == "set_image":
        result = session.set_image(message.get("image_path"))
    elif op == "predict":
        result = session.predict(
            positive_points=message.get("positive_points") or [],
            negative_points=message.get("negative_points") or [],
            multimask_output=bool(message.get("multimask_output", False)),
            scale_factor=message.get("scale_factor", 1.0),
        )
    elif op == "reset":
        result = session.reset()
    elif op == "clear_context":
        result = session.clear_context()
    elif op == "quit":
        _ok(req_id, bye=True)
        return False
    else:
        raise ValueError("未知操作: {}".format(op))
    _ok(req_id, **result)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug-log")
    args = parser.parse_args(argv)
    del args

    session = SamSession()
    _emit({"event": "ready"})

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError as exc:
                _err(None, "JSON 解析失败: {}".format(exc))
                continue
            req_id = message.get("id")
            try:
                keep_going = _dispatch(session, message)
            except BaseException as exc:  # noqa: BLE001 - 子进程边界需要兜住所有异常
                _err(req_id, str(exc), traceback=traceback.format_exc())
                continue
            if not keep_going:
                break
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
