# 运行时依赖安装

当前插件只维护一套独立子进程运行环境：

```text
land_cover_classification/vendor/sam_runtime/venv/
```

这套环境同时服务两个功能：

- PyTorch bundle 主推理
- SAM2 AI 辅助编辑

Windows 下脚本固定将 venv 实体放在 `%LOCALAPPDATA%\LCCRuntime\venv`，并在插件目录的 `vendor\sam_runtime\venv` 创建目录联接。这样可以避免 QGIS 默认插件路径过长导致 wheel 安装失败；插件代码和验证命令仍使用原有入口路径。

不要把 PyTorch、SAM2、rasterio、segmentation-models-pytorch 等重依赖安装到 QGIS 主进程 Python 中。QGIS 主进程只负责界面、图层、矢量化与导出。

## 一、部署插件目录

将仓库中的 `land_cover_classification/` 目录复制到 QGIS 插件目录。

Windows 默认目录：

```text
%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\land_cover_classification
```

Linux 默认目录：

```text
~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/land_cover_classification
```

## 二、创建插件统一运行环境

插件要求 QGIS 3.44+。安装入口优先使用预置包，否则下载 **python-build-standalone 完整 CPython 3.12.12，构建 20251014**，以该独立解释器创建不继承系统 site-packages 的 venv；不使用 embeddable ZIP，也不使用 QGIS 或系统 Python 创建环境。安装需要联网。模型由外部提供，准备齐全后推理可离线运行。

### 插件引导安装

启动插件、打开主面板时仅检查 venv 解释器文件是否存在，不导入重依赖，也不代表环境功能验收已通过；缺少时直接打开安装提示框，提供“开始安装”“复制手动安装命令”“关闭”。安装使用 QProcess 异步执行与手动方式相同的 `.bat` / `.sh`，显示阶段、连续输出和日志路径；安装过程中“关闭”变为“取消安装”。

成功后更新环境状态，不自动开始推理。失败或取消保留窗口、原始异常、退出码及已有目录，不触发修复或重装。日志可选择、复制；URL 中的代理和索引认证、路径及查询参数均脱敏。安装器不修改 QGIS 主进程的 Python 包路径或注册 QGIS DLL 目录。

### 手动安装

Windows 命令提示符（路径包含空格时保留引号）：

```bat
"<插件目录>\vendor\sam_runtime\create_sam_venv.bat"
```

Windows PowerShell：

```powershell
& '<插件目录>\vendor\sam_runtime\create_sam_venv.bat'
```

双击批处理或手动执行结束后保留窗口供查看。自动调用使用 `--non-interactive`，不等待按键。批处理内联调用系统 PowerShell，已取消独立的 `create_sam_venv.ps1`。

Linux / macOS 显式使用 Bash，无需脚本可执行位：

```bash
bash '<插件目录>/vendor/sam_runtime/create_sam_venv.sh'
```

安装逻辑只有三个主要实现文件：上述两个入口负责平台、下载、校验和安全解压；`runtime_setup.py` 负责 venv、依赖、功能验证及日志。不会创建后再搬迁 Python 或 venv。

### 固定 Python 下载链接

仓库预置下表中的 Windows x86_64 包，完整获取插件后无需再次下载该 CPython 归档。其他系统或预置包缺失时，如果网络不顺畅、安装器下载缓慢或反复中断，可使用专业下载器（如 迅雷等）下载。按当前系统和架构选择下表中的下载链接，下载完成后按下一节说明放入预置目录，再运行原安装入口。

以下四项已于 2026-09-08 对照 [GitHub 20251014 release 元数据](https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20251014) 逐项确认资产存在和官方 SHA-256，并固定在相应入口中。

| 系统与架构 | 官方下载链接（文件名） |
| --- | --- |
| Windows x86_64 | [cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-pc-windows-msvc-install_only.tar.gz) |
| Linux x86_64 | [cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-unknown-linux-gnu-install_only.tar.gz) |
| macOS Intel | [cpython-3.12.12+20251014-x86_64-apple-darwin-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-apple-darwin-install_only.tar.gz) |
| macOS Apple Silicon | [cpython-3.12.12+20251014-aarch64-apple-darwin-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-aarch64-apple-darwin-install_only.tar.gz) |

