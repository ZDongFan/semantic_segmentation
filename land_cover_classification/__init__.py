# -*- coding: utf-8 -*-
"""
/***************************************************************************
 LandCoverClassification
                                 一个 QGIS 插件
 基于 PyTorch bundle 的遥感影像语义分割与 SAM 辅助编辑
                             -------------------
        begin                : 2026-05-14
        copyright            : (C) 2026 by zdf
        email                : 819754924@qq.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   本程序是自由软件；可在 GNU 通用公共许可证版本 2 或更高版本条款下重新分发或修改。 *
 *                                                                         *
 ***************************************************************************/
 此脚本初始化插件，使其被 QGIS 识别加载。
"""


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """从 LandCoverClassification 模块加载插件主类。

    :param iface: QGIS 接口实例
    :type iface: QgsInterface
    """
    from .land_cover_classification import LandCoverClassification
    return LandCoverClassification(iface)
