# -*- coding: utf-8 -*-
"""扫描 PyTorch 语义分割 bundle。"""

import json
import os

try:
    from qgis.core import QgsMessageLog, Qgis
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


_LOG_TAG = "LandCoverClassification"


def _log(message, level="info"):
    if not _HAS_QGIS:
        return
    qgis_level = {
        "info": Qgis.Info,
        "warning": Qgis.Warning,
        "critical": Qgis.Critical,
    }.get(level, Qgis.Info)
    QgsMessageLog.logMessage(message, _LOG_TAG, qgis_level)


def get_model_info(model_dir):
    """读取 PyTorch bundle 的 `manifest.json`。"""
    manifest_path = os.path.join(model_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            "目录 {} 下没有 manifest.json 文件。".format(model_dir))
    with open(manifest_path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _is_valid_pytorch_bundle(manifest):
    return (
        manifest.get("framework") == "pytorch"
        and manifest.get("task") == "semantic_segmentation"
    )


def _display_name(entry, manifest):
    return (
        manifest.get("display_name")
        or manifest.get("name")
        or manifest.get("model_name")
        or entry
    )


def scan(root_dir):
    """列出 `root_dir` 下可用的 PyTorch 语义分割 bundle。"""
    results = []
    if not root_dir or not os.path.isdir(root_dir):
        return results

    for entry in sorted(os.listdir(root_dir)):
        sub_dir = os.path.join(root_dir, entry)
        if not os.path.isdir(sub_dir):
            continue

        manifest_path = os.path.join(sub_dir, "manifest.json")
        if os.path.isfile(manifest_path):
            try:
                manifest = get_model_info(sub_dir)
            except Exception as exc:  # noqa: BLE001 - manifest 解析失败要跳过。
                _log("跳过 '{}':解析 manifest.json 失败({})".format(
                    sub_dir, exc), "warning")
                continue
            if _is_valid_pytorch_bundle(manifest):
                results.append({
                    "name": _display_name(entry, manifest),
                    "path": os.path.abspath(sub_dir),
                    "backend": "pytorch",
                    "manifest": manifest,
                })
            else:
                _log("跳过 '{}':不是 PyTorch 语义分割 bundle".format(sub_dir),
                     "info")
            continue

        _log("跳过 '{}':缺少 manifest.json".format(sub_dir), "info")

    return results