Windows 从系统注册表读取原生架构并检查操作系统位数；32 位进程运行在 x64 Windows 时仍选择 x64 包。macOS 使用 `hw.optional.arm64` 识别 Apple Silicon，Rosetta 下也选择 ARM64。真正 32 位系统明确报告 PyTorch/SAM2 不支持；Windows ARM64、Linux ARM64 和其他组合报告“当前安装脚本未提供该平台组合”。Linux 使用 glibc 包，解释器启动失败时保留原始错误，不额外安装系统库或改换架构。

### 预置 CPython 压缩包

使用下载器取得上表中的固定版本压缩包后，将其放入 `land_cover_classification/vendor/sam_runtime/cpython_packages/`（目录不存在时自行创建），保持官方原始文件名和字面量 `+`，不使用 URL 中的 `%2B`，也无需重命名为 SHA。多个系统的包可以共存，只选择当前系统与架构对应的精确文件名；其他版本、其他平台及未完成的 `.part` 文件不参与选择。

已有 venv 或独立 Python 仍按原有规则处理。仅在需要取得 CPython 归档时，先检查预置目录：匹配文件必须是普通文件，经固定 SHA-256 校验及归档安全检查后只读解压。本地包不移动、不改名、不删除；损坏、目录、符号链接或重解析点会明确报错并保留，不静默联网绕过。没有匹配文件时才进入原有下载缓存、完整 `.part` 复验和联网续传流程。安装锁、取消及异常环境保留策略不变，日志区分本地包、已有缓存和联网下载。

预置包仅跳过 CPython 下载；后续依赖安装仍使用现有来源和配置，需要联网。通用依赖的清华镜像和 `PIP_INDEX_URL`、Torch 来源及已有环境只验证行为不变。

预置目录允许提交固定 Windows 包 `cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only.tar.gz` 和空的 .gitkeep 占位文件；其他平台、其他版本的归档与临时文件仍由 Git 忽略。`pb_tool.cfg` 的 `extra_dirs: vendor models` 已覆盖本目录，无需修改构建配置。发布时默认携带已提交的 Windows 包，也可在干净的插件暂存目录中人工附带其他平台归档并打成 ZIP；`git archive` 会包含所选提交中的 Windows 包，被忽略的其他本地包需在发布暂存目录补入。不要将本机 venv、下载缓存或日志随包发布。

### 目录、重复执行与缓存

| 系统 | 独立 Python 最终位置 | venv 最终位置 | 固定入口 |
| --- | --- | --- | --- |
| Windows | `%LOCALAPPDATA%\LCCRuntime\python-3.12.12-20251014\python` | `%LOCALAPPDATA%\LCCRuntime\venv` | `<插件目录>\vendor\sam_runtime\venv` 目录联接 |
| Linux/macOS | `<sam_runtime>/python-3.12.12-20251014/python` | 默认 `<sam_runtime>/venv` | 自定义位置时创建符号链接 |

Linux/macOS 保留自定义 venv 路径：

```bash
SAM_VENV_DIR='/可写路径/独立环境' bash '<插件目录>/vendor/sam_runtime/create_sam_venv.sh'
```

Windows 固定存放在 LOCALAPPDATA，不接受 `SAM_VENV_DIR`。发现旧 `SAM_PYTHON` 时说明新策略并忽略该解释器；`SAM_RECREATE` 已取消，不再删除旧环境。已有完整 venv 只执行功能验证和 `pip check`，不重复安装。已有不完整或异常 venv 报告其路径及错误并停止，不复制旧 site-packages，不自动删除或修复。

CPython 下载在安装锁保护下使用固定 `downloads/<SHA-256>.tar.gz.part`；超时、断流或取消后保留文件，重新执行现有安装入口会从已保留字节续传。每次请求从固定 GitHub release URL 开始，最多尝试 3 次，失败后分别等待 2 秒、5 秒；连接超时 30 秒、单次传输最长 900 秒、持续 120 秒低于 1,024 字节/秒会超时。仅重试 curl 退出码 `5/6/7/18/28/52/55/56` 或 HTTP `408/429/500/502/503/504`；权限、磁盘、证书、其他 HTTP 和不支持续传的错误直接停止并保留诊断。

下载开始、失败、重试、取消和校验立即输出日志；传输期间每 10 秒输出累计 MiB、最近区间 KiB/s、本次耗时与尝试次数，无新数据时仍报告 `0 KiB/s` 和“正在等待数据”。不额外请求 HEAD 或显示总量百分比。取消在下载和退避期间约每 200 毫秒检查一次，等待 curl 完全退出后才释放本次锁。

