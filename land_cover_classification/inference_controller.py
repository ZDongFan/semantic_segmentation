# -*- coding: utf-8 -*-
"""PyTorch 推理工作流的轻量控制器。"""

from __future__ import annotations


LANDSLIDE_CLASS_NAME = "landslide"


class BundleContractError(ValueError):
    """bundle 无法映射到单一 landslide 类别时抛出。"""


def landslide_class_id(manifest):
    """从 manifest 校验并返回唯一的 landslide 类别索引。"""
    if not isinstance(manifest, dict):
        raise BundleContractError("manifest 必须是对象。")
    labels = (manifest.get("class_names") or manifest.get("classes")
              or manifest.get("labels")
              or manifest.get("_Attributes", {}).get("labels") or [])
    if not isinstance(labels, list):
        raise BundleContractError("manifest.class_names 必须是数组。")

    matches = [
        index for index, label in enumerate(labels)
        if str(label).strip().casefold() == LANDSLIDE_CLASS_NAME
    ]
    if len(matches) != 1:
        raise BundleContractError(
            "manifest.class_names 必须且只能包含一个 landslide 类别，当前匹配到 {} 个。".format(
                len(matches)))

    configured = manifest.get("landslide_class_id")
    if configured is not None:
        if isinstance(configured, bool):
            raise BundleContractError("manifest.landslide_class_id 必须是整数。")
        try:
            configured = int(configured)
        except (TypeError, ValueError) as exc:
            raise BundleContractError(
                "manifest.landslide_class_id 必须是整数。") from exc
        if configured != matches[0]:
            raise BundleContractError(
                "manifest.landslide_class_id 与 class_names 中 landslide 的索引不一致: {} != {}。".format(
                    configured, matches[0]))
    return matches[0]


class InferenceController:
    """集中保存本次推理的 bundle 类别契约与运行标识。"""

    def __init__(self):
        self.run_id = None
        self.landslide_class_id = None
        self.manifest = None

    def prepare(self, manifest, run_id):
        """在启动子进程前校验 bundle 并记录本次运行。"""
        self.landslide_class_id = landslide_class_id(manifest)
        self.manifest = dict(manifest)
        self.run_id = str(run_id)
        return self.landslide_class_id
