# -*- coding: utf-8 -*-
"""地物分类插件的依赖检查。

`check()` 返回缺失的第三方包列表。
`installation_hint()` 根据平台拼接对应安装命令,paddlepaddle 指向官方
镜像源 —— 默认 PyPI 不提供本插件需要的 mkl/avx 优化版。
"""

import sys


# (显示名, 实际 import 模块名)
_REQUIREMENTS = [
    ("paddlepaddle", "paddle"),
    ("paddlers", "paddlers"),
    ("opencv-contrib-python", "cv2"),
    ("PyYAML", "yaml"),
    ("GDAL", "osgeo.gdal"),
]


def check():
    """返回缺失的依赖显示名列表。"""
    missing = []
    for display_name, module_name in _REQUIREMENTS:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(display_name)
    return missing


def installation_hint(missing):
    """为缺失依赖拼接一段可读的安装提示。"""
    if not missing:
        return ""

    lines = [
        "地物分类插件需要以下依赖包:",
        "  " + ", ".join(missing),
        "",
    ]

    if "GDAL" in missing:
        lines.extend([
            "GDAL 一般由 QGIS 自带(Windows 上来自 OSGeo4W,Linux 上来自",
            "系统 QGIS 包)。如果它缺失,请重装 QGIS,而不是用 pip 安装",
            "GDAL —— pip 版本极易与 QGIS 自带 Python 不匹配。",
            "",
        ])

    needs_pip = [m for m in missing if m != "GDAL"]
    if not needs_pip:
        return "\n".join(lines)

    if sys.platform.startswith("win"):
        lines.extend([
            "请打开「OSGeo4W Shell」(确保 QGIS 自带 Python 在 PATH 中),",
            "然后执行:",
            "",
            "  python -m pip install paddlepaddle==2.4.2 -f "
            "https://www.paddlepaddle.org.cn/whl/windows/mkl/avx/stable.html",
            "  python -m pip install -r "
            "\"%APPDATA%\\QGIS\\QGIS3\\profiles\\default\\python\\plugins"
            "\\land_cover_classification\\vendor\\PaddleRS\\requirements.txt\"",
        ])
    elif sys.platform.startswith("linux"):
        lines.extend([
            "请使用与 QGIS 相同的 Python 解释器执行:",
            "",
            "  python -m pip install paddlepaddle==2.4.2 -f "
            "https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html",
            "  python -m pip install -r "
            "~/.local/share/QGIS/QGIS3/profiles/default/python/plugins"
            "/land_cover_classification/vendor/PaddleRS/requirements.txt",
        ])
    else:
        lines.extend([
            "请前往以下页面选择对应平台的安装包:",
            "  https://www.paddlepaddle.org.cn/install/old",
            "然后安装 paddlepaddle==2.4.2,再执行(把路径替换为本机 QGIS 插件目录):",
            "  python -m pip install -r "
            ".../python/plugins/land_cover_classification/vendor/PaddleRS"
            "/requirements.txt",
        ])

    lines.extend([
        "",
        "如使用 GPU,请把 `paddlepaddle` 换成 "
        "`paddlepaddle_gpu==2.4.2.post<CUDA 版本>`,",
        "并到 https://www.paddlepaddle.org.cn/install/old 选对应镜像源 URL。",
        "",
        "安装完成后请重启 QGIS。",
    ])

    return "\n".join(lines)
