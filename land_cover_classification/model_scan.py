# -*- coding: utf-8 -*-
"""扫描目录,发现已导出的 PaddleRS 分割模型。

每个候选模型放在独立子目录中,至少包含一份 `model.yml` 描述文件。
本模块通过 PyYAML 解析该文件,仅保留 `_Attributes.model_type == "segmenter"`
的子目录。
"""

import os

import yaml

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
    """读取并返回已导出 PaddleRS 模型目录下的 `model.yml`。

    若文件不存在,抛出 FileNotFoundError。
    """
    yml_path = os.path.join(model_dir, "model.yml")
    if not os.path.exists(yml_path):
        raise FileNotFoundError(
            "目录 {} 下没有 model.yml 文件。".format(model_dir))
    with open(yml_path, "r", encoding="utf-8") as f:
        return yaml.load(f.read(), Loader=yaml.Loader)


def scan(root_dir):
    """列出 `root_dir` 下发现的所有分割模型。

    返回形如 `[{"name": <模型名>, "path": <绝对路径>}, ...]` 的列表。
    无法解析 `model.yml` 或类型不匹配的子目录会被静默跳过,跳过原因
    会写入 QGIS 消息日志(如果在 QGIS 环境内运行)。
    """
    results = []
    if not root_dir or not os.path.isdir(root_dir):
        return results

    for entry in sorted(os.listdir(root_dir)):
        sub_dir = os.path.join(root_dir, entry)
        if not os.path.isdir(sub_dir):
            continue
        try:
            info = get_model_info(sub_dir)
        except FileNotFoundError:
            _log("跳过 '{}':缺少 model.yml".format(sub_dir), "info")
            continue
        except Exception as exc:  # noqa: BLE001 - YAML 解析失败等异常
            _log("跳过 '{}':解析 model.yml 失败({})".format(
                sub_dir, exc), "warning")
            continue

        try:
            model_type = info["_Attributes"]["model_type"]
        except (KeyError, TypeError):
            _log("跳过 '{}':缺少 _Attributes.model_type 字段".format(sub_dir),
                 "info")
            continue

        if model_type != "segmenter":
            _log(
                "跳过 '{}':model_type 为 '{}',不是 'segmenter'".format(
                    sub_dir, model_type), "info")
            continue

        display_name = info.get("Model") or entry
        results.append({"name": display_name, "path": os.path.abspath(sub_dir)})

    return results
