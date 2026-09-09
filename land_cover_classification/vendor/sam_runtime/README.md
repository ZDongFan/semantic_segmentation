# 插件统一独立运行环境

本目录只保留三个主要实现文件：

- `create_sam_venv.bat`：Windows 入口，内联调用系统 PowerShell 下载固定 Python。
- `create_sam_venv.sh`：Linux/macOS 入口，显式通过 Bash 执行。
- `runtime_setup.py`：公共 venv 创建、依赖安装、功能验证、日志和环境清理。

使用 python-build-standalone **CPython 3.12.12 / 20251014** 完整发行包；不探测 QGIS/系统 Python，不再使用 `SAM_PYTHON` 或 `SAM_RECREATE`。两个入口的 URL 和 SHA-256 均固定。`requirements-sam.txt` 仅供参考，实际安装保留 Torch 专用源与精确约束。

Windows：双击或运行 `create_sam_venv.bat`；插件传入 `--non-interactive`。Python 和 venv 存放在 `%LOCALAPPDATA%\LCCRuntime`，通过目录联接访问固定 `venv` 入口。

Linux/macOS：运行 `bash create_sam_venv.sh`，默认 Python 和 venv 均在本目录；可用 `SAM_VENV_DIR` 自定义 venv 实体位置并创建符号链接。macOS Apple Silicon 在 Rosetta 下也选择 ARM64；Intel 当前缺少兼容的官方 Torch/SAM2 组合，会明确失败。

通用依赖默认清华镜像，`PIP_INDEX_URL` 可覆盖；Windows/Linux 的 `SAM_TORCH_*` 与 CUDA/CPU 回退策略保留。macOS Torch 默认官方 PyPI，禁用 CUDA 扩展，按 CPU 验证。安装联网并校验证书，外部模型准备齐全后可离线运行。

已有完整环境只验证；异常或未完成环境保留并报错，不删除、重装、修复、迁移或下载模型。安装使用简单锁，下载缓存复用前重新校验 SHA-256。

日志：Windows 为 `%LOCALAPPDATA%\LCCRuntime\logs`，Linux/macOS 为本目录 `logs`。界面日志为 `qgis-install-*.log`，入口日志为 `install-*.log`；失败和取消均保留日志。

本次 Windows CPU 环境、真实外部 bundle 和 SAM2 worker 已通过；Linux/macOS 未实测，CUDA 实机和整机断网重启 QGIS 尚未验证。完整资产列表、校验值、配置、错误处理和验收记录见 [安装文档](../../../docs/install.md)；开发调用关系及索引维护见 [开发文档](../../../docs/development.md)。
