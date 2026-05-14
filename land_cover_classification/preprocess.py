# -*- coding: utf-8 -*-
"""推理前可选的图像预处理函数。

每个函数接收一个 BGR 或灰度 `numpy.ndarray`,返回相同形状的数组。
`apply_chain` 按固定顺序在磁盘上执行启用的滤镜,中间结果落 `temp_dir`。
"""

import os

import cv2
import numpy as np


def clahe(img):
    """彩色图按通道分别做 CLAHE;灰度图直接一次性应用。"""
    op = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8))
    if img.ndim == 2:
        return op.apply(img)
    channels = cv2.split(img)
    return cv2.merge([op.apply(c) for c in channels])


def sharpen(img):
    """3x3 拉普拉斯风格的锐化卷积核。"""
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.int8)
    return cv2.filter2D(img, -1, kernel)


def median_blur(img):
    return cv2.medianBlur(img, 3)


def gaussian_blur(img):
    return cv2.GaussianBlur(img, (3, 3), 0, 0)


# 固定执行顺序;无论用户勾选哪几项,链路顺序保持确定。
_CHAIN = [
    ("clahe", clahe),
    ("sharpen", sharpen),
    ("median", median_blur),
    ("gaussian", gaussian_blur),
]


def apply_chain(input_path, flags, temp_dir):
    """按顺序串接启用的滤镜,返回最终输出文件路径。

    `flags` 形如 `{"clahe": True, "sharpen": False, ...}`。若所有项都未启用,
    直接返回原始 `input_path` 不做任何处理。
    """
    enabled = [(name, fn) for name, fn in _CHAIN if flags.get(name)]
    if not enabled:
        return input_path

    os.makedirs(temp_dir, exist_ok=True)
    img = cv2.imread(input_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError("cv2 无法读取影像:{}".format(input_path))

    base = os.path.splitext(os.path.basename(input_path))[0]
    last_path = input_path
    for name, fn in enabled:
        img = fn(img)
        last_path = os.path.join(temp_dir, "{}_{}.png".format(base, name))
        cv2.imwrite(last_path, img)
    return last_path
