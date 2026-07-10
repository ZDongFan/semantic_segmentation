# 插件运行环境

`sam_runtime/` 提供 PyTorch 主推理和 SAM AI 辅助编辑共用的独立运行环境创建脚本与说明文档。

请使用 `sam_runtime/create_sam_venv.bat` 或 `sam_runtime/create_sam_venv.sh` 创建 `venv/`；不要将重依赖安装到 QGIS 主进程 Python 环境。