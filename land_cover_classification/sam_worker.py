# -*- coding: utf-8 -*-
"""SAM AI 编辑推理子进程入口。

该脚本运行在独立的 SAM 虚拟环境下,负责:
- 加载 SAM ViT-B 模型权重
- 读取本机影像,做必要裁剪
- 接收主进程发来的正/负样本点提示
- 返回二值 mask 与 score

与主对话框之间使用 stdin/stdout 上的 JSON line 协议通信,每行一条
消息,字段 `op` 表示操作类型,字段 `id` 用于配对请求和响应。

支持的 op:
- init       : 加载模型,参数 model_path、model_type、device
- set_image  : 设置当前推理影像,参数 image_path
- predict    : 输入正负样本点,返回 mask 几何描述
- reset      : 清空当前影像缓存
- quit       : 优雅退出

为了避免 mask numpy 数据通过 stdout 大量传输,
predict 的返回值不是原始 mask,而是经过 OpenCV 轮廓化后的
多边形坐标(影像像素空间),由主进程负责像素 -> 地理坐标的换算。
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import traceback


MAX_MASK_POLYGONS = 1
MAX_MASK_RING_POINTS = 240
MAX_MASK_TOTAL_POINTS = 6000
MIN_MASK_CONTOUR_AREA = 64.0
MASK_SIMPLIFY_RATIO = 0.004


def _emit(message):
    """向 stdout 写一行 JSON。"""
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
    """简化单个轮廓,避免复杂 mask 把 QGIS 主进程拖崩。"""
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


class _SamSession:
    """SAM 模型会话,保存当前模型和影像 embedding。"""

    def __init__(self):
        self._predictor = None
        self._image_path = None
        self._image_shape = None  # (height, width)

    def init(self, model_path, model_type="vit_b", device=None):
        import torch
        from segment_anything import sam_model_registry, SamPredictor

        if model_type not in sam_model_registry:
            raise ValueError("不支持的 SAM 模型类型: {}".format(model_type))
        if not model_path or not os.path.isfile(model_path):
            raise IOError("SAM 模型权重不存在: {}".format(model_path))

        sam = sam_model_registry[model_type](checkpoint=model_path)
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        sam.to(device=device)
        self._predictor = SamPredictor(sam)
        self._image_path = None
        self._image_shape = None
        return {"device": device, "model_type": model_type}

    def set_image(self, image_path):
        import cv2

        if self._predictor is None:
            raise RuntimeError("SAM 模型尚未初始化。")
        if not image_path or not os.path.isfile(image_path):
            raise IOError("影像文件不存在: {}".format(image_path))

        # 路径含非 ASCII 字符时优先走 fromfile,避免 cv2.imread 先写 warning。
        image = self._read_image(image_path)
        if image is None:
            raise IOError("无法读取影像: {}".format(image_path))
        if image.ndim == 2:
            image_rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        elif image.shape[2] == 4:
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
        else:
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        self._predictor.set_image(image_rgb)
        self._image_path = image_path
        self._image_shape = (image_rgb.shape[0], image_rgb.shape[1])
        return {
            "image_path": image_path,
            "height": int(image_rgb.shape[0]),
            "width": int(image_rgb.shape[1]),
        }

    @staticmethod
    def _read_image(image_path):
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

            image = Image.open(image_path)
            return np.asarray(image)
        except Exception:
            return None

    def predict(self, positive_points, negative_points,
                multimask_output=False):
        import numpy as np

        if self._predictor is None:
            raise RuntimeError("SAM 模型尚未初始化。")
        if self._image_shape is None:
            raise RuntimeError("尚未设置推理影像。")
        if not positive_points and not negative_points:
            raise ValueError("至少需要一个提示点。")

        points = []
        labels = []
        for point in positive_points or []:
            points.append([float(point[0]), float(point[1])])
            labels.append(1)
        for point in negative_points or []:
            points.append([float(point[0]), float(point[1])])
            labels.append(0)
        point_coords = np.asarray(points, dtype=np.float32)
        point_labels = np.asarray(labels, dtype=np.int32)

        masks, scores, _ = self._predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            multimask_output=bool(multimask_output),
        )
        # 选 score 最高的那张 mask
        best_idx = int(np.argmax(scores))
        mask = masks[best_idx].astype(np.uint8)
        score = float(scores[best_idx])

        polygons = self._mask_to_polygons(mask)
        return {
            "score": score,
            "image_height": int(self._image_shape[0]),
            "image_width": int(self._image_shape[1]),
            "polygons": polygons,
        }

    @staticmethod
    def _mask_to_polygons(mask):
        """把二值 mask 转换为像素坐标多边形列表,便于走文本通道传输。"""
        import cv2

        contours, _hierarchy = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TL89_KCOS
            if hasattr(cv2, "CHAIN_APPROX_TL89_KCOS")
            else cv2.CHAIN_APPROX_SIMPLE,
        )
        polygons = []
        if not contours:
            return polygons

        min_area = max(MIN_MASK_CONTOUR_AREA, float(mask.size) * 0.000002)
        outer_candidates = []
        for idx, contour in enumerate(contours):
            area = abs(cv2.contourArea(contour))
            if area < min_area:
                continue
            outer_candidates.append((idx, area, contour))

        outer_candidates.sort(key=lambda item: item[1], reverse=True)
        outers = {}
        total_points = 0
        for idx, _area, contour in outer_candidates[:MAX_MASK_POLYGONS]:
            pts = _simplified_contour_points(cv2, contour)
            if len(pts) < 3:
                continue
            if total_points + len(pts) > MAX_MASK_TOTAL_POINTS:
                break
            outers[idx] = {"shell": pts, "holes": []}
            total_points += len(pts)
        polygons.extend(outers.values())
        return polygons

    def reset(self):
        self._image_path = None
        self._image_shape = None
        if self._predictor is not None:
            try:
                self._predictor.reset_image()
            except Exception:
                # 不同版本的 segment_anything 在 reset 时行为可能不一致,
                # 这里只确保不传递异常给主进程。
                pass
        return {}


def _dispatch(session, message):
    op = message.get("op")
    req_id = message.get("id")
    if op == "init":
        result = session.init(
            model_path=message.get("model_path"),
            model_type=message.get("model_type", "vit_b"),
            device=message.get("device"),
        )
    elif op == "set_image":
        result = session.set_image(message.get("image_path"))
    elif op == "predict":
        result = session.predict(
            positive_points=message.get("positive_points") or [],
            negative_points=message.get("negative_points") or [],
            multimask_output=bool(message.get("multimask_output", False)),
        )
    elif op == "reset":
        result = session.reset()
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

    session = _SamSession()
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
            except BaseException as exc:  # noqa: BLE001 - 子进程边界
                _err(req_id, str(exc), traceback=traceback.format_exc())
                continue
            if not keep_going:
                break
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
