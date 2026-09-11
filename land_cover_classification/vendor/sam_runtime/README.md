# 插件统一独立运行环境

本目录只保留三个主要实现文件：

- `create_sam_venv.bat`：Windows 入口，内联调用系统 PowerShell 获取固定 Python。
- `create_sam_venv.sh`：Linux/macOS 入口，显式通过 Bash 执行。
- `runtime_setup.py`：公共 venv 创建、依赖安装、功能验证、日志和环境清理。

使用 python-build-standalone **CPython 3.12.12 / 20251014** 完整发行包；不探测 QGIS/系统 Python，不再使用 `SAM_PYTHON` 或 `SAM_RECREATE`。两个入口的 URL 和 SHA-256 均固定。`requirements-sam.txt` 仅供参考，实际安装保留 Torch 专用源与精确约束。

Windows：双击或运行 `create_sam_venv.bat`；插件传入 `--non-interactive`。Python 和 venv 存放在 `%LOCALAPPDATA%\LCCRuntime`，通过目录联接访问固定 `venv` 入口。

Linux/macOS：运行 `bash create_sam_venv.sh`，默认 Python 和 venv 均在本目录；可用 `SAM_VENV_DIR` 自定义 venv 实体位置并创建符号链接。macOS Apple Silicon 在 Rosetta 下也选择 ARM64；Intel 当前缺少兼容的官方 Torch/SAM2 组合，会明确失败。

通用依赖默认清华镜像，`PIP_INDEX_URL` 可覆盖；Windows/Linux 的 `SAM_TORCH_*` 与 CUDA/CPU 回退策略保留。macOS Torch 默认官方 PyPI，禁用 CUDA 扩展，按 CPU 验证。安装联网并校验证书，外部模型准备齐全后可离线运行。

已有完整环境只验证；异常或未完成环境保留并报错，不删除、重装、修复、迁移或下载模型。安装使用简单锁，下载缓存复用前重新校验 SHA-256。

需要取得 CPython 归档时，先读取本目录的 `cpython_packages/`：只选择当前系统与架构对应的官方原始文件名，使用字面量 `+`，允许多个平台包共存。固定版本和 SHA 不变；普通本地文件通过 SHA-256 与归档安全检查后只读使用，不移动、改名或删除。匹配包损坏、为目录、符号链接或重解析点时停止并保留，不联网绕过；没有匹配文件才检查下载缓存、完整 `.part` 和联网续传。其他版本、其他平台、未完成文件不参与选择。已有环境与独立 Python 分支仍优先按原规则处理。

下载方式和各系统对应链接统一见 [安装文档](../../../docs/install.md#固定-python-下载链接)。预置目录提交固定 Windows x86_64 包和 .gitkeep，其他平台、版本的归档与临时文件仍受 Git 忽略；`pb_tool.cfg` 的 `vendor` 打包目录包含它，发布者可在干净暂存目录人工附带包。`git archive` 会携带所选提交中的 Windows 包；其他被忽略的归档需另行加入发布 ZIP。预置包只免去 CPython 下载，依赖安装来源及联网要求不变。

CPython 下载在安装锁保护下使用固定 `downloads/<SHA-256>.tar.gz.part`；超时、断流或取消后保留文件，重新执行现有安装入口会从已保留字节续传。每次请求从固定 GitHub release URL 开始，最多尝试 3 次，失败后分别等待 2 秒、5 秒；连接超时 30 秒、单次传输最长 900 秒、持续 120 秒低于 1,024 字节/秒会超时。仅重试 curl 退出码 `5/6/7/18/28/52/55/56` 或 HTTP `408/429/500/502/503/504`；权限、磁盘、证书、其他 HTTP 和不支持续传的错误直接停止并保留诊断。

下载开始、失败、重试、取消和校验立即输出日志；传输期间每 10 秒输出累计 MiB、最近区间 KiB/s、本次耗时与尝试次数，无新数据时仍报告 `0 KiB/s` 和“正在等待数据”。不额外请求 HEAD 或显示总量百分比。取消在下载和退避期间约每 200 毫秒检查一次，等待 curl 完全退出后才释放本次锁。

已有正式缓存和 `.part` 都先校验 SHA-256；完整 `.part` 直接发布，实际传输完成后也只有校验通过才原子改名为 `<SHA-256>.tar.gz` 并进入归档检查和解压。下载路径是目录、符号链接或重解析点时明确失败。服务端忽略 Range 或返回 416 时重新校验，失败则保留且不退回覆盖下载；SHA 不匹配时需用户确认没有安装进程后移走异常文件再重新下载。旧版 `.part-<PID>` 不会自动迁移、合并或清理，新版只续传固定 `.part`。

日志：Windows 为 `%LOCALAPPDATA%\LCCRuntime\logs`，Linux/macOS 为本目录 `logs`。界面日志为 `qgis-install-*.log`，入口日志为 `install-*.log`；失败和取消均保留日志。

历史验收（2026-09-08）的 Windows CPU 环境、真实外部 bundle 和 SAM2 worker 已通过；Linux/macOS 未实测，CUDA 实机和整机断网重启 QGIS 尚未验证。完整资产列表、校验值、配置、错误处理和验收记录见 [安装文档](../../../docs/install.md)；开发调用关系及索引维护见 [开发文档](../../../docs/development.md)。