已有正式缓存和 `.part` 都先校验 SHA-256；完整 `.part` 直接发布，实际传输完成后也只有校验通过才原子改名为 `<SHA-256>.tar.gz` 并进入归档检查和解压。下载路径是目录、符号链接或重解析点时明确失败。服务端忽略 Range 或返回 416 时重新校验，失败则保留且不退回覆盖下载；SHA 不匹配时需用户确认没有安装进程后移走异常文件再重新下载。旧版 `.part-<PID>` 不会自动迁移、合并或清理，新版只续传固定 `.part`。

解压前拒绝越界路径和不安全链接，保留 Unix 可执行权限；新 Python 目录未完成时不会被静默覆盖。

Windows 引导使用系统独占文件句柄锁 `standalone-3.12.12.lock`；Bash 引导使用 `.bootstrap-lock`；公共实现使用 venv 相邻的 `.venv.install-lock`（自定义 venv 时按目录名命名）。并发安装直接失败。强制结束进程或断电可能留下目录锁：先确认没有安装进程，再人工处理日志指出的具体锁；脚本不会猜测并清除锁。

### 依赖来源和设备策略

通用依赖默认使用清华镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`。已有 `PIP_INDEX_URL` 优先；例如：

```powershell
$env:PIP_INDEX_URL = 'https://pypi.org/simple'
& '<插件目录>\vendor\sam_runtime\create_sam_venv.bat'
```

```bash
PIP_INDEX_URL=https://pypi.org/simple bash '<插件目录>/vendor/sam_runtime/create_sam_venv.sh'
```

通用源出错时保留诊断，不自动更换。已有未完成 venv 不会因改了索引而自动补装。下载和 pip 继承已配置的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，保留 TLS 证书校验；可通过系统可信证书或工具支持的 CA 配置使用企业代理，不关闭校验。

Windows/Linux 保留 `SAM_TORCH_CPU_INDEX`（默认 `https://download.pytorch.org/whl/cpu`）、`SAM_TORCH_CUDA_INDEX`、`SAM_TORCH_CUDA_INDEXES`、`SAM_TORCH_PACKAGES`。检测 NVIDIA 环境后按驱动能力尝试 cu128、cu126、cu124、cu121、cu118；CUDA wheel 安装、加载或 GPU 张量运算失败后回退 CPU wheel。没有 NVIDIA 环境时直接使用 CPU。CUDA 和 CPU 均失败会终止，不保证所有机器都能成功安装。

macOS 不探测 CUDA，从官方 PyPI 安装平台匹配的 Torch/torchvision wheel，并关闭 SAM2 CUDA 扩展；显式 CUDA 源会报告不适用。当前设备选择仍为 CUDA/CPU，macOS 按 CPU 验收，没有增加 MPS 逻辑。

Torch 步骤清除通用 `PIP_*` 来源配置、禁用 pip 配置文件，使用 `--isolated` 和显式索引，仅接受 wheel，ABI/平台由 pip 选择。通用依赖安装使用已安装 Torch/torchvision 的精确约束及已有构建环境，禁止替换专用 wheel；最终再次检查版本及功能。Windows/Linux 只有检测到 `nvcc` 和 C/C++ 工具链且没有显式设置 `SAM2_BUILD_CUDA=0` 时启用 SAM2 CUDA 扩展。

