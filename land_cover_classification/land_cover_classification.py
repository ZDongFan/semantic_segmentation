# -*- coding: utf-8 -*-
"""
/***************************************************************************
 LandCoverClassification
                                 一个 QGIS 插件
 基于 PyTorch bundle 的遥感影像语义分割与 SAM 辅助编辑
                              -------------------
        begin                : 2026-05-14
        git sha              : $Format:%H$
        copyright            : (C) 2026 by zdf
        email                : 819754924@qq.com
 ***************************************************************************/
"""
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDockWidget

# 从 resources.py 加载 Qt 资源
from .resources import *
# 引入对话框
from .land_cover_classification_dialog import LandCoverClassificationDialog
import os.path


DOCK_PANEL_WIDTH = 400


class LandCoverClassification:
    """QGIS 插件实现类。"""

    def __init__(self, iface):
        """构造函数。

        :param iface: 传入的 QGIS 接口实例,通过它可以在运行期操作 QGIS。
        :type iface: QgsInterface
        """
        # 保存 QGIS 接口引用
        self.iface = iface
        # 插件目录
        self.plugin_dir = os.path.dirname(__file__)
        # 初始化本地化
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'LandCoverClassification_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        # 实例属性声明
        self.actions = []
        self.menu = self.tr(u'&地物分类')

        # 标记插件是否是当前 QGIS 会话中第一次启动
        # 必须在 initGui() 中赋值,以便插件重载后仍生效
        self.first_start = None
        self.dlg = None
        self.dock = None

    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """通过 Qt 翻译 API 获取字符串翻译。

        因为本类没有继承 QObject,所以自行实现该方法。

        :param message: 待翻译字符串
        :type message: str, QString

        :returns: 翻译后的字符串
        :rtype: QString
        """
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('LandCoverClassification', message)

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None):
        """向 QGIS 工具栏添加一个图标动作。

        :param icon_path: 图标路径,可以是 Qt 资源路径(如 ':/plugins/foo/bar.png')
            或文件系统路径。
        :type icon_path: str

        :param text: 菜单项中显示的文字。
        :type text: str

        :param callback: 动作被触发时的回调函数。
        :type callback: function

        :param enabled_flag: 动作是否默认启用,默认 True。
        :type enabled_flag: bool

        :param add_to_menu: 是否同时加入插件菜单,默认 True。
        :type add_to_menu: bool

        :param add_to_toolbar: 是否同时加入工具栏,默认 True。
        :type add_to_toolbar: bool

        :param status_tip: 鼠标悬停时显示的状态栏文字(可选)。
        :type status_tip: str

        :param parent: 新动作的父控件,默认 None。
        :type parent: QWidget

        :param whats_this: 鼠标悬停时在状态栏显示的提示文字(可选)。

        :returns: 创建好的动作。同时被加入 self.actions 列表。
        :rtype: QAction
        """

        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            # 把插件图标加入到插件工具栏
            self.iface.addToolBarIcon(action)

        if add_to_menu:
            self.iface.addPluginToMenu(
                self.menu,
                action)

        self.actions.append(action)

        return action

    def initGui(self):
        """在 QGIS GUI 中创建菜单项与工具栏图标。"""

        icon_path = ':/plugins/land_cover_classification/icon.png'
        self.add_action(
            icon_path,
            text=self.tr(u'地物分类'),
            callback=self.run,
            parent=self.iface.mainWindow())

        # 将在 run() 中置为 False
        self.first_start = True

    def unload(self):
        """从 QGIS GUI 中移除本插件的菜单项与工具栏图标。"""
        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr(u'&地物分类'),
                action)
            self.iface.removeToolBarIcon(action)

        if self.dock is not None:
            if self.dlg is not None:
                self.dlg.close()
            self.iface.mainWindow().removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
            self.dlg = None

    def _create_dock(self):
        """创建默认停靠在 QGIS 右侧的插件面板。"""
        dock = QDockWidget(self.tr(u'地物分类'), self.iface.mainWindow())
        dock.setObjectName("LandCoverClassificationDock")
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setMinimumWidth(DOCK_PANEL_WIDTH)
        dock.setMaximumWidth(DOCK_PANEL_WIDTH)
        dock.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable
        )

        self.dlg = LandCoverClassificationDialog(self.iface, parent=dock)
        self.dlg.setWindowFlags(Qt.Widget)
        self.dlg.setMinimumWidth(DOCK_PANEL_WIDTH)
        self.dlg.setMaximumWidth(DOCK_PANEL_WIDTH)
        try:
            self.dlg.closeBtn.clicked.disconnect()
        except TypeError:
            pass
        self.dlg.closeBtn.clicked.connect(dock.hide)

        dock.setWidget(self.dlg)
        self.iface.mainWindow().addDockWidget(Qt.RightDockWidgetArea, dock)
        self.dock = dock

    def run(self):
        """打开插件主对话框。"""

        settings = QSettings()
        notice_key = "LandCoverClassification/paddlers_deprecated_notice_shown"
        if not settings.value(notice_key, False, type=bool):
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.information(
                self.iface.mainWindow(),
                self.tr("PaddleRS 已下线"),
                self.tr("PaddleRS 推理入口已下线，请使用 PyTorch bundle。详见 docs/model_layout.md。"),
            )
            settings.setValue(notice_key, True)

        # 延迟构造对话框,只在首次使用时创建一次。
        if self.first_start or self.dock is None:
            self.first_start = False
            self._create_dock()

        # 非模态:推理过程中用户仍可平移/缩放画布。
        self.dlg.show()
        self.dock.show()
        self.dock.raise_()
        self.dock.activateWindow()