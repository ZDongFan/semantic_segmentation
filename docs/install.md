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

插件要求 QGIS 3.44+。安装入口下载 **python-build-standalone 完整 CPython 3.12.12，构建 20251014**，以该独立解释器创建不继承系统 site-packages 的 venv；不使用 embeddable ZIP，也不使用 QGIS 或系统 Python 创建环境。安装需要联网。模型由外部提供，准备齐全后推理可离线运行。

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

### 固定 Python 资产

以下四项已于 2026-09-08 对照 [GitHub 20251014 release 元数据](https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20251014) 逐项确认资产存在和官方 SHA-256，并固定在相应入口中。

| 平台 | 资产 | SHA-256 |
| --- | --- | --- |
| Windows x86_64 | [cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-pc-windows-msvc-install_only.tar.gz) | `2d670beb3b930d30e3a13cc909923a001dbdfcb5537692d5da40b6b41643ce1c` |
| Linux x86_64 | [cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-unknown-linux-gnu-install_only.tar.gz) | `1ab2b6594d1c3d76cbebea09d6bc3e6ba68d8eb3b6322080375c4cc3dd188f34` |
| macOS Intel | [cpython-3.12.12+20251014-x86_64-apple-darwin-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-x86_64-apple-darwin-install_only.tar.gz) | `9b8589eefb153cbe7cb652993d0ecc94aeb2fa13c1a2e8bc240f5f74f23bb21b` |
| macOS Apple Silicon | [cpython-3.12.12+20251014-aarch64-apple-darwin-install_only.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.12.12%2B20251014-aarch64-apple-darwin-install_only.tar.gz) | `6ceba34fe78802853a30bde6f303a0a54f71f6ab07a673da34e90c0aa06c786e` |

Windows 从系统注册表读取原生架构并检查操作系统位数；32 位进程运行在 x64 Windows 时仍选择 x64 包。macOS 使用 `hw.optional.arm64` 识别 Apple Silicon，Rosetta 下也选择 ARM64。真正 32 位系统明确报告 PyTorch/SAM2 不支持；Windows ARM64、Linux ARM64 和其他组合报告“当前安装脚本未提供该平台组合”。Linux 使用 glibc 包，解释器启动失败时保留原始错误，不额外安装系统库或改换架构。

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

下载先写入 `.part-<PID>`，通过 SHA-256 才成为 `downloads/<SHA-256>.tar.gz` 缓存；重试复用校验通过的缓存。缓存损坏时明确失败。解压前拒绝越界路径和不安全链接，保留 Unix 可执行权限；新 Python 目录未完成时不会被静默覆盖。

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

### 提交附带的验收记录（2026-09-08）

以下为提交 `8f0f84cc` 附带的历史验收记录，不表示每次更新文档或索引后重新执行过这些测试。本地测试资产与已提交测试应分别核对，开发入口及复验建议见 [development.md](development.md)。

| 平台 | 独立 Python 安装 | 完整依赖与外部模型 |
| --- | --- | --- |
| Windows x86_64 | 实际 3.12.12 启动、SSL/SQLite、创建 venv、目录联接均通过；下载曾连接重置，使用此前实际下载且 SHA-256 通过的缓存完成 | CPU 完整功能验收通过；Torch 2.14.0+cpu、torchvision 0.29.0+cpu、SAM2 1.1.0、Rasterio 1.5.1；真实 bundle 和 SAM2 交互通过 |
| Linux x86_64 | 官方资产/SHA 已核对；目标系统未实测 | 未验证 |
| macOS Intel | 官方资产/SHA 已核对；目标系统未实测 | 官方依赖无交集；明确失败分支在本机模拟测试通过，目标系统未验证 |
| macOS Apple Silicon | 官方资产/SHA 已核对；目标系统未实测 | 未验证，待 macOS CPU 实测 |

本机安装器 19 项回归测试通过，覆盖原生架构选择、Rosetta 模拟、归档越界/链接、环境清理、凭据脱敏、并发锁、静默子进程取消、已有异常环境保留、精确版本约束和 CUDA 失败回退（模拟）。已有主推理输入适配 4 项测试通过。另实际验证了损坏缓存失败、pip 无匹配 wheel/冲突（dry-run）、中文及空格路径联接和重复确认，以及两个真实 Windows 入口并发时第二个被锁拒绝、首个正常完成。

实际 QGIS 中验证了 QProcess 启动、实时日志、完整环境重复执行和取消；安装成功没有自动推理。使用外部 `landslide_mitb2_dem_50m_v1` 与 `sam2.1_hiera_base_plus.pt`，对中文及空格路径的 128×128 测试影像完成语义分割及 SAM 正负点交互。新 worker 进程通过审计钩子禁止 Python 网络连接，仍成功运行。**这不等于整机断网后重启 QGIS 的验收**：后者尚未执行；CUDA 实机、其他操作系统、真实 Rosetta 和 32 位宿主进程也尚未实测。

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
