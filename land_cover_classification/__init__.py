# -*- coding: utf-8 -*-
"""
/***************************************************************************
 LandCoverClassification
                                 一个 QGIS 插件
 基于 PaddleRS 的遥感影像地物分类(语义分割)
                             -------------------
        begin                : 2026-05-14
        copyright            : (C) 2026 by zdf
        email                : 819754924@qq.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   本程序是自由软件;可在 GNU 通用公共许可证(版本 2 或更高版本,由自由软件 *
 *   基金会发布)条款下重新分发和/或修改。                                  *
 *                                                                         *
 ***************************************************************************/
 此脚本初始化插件,使其被 QGIS 识别加载。
"""

import os
import sys

# 把内置 vendor 的 PaddleRS 插到 sys.path 最前面,确保业务模块里 `import paddlers`
# 命中本插件携带的副本,而不是用户机器上可能存在的其他版本。
_VENDOR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "vendor", "PaddleRS"))
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """从 LandCoverClassification 文件加载主类。

    :param iface: QGIS 接口实例
    :type iface: QgsInterface
    """
    from .land_cover_classification import LandCoverClassification
    return LandCoverClassification(iface)
