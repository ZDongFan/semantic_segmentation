# 项目核心

- QGIS 3.44+ 插件，显示名 `LandCoverClassification`；插件源码根为 `land_cover_classification/`。
- 主流程：PyTorch bundle 语义分割 + 可选 DEM 对齐/派生因子/规则后处理（无 DEM 使用归一化零值中性输入，部分覆盖按有效掩膜混合），输出类别 GeoTIFF、审计 `<output>.postprocess.json` 和草稿矢量；随后可编辑并导出 GeoTIFF、Shapefile、DXF。
- UI 与 QGIS 集成：`land_cover_classification.py` 负责插件入口、菜单/停靠面板；`land_cover_classification_dialog.py` 负责输入、运行、草稿、导出和 AI 编辑。其余模块定位见 `mem:tech_stack`。
- 推理核心：`pytorch_inference_core.run_inference()` 委托 `pytorch_streaming.py` 执行 core+halo 窗口化磁盘流；不得使用整图回归工具替代生产入口。`pytorch_inference_runner.py` 是 JSON 行协议子进程入口；`model_scan.py` 只接受含有效 `manifest.json` 的 PyTorch bundle。
- 草稿以 `DraftSession` 的临时 GeoPackage generation 为唯一真相；推理融合保留用户区域，局部推理只替换 ROI 内 inference。AI/手工编辑与导出通过会话协调。
- 安装调用关系与源码导航见 `docs/development.md`：打开面板检查解释器存在性，`RuntimeInstallDialog` 用 QProcess 执行固定独立 Python 安装入口，成功后不自动推理。
- AI 编辑：`ai_segment_tool.py` 处理画布点与预览；`sam_worker.py` 是 JSON 行协议 worker，支持 init/set_image/predict/clear_context/quit；对话框负责将有效几何写回草稿层。
- 核心不变量与 bundle/DEM 契约见 `mem:conventions`；开发与验证命令见 `mem:suggested_commands`、`mem:task_completion`。
- 工作区 UI 的条件和摘要由 `workflow_ui.py` 推导；项目页通过 `project_panel.py` 组织保存、发布及历史入口。非模态属性、版本对比和本地恢复分别由 `attribute_browser.py`、`history_dialogs.py`、`recovery.py` 负责，开发边界见 `docs/development.md`，状态及失败保护见 `mem:conventions`。