**macOS Intel 当前没有完整官方 Torch/SAM2 组合。** [PyPI Torch 文件列表](https://pypi.org/pypi/torch/json) 中官方 macOS x86_64 wheel 最高为 2.2.2；当前 [SAM2 1.1.0 源包](https://pypi.org/project/sam2/1.1.0/) 要求 `torch>=2.5.1`、`torchvision>=0.20.1`。安装器明确报告此依赖冲突并保留 Python、venv 及日志，不安装旧 Torch、不降级到 SAM1。Python 资产可用不能据此标记完整支持。

### 日志和常见错误

| 情况 | 行为与日志 |
| --- | --- |
| Windows 安装日志 | `%LOCALAPPDATA%\LCCRuntime\logs\install-*.log`；界面额外保存 `qgis-install-*.log` |
| Linux/macOS 安装日志 | `<sam_runtime>/logs/install-*.log`；界面额外保存 `qgis-install-*.log` |
| GitHub 下载中断、代理或 TLS 错误 | 保留 curl 原始错误、退出码、部分下载路径；不创建成功缓存，不关闭 TLS 校验 |
| SHA-256 不匹配、越界归档 | 停止解压/安装，显示具体缓存或归档成员；不自动换版本 |
| Python/SSL/SQLite 启动错误 | 保留解释器路径、异常及堆栈，停止创建 venv |
| `No matching distribution`、`ResolutionImpossible` | 保留 pip 原始版本和冲突信息；不解除 Torch 约束或切换 SAM 后端 |
| 已有异常环境或目录冲突 | 显示目标和固定入口路径，保留原目录及模型 |
| 取消、并发安装 | 取消终止当前安装子进程，界面保留退出状态；并发锁失败不会清理其他安装 |

安装器对环境执行 SQLite 查询、SSL 初始化、NumPy 矩阵运算、Torch CPU 张量、Torchvision NMS 原生操作、无权重 SAM2 构建，以及 Rasterio 内存 GeoTIFF 写读和 EPSG:4326 → EPSG:3857 转换。CUDA 模式额外执行 GPU 张量运算。模型缺失不判定为环境安装失败。

手动重新执行同一入口即可检查完整环境。也可直接验收环境：

```powershell
& '<插件目录>\vendor\sam_runtime\venv\Scripts\python.exe' '<插件目录>\vendor\sam_runtime\runtime_setup.py' --functional-check
```

```bash
'<插件目录>/vendor/sam_runtime/venv/bin/python' '<插件目录>/vendor/sam_runtime/runtime_setup.py' --functional-check
```

```powershell
python land_cover_classification/test/test_cpython_download.py -v
```

## 三、准备 PyTorch Bundle

将训练仓导出的 bundle 子目录放入：

```text
land_cover_classification/models/semantic_segmentation/
```

该目录中的真实 bundle 由外部提供，默认被 `.gitignore` 忽略；远端仓库只保留 `.gitkeep` 是正常状态，不代表本机目录没有模型。当前开发工作区的验证 bundle 可位于 `landslide_mitb2_dem_50m_v1/` 子目录，但它仅供本机验证，不应提交到远端。

每个 bundle 至少包含：

- `manifest.json`
- `weights.pt`
- `arch.py`
- `dem_factors.py`

常见可选文件：

- `preprocess.json`
- `postprocess.json`
- `README.md`
详细 schema 见 [model_layout.md](model_layout.md)。缺少 manifest.json 的目录会被跳过，不显示在模型下拉框中。


## 四、准备 SAM AI 编辑资源

默认 SAM2 权重路径：

```text
land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt
```

请将 `sam2.1_hiera_base_plus.pt` 放到上述路径。SAM 权重不是 PyTorch 主推理的必需项；只运行语义分割时可以暂时不准备，但启动 AI 辅助编辑前必须存在。

## 五、首次运行

1. 重启 QGIS。
2. 启用 `LandCoverClassification` 插件。
3. 在“模型推理”页签选择输入影像和 PyTorch bundle；DEM 文件可不选，也可只覆盖目标范围的一部分，再执行全图推理。
4. 如需仅处理当前视图，可在“模型推理”页签使用“按当前画布范围推理”；该功能要求输入影像具有有效地理参考，且当前画布与影像存在有效交集。
5. 如需处理任意闭合范围，可在“模型推理”页签逐点绘制多边形，确认蓝色预览后点击“按绘制范围推理”。范围只在当前插件会话中保留，且同样要求输入影像具有有效地理参考。

输入文件可使用 JPEG、PNG、GeoTIFF、ENVI `.dat/.img` 或 ERDAS Imagine `.img`。`.ige` 只能作为同目录同名 `.img` 的入口。点击运行或启动 AI 编辑后，插件会分别验证 QGIS GDAL 与统一 runtime 的实际读取能力；runtime 缺少 ENVI/HFA 驱动时，QGIS 会以可取消任务转换为会话级 tiled BigTIFF。转换前会检查临时目录空间，原始文件不会被覆盖。

## 会话草稿说明

会话草稿不需要额外安装依赖，也不会创建第二套 runtime。选择影像只加载底图，不创建草稿层；首次 AI 追加有效 mask 或模型推理融合成功后才创建临时 GeoPackage generation。每次 AI 追加、手工提交或模型融合均会先生成并校验新 generation，成功后才切换可见草稿层，失败会保留上一版。启动插件时检查 venv 是否存在，缺少时立即引导安装；点击 AI 编辑或模型推理不再触发安装弹窗，执行功能时仍保留必要的依赖和输入验证。会话关闭后草稿不会恢复，请在关闭前导出需要保留的成果。
