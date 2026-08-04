# -*- coding: utf-8 -*-
"""AI 编辑会话状态控制。"""


class AiEditController:
    """限制 AI 编辑仅依赖当前工作影像和会话草稿。"""

    def __init__(self):
        self.preview_geometry = None
        self.active = False

    def startable(self, session, image_path):
        """返回 AI 启动前的业务校验错误；成功时返回空字符串。"""
        if not image_path:
            return "请先选择工作影像，无法启动 AI 编辑。"
        return ""

    def set_preview(self, geometry):
        """记录本次暂态 mask；负点只影响这个预览。"""
        self.preview_geometry = geometry

    def clear_preview(self):
        """丢弃暂态 mask，不产生任何持久化排除区域。"""
        self.preview_geometry = None

    def stop(self):
        """停止会话，不修改草稿要素。"""
        self.active = False
        self.clear_preview()
