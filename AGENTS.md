# AGENTS.md

`AGENTS.md` 与 `CLAUDE.md` 需要同步维护，尤其是 PyTorch 主推理、统一 runtime/CUDA 安装策略、DEM 因子与后处理、AI 编辑行为、导出流程、验证建议和协作约定。

## 项目概览

当前仓库是一个面向 **QGIS 3.44+** 的语义分割插件项目，仓库根目录主要放说明文档，真正的插件代码位于 `land_cover_classification/` 目录。

- 仓库目录名：`new_semantic_segmentation`
- 插件目录名：`land_cover_classification`
- 插件显示名：`LandCoverClassification`
- 主要用途：在 QGIS 中对栅格图层或本地影像执行基于 PyTorch bundle 的遥感语义分割，并结合 DEM 派生因子进行规则后处理；结果组织为“类别栅格 -> 草稿矢量 -> 最终成果”的人工编辑流程。推理生成草稿层后，可使用 SAM2.1 Base+ 进行 AI 辅助编辑并写回草稿层。

## 主要技术栈与运行环境

- 语言：Python
- 宿主：QGIS 3
- 最低 QGIS 版本：`3.44`
- UI：PyQt / QGIS PyQt
- 主推理框架：PyTorch bundle，运行在插件统一 venv 子进程中
- DEM 后处理：rasterio 重投影 / 对齐、bundle 内 `dem_factors.py` 派生因子、阈值化后的形态学合并 / 填孔 / 平滑 / 去小斑、规则过滤与审计 JSON
- 大影像处理：生产推理采用“核心区 + halo”窗口化磁盘流，影像、DEM、概率和后处理栅格不得整图驻留内存；生产 GeoTIFF 统一使用 tiled BigTIFF
- AI 辅助编辑：SAM2.1 Base+ + PyTorch + sam2，运行在同一个插件统一 venv 子进程中
- 影像处理：OpenCV、NumPy、GDAL / OSGeo、rasterio
- 模型配置解析：JSON、PyYAML
- 矢量输出：QGIS Vector API + GDAL/OGR
- 打包方式：`pb_tool.cfg`、`Makefile`

插件不把 PyTorch、SAM2、rasterio、segmentation-models-pytorch 等重依赖安装到 QGIS Python 环境，而是统一安装到：

`land_cover_classification/vendor/sam_runtime/venv/`

这套 venv 同时服务：

- `pytorch_inference_runner.py`：PyTorch bundle 主推理
- `sam_worker.py`：SAM2 AI 辅助编辑

`vendor/sam_runtime/create_sam_venv.bat` / `create_sam_venv.sh` 是这套环境的唯一创建入口，下载固定的 python-build-standalone 完整 CPython 3.12.12（构建 20251014），校验固定 URL 对应的 SHA-256 后安全解压，内部复用 `runtime_setup.py` 创建 venv、安装依赖并验证。Windows 批处理内联调用系统 PowerShell，独立 `create_sam_venv.ps1` 已移除；不探测或使用 QGIS/系统 Python，`SAM_PYTHON` 仅提示后忽略，`SAM_RECREATE` 已取消。Windows 固定将 Python 和 venv 放在 `%LOCALAPPDATA%\LCCRuntime`，在插件内建立 venv 目录联接，拒绝 `SAM_VENV_DIR`；Linux/macOS 通过 Bash 执行，默认放在 runtime 目录，仅这两个平台支持 `SAM_VENV_DIR` 与符号链接。创建结果必须满足 `include-system-site-packages=false`；已有完整环境只验证，异常或未完成目录保留并报错，不自动删除、重装、修复或迁移，也不下载模型。下载缓存复用前重新校验 SHA-256，安装保留并发锁、取消和脱敏日志。

Windows/Linux 保留 CUDA wheel 候选与 CPU 回退；Apple Silicon 从官方 PyPI 安装 macOS ARM64 Torch wheel、禁用 SAM2 CUDA 扩展并按 CPU 验证。Apple Silicon 在 Rosetta 下也选择 ARM64；macOS Intel 当前没有兼容的官方 Torch/SAM2 wheel 组合，必须明确失败。固定资产、支持架构和平台实测边界见 `docs/install.md`；不得将文档中的本机验收版本当作通用依赖前提。

通用依赖默认从清华 PyPI 镜像 `https://pypi.tuna.tsinghua.edu.cn/simple` 安装，用户的 `PIP_INDEX_URL` 优先；镜像失败不静默切换，也不在日志输出自定义 URL 凭据。Windows/Linux 的 Torch/torchvision 默认从 PyTorch 官方 CPU/CUDA wheel 索引安装，并保留 `SAM_TORCH_CPU_INDEX`、`SAM_TORCH_CUDA_INDEX`、`SAM_TORCH_CUDA_INDEXES` 和 `SAM_TORCH_PACKAGES` 配置；检测 NVIDIA 环境后按驱动能力尝试 CUDA 候选，安装、加载或 GPU 张量验证失败后回退 CPU。Torch 安装清除通用 `PIP_*` 来源配置、禁用 pip 配置文件并使用显式索引与 wheel-only 约束；macOS 使用官方 PyPI。安装 SAM2 等通用依赖时必须用精确版本约束和已有构建环境防止重新获取或替换 PyTorch，并校验安装前后的 torch / torchvision 版本、CUDA 构建及可用性。只有同时检测到 `nvcc` 和 C/C++ 编译工具链且未显式禁用时，才允许 `SAM2_BUILD_CUDA=1`。

PyTorch 主推理依赖以 `land_cover_classification/pytorch_deps_check.py` 为准，当前检查的依赖包括：

- `torch`
- `torchvision`
- `segmentation-models-pytorch`（导入模块 `segmentation_models_pytorch`）
- `timm`
- `rasterio`
- `opencv-contrib-python-headless`（导入模块 `cv2`）
- `numpy`
- `scipy`
- `PyYAML`（导入模块 `yaml`）

SAM AI 辅助编辑依赖以 `land_cover_classification/sam_deps_check.py` 为准，只在启动 AI 编辑时检查。当前依赖包括：

- `torch`
- `torchvision`
- `sam2`
- `opencv-contrib-python`（导入模块 `cv2`）
- `numpy`

在 QGIS 进程中启动 PyTorch 主推理或 SAM 子进程时必须使用清洁环境，避免继承 `PYTHONHOME`、`PYTHONPATH`、`PYTHONUSERBASE`、`QGIS_PREFIX_PATH` 等 QGIS/OSGeo4W 环境变量。

`pytorch_deps_check.py` 中 CUDA 探测和依赖导入探测是两条诊断路径：CUDA 不可用只意味着推理回退 CPU，不应误报为依赖缺失；重依赖导入超时应提示用户运行 `pytorch_deps_check.py --json` 继续诊断。

## 仓库结构

### 根目录

- `README.md`：项目总说明
- `AGENTS.md`：Codex 协作与项目约定
- `CLAUDE.md`：Claude 协作与项目约定
- `docs/install.md`：依赖安装说明
- `docs/model_layout.md`：模型目录结构说明
- `land_cover_classification/`：QGIS 插件主体目录

### 插件目录 `land_cover_classification/`

- `__init__.py`：QGIS 插件入口，定义 `classFactory(iface)`
- `land_cover_classification.py`：插件主类，负责菜单、工具栏和对话框入口；打开面板后调用 `check_runtime_on_startup()`，只检查 venv 解释器是否存在，缺少时显示安装引导
- `land_cover_classification_dialog.py`：主对话框逻辑，负责模型 / 输入 / DEM 选择、全图或画布范围推理、推理进度、会话草稿编辑与导出交互；内含 `RuntimeInstallDialog`，通过 QProcess 异步执行安装入口并显示日志
- `raster_input_adapter.py`：统一处理 ENVI/ERDAS 路径、GDAL 能力探测、runtime 有限窗口探测、会话级 BigTIFF fallback 和转换缓存
- `land_cover_classification_dialog_base.ui`：Qt Designer 维护的 UI 文件
- `workflow_ui.py`：工作区布局、按钮条件、摘要及草稿显示控制
- `attribute_browser.py`：非模态只读成果属性、地图点选与分页浏览
- `history_dialogs.py`：已发布版本对比、临时地图叠加和恢复管理窗口
- `recovery.py`：成功 generation 的身份记录、恢复校验、恢复前备份及原子切换
- `pytorch_inference_core.py`：PyTorch bundle 加载、有限块回归工具、DEM 对齐、规则后处理和 GeoTIFF profile；生产入口委托窗口化流式实现
- `pytorch_inference_runner.py`：独立 PyTorch 推理子进程入口
- `pytorch_deps_check.py`：PyTorch 主推理依赖检查与安装提示，复用插件统一 venv

- `model_scan.py`：扫描可用 PyTorch `manifest.json` bundle，缺少或无效的 manifest 会被跳过
- `inference_controller.py`：启动前校验 bundle 的唯一 `landslide` 类别契约，并保存本次 `run_id` 与类别索引
- `draft_session.py`：临时 GeoPackage generation 会话、草稿合成、局部推理融合与草稿字段规范化
- `export_service.py`：从当前 generation 导出 0/1 tiled BigTIFF、公开字段 Shapefile 或 DXF
- `ai_edit_controller.py`：AI 编辑会话状态和临时预览控制



- `sam_deps_check.py`：SAM2 模型权重和依赖检查，复用插件统一 venv
- `sam_worker.py`：SAM2 独立子进程入口，使用 JSON line 协议通信
- `ai_segment_tool.py`：AI 辅助编辑地图交互工具
- `metadata.txt`：QGIS 插件元数据
- `pb_tool.cfg`：插件打包配置
- `resources.qrc` / `resources.py`：Qt 资源文件
- `models/semantic_segmentation/`：默认 PyTorch bundle 模型根目录
- `models/sam2/`：SAM2.1 默认权重目录

- `vendor/sam_runtime/`：插件统一 venv 创建脚本、requirements 与运行环境说明
- `test/`：插件相关测试；`test_draft_generation_publish.py`、`test_inference_controller.py`、`test_roi_streaming.py` 分别覆盖 generation 发布回滚、bundle 类别契约和 ROI 流式边界

## 运行与开发约定

- 当前本机开发环境已将本仓库的 `land_cover_classification/` 目录软连接到 QGIS profile 的插件目录；调试插件时只修改仓库内源码和模型 bundle 即可，不要再单独改 profile 下的副本，除非先确认软连接失效。
- 本项目按通用 QGIS 插件开发，不得把本机环境、单个用户数据路径、临时 venv 状态或某一台机器安装的第三方库版本作为实现前提；禁止通过硬编码绝对路径、固定本机依赖状态、只改本机默认模型等方式绕过通用问题。
- 遇到第三方库 API 差异时（例如 `segmentation_models_pytorch` decoder 调用约定变化）应优先通过 bundle 自描述、能力检测、签名检测、版本范围声明或清晰的兼容适配层解决；不能只针对当前机器的已安装版本写死调用方式。
- 如果某个差异无法通用兼容，必须在依赖检查、错误提示和文档中明确支持的版本范围与失败原因；不要让用户只看到底层 Python 异常。
- 修改默认 PyTorch bundle 的 `arch.py`、`dem_factors.py`、`manifest.json`、`preprocess.json`或 `postprocess.json` 时，不得只服务于当前本机 venv；必须同时考虑训练导出约定、插件消费端兼容、模型目录文档和回归测试。
- 当前推理侧只接受 v3 显式米制 DEM 因子契约：`postprocess.json.dem_factors` 必须与 `dem_factors.py.FACTOR_NAMES` 完全一致，`tpi` / `relief` 必须声明 `scale_mode: "meters"`、正数 `window_m` 和 `unit: "m"`；旧的隐式固定像素窗口或只写规则阈值的 bundle 必须直接失败。
- `land_cover_classification/models/semantic_segmentation/*` 和 `land_cover_classification/models/sam2/*` 默认被 `.gitignore` 忽略，只保留 `.gitkeep`。训练端导出的真实 bundle、`weights.pt`、SAM 权重和 `__pycache__` 只能作为本机验证资产，不要当作通用源码提交；需要固化约定时应更新文档、示例 schema 或测试。
- 模型资产由外部提供。本机工作区的模型根目录即使在远端仓库或 Git 追踪结果中只显示 `.gitkeep`，也不能据此判定目录为空；处理模型参数、后处理或推理问题前，必须先枚举 `land_cover_classification/models/semantic_segmentation/` 和 `land_cover_classification/models/sam2/` 下实际存在的本机文件。
- 当前工作区用于验证的外部 PyTorch bundle 位于 `land_cover_classification/models/semantic_segmentation/landslide_mitb2_dem_50m_v1/`。它是本机外部资产，可用于读取实际 `postprocess.json` 等配置；不得将此目录名作为代码硬编码默认值，也不得提交其中的权重或 bundle 文件。
- 本机样例数据、临时裁剪影像和 smoke test 输出只能用于验证；不得写入插件通用逻辑、默认配置或协作文档作为固定路径或默认行为。
- 不要在 QGIS 主进程内 `import torch`、`import sam2`、`import rasterio` 或 `import segmentation_models_pytorch` 等重依赖；主推理和 SAM 都应放在独立子进程中运行。
- PyTorch 生产推理必须经由 `pytorch_inference_core.run_inference()` 的窗口化流式入口；不得在生产路径调用整图 `read_image()`、`sliding_window_predict()` 或内存版 `apply_postprocess()`。后两者仅用于有限输入的回归测试，不能以提高上限的方式绕过保护。
- 生产中间栅格、类别栅格和编辑后导出的 GeoTIFF 必须复用统一的 tiled BigTIFF profile / creation options；不得恢复经典 TIFF 或整图写出以迁就单个样例。
- 修改 PyTorch runner 或对话框进程通信时，要保留 `LCC_EVENT error` 的异常链 `details`、`traceback`、分片 stdout 缓冲与进程结束前的尾部解析，并把完整诊断写入 QGIS `QgsMessageLog`。
- `pytorch_streaming.py` 是生产 PyTorch 推理的实现入口：必须保持“核心区 + halo”窗口、局部 DEM 对齐、概率 / 形态学 / 连通域 / 审计栅格的磁盘流边界，不得退回整图内存实现。
- 画布范围局部推理的 canonical ROI 必须使用输入影像 CRS 的 `canvas_intersection` 及 `[xmin, ymin, xmax, ymax]`。先将画布范围转换到影像 CRS 并与影像求交，再向外取整到像素窗口；没有有效像素交集必须失败，严禁回退为全图推理。核心窗口只能位于 ROI 内，halo 可超出 ROI 读取上下文；ROI 外概率与辅助掩膜必须显式保持 NoData / 无效，审计 JSON 必须记录模式、边界、CRS、像素窗口和 halo。
- 局部推理融合只能替换 ROI 内 `origin=inference`：保留 ROI 外既有 inference，并始终保留 `origin=user` 的 AI 和手工要素。不得把画布范围、AI 负点、用户删除记录或拒绝区作为持久化状态。
- PyTorch runner 的 JSON line 协议输出必须固定使用 UTF-8，并保持 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8:backslashreplace`、`GDAL_FILENAME_IS_UTF8=YES` 与 `CPL_DEBUG=OFF`；不要让 GDAL 原生日志或文件名编码污染协议流。
- 对话框应先以字节缓冲 stdout 再逐行解码（UTF-8 优先并回退 Windows 本地编码）；`BoundedDiagnostics` 必须去重且限制为 256 行 / 64 KiB，并记录省略统计后写入 QGIS `QgsMessageLog`。
- 流式后处理的进度必须在 88%–98% 单调推进，事件包含 `stage` / `status`，并抑制同一整百分比的重复事件；改动时应覆盖窗口 / halo、进度限流、拆分 stdout、残留半行及诊断限额测试。
- 修改 DXF 视图范围时，必须按 DXF 组码对流式改写并经临时文件原子替换；不得重新引入整文件 `read().splitlines()` 的高峰内存实现。
- 插件只维护一套运行环境：`vendor/sam_runtime/venv/`。不要重新引入第二套 PyTorch runtime。
- 需要取得 CPython 归档时，两个创建入口先检查 `vendor/sam_runtime/cpython_packages/` 下当前平台的精确官方文件名（字面量 `+`）；版本、构建与固定 SHA 不变。匹配包必须为普通文件，通过 SHA-256 和归档安全检查后只读使用；损坏、目录、链接或重解析点必须失败并保留，不联网绕过。没有匹配包才进入现有缓存、完整 `.part` 与联网续传；其他平台、版本、未完成文件不参与选择。已有环境或独立 Python 仍按原规则处理。下载说明及各系统链接统一维护在 docs/install.md，预置目录允许提交 .gitkeep 与固定 Windows 包 cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only.tar.gz；其他平台、版本的归档和临时文件仍忽略，发布暂存目录可人工附带。
- CPython 下载必须在安装锁保护下写入固定 `<SHA-256>.tar.gz.part`，断流、超时及取消保留并支持跨次续传；最多 3 次显式尝试，退避 2/5 秒，每 10 秒输出增量速度和累计大小，零进展也输出。取消约每 200 毫秒检查，等待 curl 退出后释放锁。缓存与完整 `.part` 均需 SHA-256 校验后才发布或解压；Range 不支持、416 且校验失败、SHA 不匹配均停止保留，不覆盖下载。拒绝临时路径链接/重解析点/目录；旧 `.part-<PID>` 不自动迁移或清理。
- 打开插件面板时通过 `check_runtime_on_startup()` / `pytorch_deps_check.venv_ready()` 仅检查解释器文件是否存在；缺少时显示安装引导，用户点击“开始安装”才启动 QProcess。不得在 QGIS 主进程导入 PyTorch / SAM / rasterio 等重依赖，也不得将存在性检查视为依赖健康验证。安装窗口复用手动入口，保留 UTF-8 增量解码、完整行脱敏、实时日志、退出状态及协作取消；成功只更新状态，不自动推理。点击推理或 AI 编辑不再弹安装窗口，缺少环境时提示重新打开插件；推理基础参数校验后仍检查 venv，依赖导入诊断通过独立子进程或 `pytorch_deps_check.py --json` 完成。
- SAM AI 辅助编辑依赖仍只在启动 AI 编辑时通过 `sam_deps_check.ensure_ready()` 检查。
- 修改统一运行环境依赖或创建脚本时，要同时检查 `vendor/sam_runtime/create_sam_venv.bat`、`create_sam_venv.sh`、`runtime_setup.py`、`vendor/sam_runtime/README.md`、`pytorch_deps_check.py`、`sam_deps_check.py`、`land_cover_classification.py`、`land_cover_classification_dialog.py` 和 `docs/install.md`；保留 `SAM_TORCH_CPU_INDEX`、`SAM_TORCH_CUDA_INDEX`、`SAM_TORCH_CUDA_INDEXES`、`SAM_TORCH_PACKAGES`、Windows/Linux 官方 CUDA 候选与 CPU 回退、`PIP_INDEX_URL` 及 Linux/macOS 的 `SAM_VENV_DIR`，不得恢复已取消的解释器发现或 `SAM_RECREATE` 删除流程。两个依赖检查模块统一复用 `runtime_setup.clean_environment()`，清除 Python/GDAL/PROJ/Qt/动态库环境变量及 PATH 中的 QGIS/OSGeo4W 项；安装器模块被 QGIS 导入时必须保持标准库级轻量。
- 修改 PyTorch 主推理、DEM 因子、后处理形态学、模型扫描或运行环境检查时，要同时检查 `pytorch_inference_core.py`、`pytorch_inference_runner.py`、`pytorch_deps_check.py`、`model_scan.py`、`land_cover_classification_dialog.py` 和 `land_cover_classification/test/test_rules_synthetic.py`。
- 修改草稿发布、AI / 手工编辑融合、局部推理或导出时，要同时检查 `draft_session.py`、`ai_edit_controller.py`、`export_service.py`、`inference_controller.py`、`land_cover_classification_dialog.py`、`test/test_draft_generation_publish.py`、`test/test_inference_controller.py` 与 `test/test_roi_streaming.py`。
- 修改 PyTorch bundle 约定时，要同步检查 `docs/model_layout.md`、`README.md`、`docs/install.md`、`pb_tool.cfg`、`AGENTS.md` 和 `CLAUDE.md`。
- 修改 SAM worker 协议时，要同时检查 `sam_worker.py`、`sam_deps_check.py` 和 `land_cover_classification_dialog.py`。
- 修改 AI 点提示逻辑时，要保留 crop 裁剪、多候选筛选、`low_res_masks` 迭代细化、活动 crop 复用、crop 外负点过滤、异常结果兜底重跑和负点硬约束。
- 修改 AI 编辑与画布交互时，要保留“回到 AI 点选模式”的恢复入口，以及按当前画布可见范围估算 `scale_factor` 的逻辑。
- AI 点提示在 `canvasReleaseEvent()` 中添加并触发预测；不得在鼠标按下回调中同步等待预测，避免事件重入后画布按下状态卡住并拦截 E 键。回归测试应通过真实画布分发鼠标与键盘事件。
- 修改 AI 几何 refine 逻辑时，要保留最大单面、像素尺度简化、平滑、`makeValid`、顶点预算、面积变化预算、负点覆盖检测和逐级回退。
- 启动 PyTorch 主推理 venv Python 时必须使用 `pytorch_deps_check.runtime_environment()` 的清洁环境，避免继承 QGIS 的 `PYTHONHOME` / `PYTHONPATH` 导致版本串扰。
- 启动 SAM venv Python 时必须使用 `sam_deps_check.runtime_environment()` 的清洁环境，避免继承 QGIS 的 `PYTHONHOME` / `PYTHONPATH` 导致版本串扰。
- ENVI `.dat/.img` 与 ERDAS Imagine `.img` 必须按 GDAL 实际 `ShortName` 识别；`.ige` 只定位同名 `.img`。原始源路径保持 canonical，fallback tiled BigTIFF 仅作为 `runtime_input_path`，转换缓存须在切换影像或关闭插件时清理。
- `preprocess.json.image_bands` 与 `sam_rgb_bands` 均使用 1-based 波段编号；主推理仍按 core+halo 有限窗口读取，SAM 只在活动 crop 内读取、掩膜 NoData 并执行高位深稳健拉伸。
- 插件只支持 PyTorch `manifest.json` bundle；缺少或无效 manifest 的目录不会显示在模型下拉框中。

## 模型目录约定

默认语义分割模型根目录为：

`land_cover_classification/models/semantic_segmentation/`
真实 PyTorch bundle 由外部提供，默认被 `.gitignore` 忽略；远端仓库只保留 `.gitkeep` 是预期行为，不代表本机目录没有模型。排查模型配置或推理行为时，必须先检查该目录下实际存在的本机 bundle。当前工作区的验证 bundle 为 `landslide_mitb2_dem_50m_v1/`，但该名称仅用于本机验证，不得写入通用代码或提交到远端。


每个可用模型必须是独立 PyTorch bundle 子目录，至少包含：

- `manifest.json`
- `weights.pt`
- `arch.py`
- `dem_factors.py`

可选配置文件包括：

- `preprocess.json`
- `postprocess.json`
- `README.md`

`manifest.json` 必须声明 `framework: "pytorch"`、`task: "semantic_segmentation"`；`schema_version` 可以存在，但推理侧不再用它决定新旧兼容性。`arch.py` 必须提供 `build_model(cfg)`，`dem_factors.py` 必须声明 `FACTOR_NAMES = ["slope", "aspect_sin", "aspect_cos", "tpi", "relief"]` 并提供 `compute_factors(dem_array, transform, dem_factors=..., crs_unit=...)`。推理时 DEM 文件为可选；未提供 DEM 使用归一化零值中性输入，部分覆盖按有效掩膜混合真实因子与中性输入。插件会将有效 DEM 对齐到输入影像网格后，按 `postprocess.json.dem_factors` 的显式契约计算派生因子；DEM 规则仅在组件有效覆盖率达到 50% 时执行，阈值化、形态学和最小面积处理始终保留。

训练端当前实际验证过的 bundle 形态包括 `landslide_mitb2_dem_v3`：`manifest.json` 可记录 `backbone: "mit_b2"`、`decoder: "segformer_mlp"`、`threshold: 0.6`、`training_data` 和评估指标；`dem_factors.py` 必须通过 `FACTOR_NAMES` 明确输出通道，例如 `["slope", "aspect_sin", "aspect_cos", "tpi", "relief"]`；`preprocess.json` 可用 `active_dem_channels` 启用参与模型输入的通道。消费端必须尊重 `FACTOR_NAMES`、`dem_channels`、`active_dem_channels` 与 `postprocess.json.dem_factors`，不要按旧的默认通道顺序或固定像素窗口硬编码。

`postprocess.json` 必须包含 `dem_factors`、`training_data`、`rules`，并可配置阈值、闭运算合并、填孔、边界平滑、开运算去噪、最小面积过滤和规则过滤。规则即使禁用也必须声明 `enabled`、`factor`、`stat`、`operator` 以及语义阈值字段：`slope_min_deg`、`relief_min_m` 或 `tpi_max_ridge`。当前实测配置可以关闭 slope / relief / tpi 规则，仅使用 `morph_closing`、`fill_holes`、`smooth_boundary`、`min_area_m2` 等形态学后处理改善连通、填洞和去小斑效果。`<output>.postprocess.json` 会记录 `dem_factors`、`training_data`、运行时分辨率、分辨率差异告警、规则契约、形态学计数、component 保留 / 丢弃决策和规则触发详情。

插件统一 runtime 目录为：

`land_cover_classification/vendor/sam_runtime/`

其中 `create_sam_venv.bat` / `create_sam_venv.sh` 下载固定独立 Python 并创建本机专用 venv，安装 PyTorch 主推理和 SAM2 AI 编辑依赖；Linux/macOS 显式使用 `bash create_sam_venv.sh`。重复执行仅验证已有环境，完整功能验收使用 venv Python 执行 `runtime_setup.py --functional-check`，包含 SQLite、SSL、Torch/Torchvision 原生操作、无权重 SAM2 构建及 Rasterio 写读/坐标转换，不要求模型权重。依赖和模型资源还可用以下命令分别检查：

Windows：

```bat
.\land_cover_classification\vendor\sam_runtime\venv\Scripts\python.exe land_cover_classification\pytorch_deps_check.py --json
.\land_cover_classification\vendor\sam_runtime\venv\Scripts\python.exe land_cover_classification\sam_deps_check.py --backend sam2
```

Linux / macOS：

```bash
land_cover_classification/vendor/sam_runtime/venv/bin/python land_cover_classification/pytorch_deps_check.py --json
land_cover_classification/vendor/sam_runtime/venv/bin/python land_cover_classification/sam_deps_check.py --backend sam2
```

SAM2 默认权重目录为：

`land_cover_classification/models/sam2/`

默认权重文件为：

- `sam2.1_hiera_base_plus.pt`

## 本地 QGIS MCP 调试约定

- 本机 QGIS MCP 插件默认监听 `127.0.0.1:9876`。该端口不是 HTTP 服务，不要用普通 `curl http://127.0.0.1:9876` 期待 JSON 响应；根路径通常会返回空响应。
- QGIS MCP 使用 TCP socket + 4 字节大端长度前缀 + UTF-8 JSON payload。请求体格式为 `{"type": "<command>", "params": {...}}`，响应同样是长度前缀 JSON。
- 常用命令包括 `ping`、`get_layers`、`get_project_info`、`get_layer_features`、`execute_code`。其中 `execute_code` 会在当前 QGIS 进程内执行 PyQGIS 代码，适合读取真实图层状态、复现 writer/processing 错误和验证导出结果。
- 现场排查 QGIS 插件行为时，优先通过 MCP 读取当前 QGIS 会话，而不是猜测本地状态。DXF、Shapefile、GeoTIFF 等导出问题应尽量在 QGIS 进程内读取 `QgsVectorFileWriter` 或 GDAL/OGR 返回的真实错误信息。
- Windows PowerShell 发送 MCP 请求时，要手动写入 4 字节大端长度头，再发送 JSON；读取响应时也要先读 4 字节长度，再按长度读取完整 JSON。若普通沙箱命令因本地 socket 或 Windows sandbox SID 问题失败，可按权限规则请求提升后重试。
- `land_cover_classification/qgis-mcp-main/src/qgis_mcp/client.py` 是本仓库内可参考的 Python 客户端实现，协议常量在 `helpers.py` 中。

## 本地 CodeGraph 索引

- `.codegraph/` 是每台机器的本地索引目录，除其 `.gitignore` 外的数据库、WAL、PID 和日志均不得提交或手工修改。
- CodeGraph watcher 会在文件变更后自动同步；完成一批源码或文档修改后，仍应在仓库根目录执行 `codegraph.cmd sync .` 和 `codegraph.cmd status .` 确认索引已更新。Windows PowerShell 若禁止执行 `codegraph.ps1`，请使用 `codegraph.cmd`。
- 若存在 `.serena/memories/`，行为约定变化时同步相关本地记忆；不要手工改符号缓存或把本机状态写成通用实现前提。开发入口、安装调用关系与拉取后同步步骤见 `docs/development.md`。

## 编码与文档注意事项

- 仓库里部分中文内容在某些 Windows / PowerShell 读取方式下可能显示乱码，修改时要特别注意文件实际编码。
- `metadata.txt`、部分 Python 文件和说明文档都可能受编码影响，除非必要，不要整文件重写。
- 若新增说明文档，优先放在根目录 `docs/` 或仓库根目录，而不是随意混入插件内部。
- 新增代码注释和文档字符串必须使用中文；专有名词、API 名称、变量名和命令行输出可以保留英文。

## 验证建议

- 只改文档：检查链接、路径和目录描述是否与当前仓库一致，并确保 `AGENTS.md` 与 `CLAUDE.md` 同步。
- 只改纯 Python 逻辑：优先运行对应单元测试，或至少验证模块可编译。
- 改第三方库兼容层或默认 bundle `arch.py` 调用约定时，除真实 runtime smoke test 外，还要用 mock / fake 对象覆盖新旧 API 约定，验证方案是通用兼容而不是只适配当前机器安装的版本。
- 改 PyTorch 主推理：运行 `python -m py_compile` 覆盖 `pytorch_deps_check.py`、`pytorch_inference_core.py`、`pytorch_inference_runner.py`、`model_scan.py` 和 `land_cover_classification_dialog.py`。
- 改超大影像或流式推理：以合成小栅格、伪超大元数据和 mock 验证窗口读取、halo 裁剪、跨块后处理、BigTIFF profile 与窗口写出；不要把真实超大影像完整推理作为前置验证。
- 改画布范围局部推理：覆盖跨 CRS 画布范围转换、与影像范围求交、向外取整和边界裁剪、无交集失败、ROI 内 core / ROI 外 halo、ROI 外 NoData / 无效掩膜，以及仅替换 ROI 内 inference 的磁盘合成。
- 改 PyTorch runner 诊断：覆盖 stdout 被拆分到多个 `readyReadStandardOutput` 回调、进程结束前残留半行、异常链 `details` 与 QGIS 日志输出。
- 改 DEM 后处理规则：运行 `land_cover_classification/test/test_rules_synthetic.py`，覆盖 v3 契约校验、`rule_order`、禁用规则结构校验、keep/drop 决策，并检查 `<output>.postprocess.json` 的保留 / 丢弃决策和规则审计字段。
- 改 DEM 形态学后处理：至少覆盖闭运算连接邻近斑块、填孔、边界平滑、开运算去噪、最小面积过滤和审计 JSON 计数变化。
- 改模型扫描或 bundle 约定：准备一个真实 PyTorch bundle，确认模型下拉框只显示有效的 `manifest.json` bundle，并跳过缺少或无效 manifest 的目录；同时确认 `FACTOR_NAMES`、`dem_channels`、`active_dem_channels` 与模型期望 DEM 输入通道一致。
- 改 UI 或 QGIS 集成：在 QGIS 中手动验证插件入口、模型根目录、可选 DEM 文件（含无 DEM、部分覆盖和无覆盖）、`草稿编辑` / `模型推理` / `导出` 三个页签、进度条、全图与当前画布范围推理、按钮、输出目录和草稿流程。
- 改端到端推理：使用一张带地理参考的输入影像和一张范围相交的 DEM，验证输出类别 GeoTIFF、后处理 JSON、草稿矢量和日志。
- 改导出流程：分别验证 `Raster (GeoTIFF)`、`Vector (Shapefile)` 和 `Vector (DXF / CAD)` 三种导出格式。
- 改 DXF 导出视图范围：验证大文件按组码流式重写、临时文件替换成功，以及 `$EXTMIN`、`$EXTMAX`、`$LIMMIN`、`$LIMMAX` 和活动 `VPORT` 值正确。
- 改 AI 编辑流程：验证推理完成后自动进入 `草稿编辑` 页签，启动 AI 编辑，左键 / 右键 / 撤销 / 清空 / 停止恢复 map tool，“回到 AI 点选模式”可继续工作，且预览不覆盖负点；并验证未发生后续编辑时可撤销最近一次模型融合。
- 改统一 runtime 或环境检查：验证打开插件面板仅检查解释器存在性，缺少时引导安装但不自动下载；已有环境不执行启动时重依赖导入。覆盖固定资产校验、安全解压、并发锁、已有异常环境保留、取消、UTF-8 拆分输出和日志脱敏；在 QGIS 验证 QProcess 实时输出、取消、重复打开和成功后不自动推理。分别验证 `runtime_setup.py --functional-check`、`pytorch_deps_check.py --json`、CUDA 不可用时 CPU 回退提示、`sam_deps_check.ensure_ready()`、runner 与 `sam_worker.py init/set_image/predict/clear_context/quit`。模拟 CUDA/平台测试不能替代目标系统实测；记录未验证平台和离线验收边界。

## 会话草稿与融合约定

当前草稿工作流以 `DraftSession` 的临时 GeoPackage generation 为唯一真相。草稿字段固定为 `class_id`、`class_name`、`source_id`、`feature_uuid`、`origin`、`run_id`，其中每个 `feature_uuid` 必须唯一；AI 和已手工修改的要素必须为 `origin=user`，每次全图推理只能替换全部 `origin=inference`，局部推理只能替换 ROI 内的 `origin=inference`，均按“用户区域 ∪ (最新推理区域 - 用户区域)”执行磁盘型 Processing 合成。每次 AI 追加、原生编辑提交或推理融合都必须先生成、规范化并校验下一代 GeoPackage，再复用单一可见草稿层通过 `setDataSource()` 原子切换；失败或取消必须保留旧 generation。只有在未发生后续编辑前，才可撤销最近一次模型融合。Raster、Shapefile 和 DXF 导出不能泄露 `feature_uuid`、`origin`、`run_id`；Shapefile 导出公开分类及标准成果字段，DXF 只导出闭合外边界并以流式方式修正初始视图范围。


## 内部项目与对外成果服务

- 首期后端位于 `backend/`，使用独立 Python 3.12 环境、FastAPI、PostgreSQL/PostGIS、Fiona、Shapely 与 pyproj；不得把服务依赖安装到 QGIS Python 或推理/SAM runtime。
- `project_panel.py` / `project_client.py` 提供首个项目页、自动连接/输入登记、切换前保存和草稿恢复。用户先创建或选择项目；界面不得包含服务地址、API Key、连接/刷新、手动关联或本地模式按钮。地址由 `project_service.ini` 配置并随插件打包。
- 输入影像与 DEM 直接采用 QGIS 选择的路径，不设置输入目录白名单；服务仅检查普通文件存在且可读取。保留 `LCC_STORAGE_ROOT` 管理草稿、版本和审计，数据库关联及成果下载目录边界不变。
- 临时 generation 仍是编辑态；通过内部服务保存的不可变草稿快照可在重开后恢复。保存使用 SQLite backup 合并 WAL 并显式关闭连接。草稿并发保存使用 base_draft_id；手动和切换失败保留会话，退出失败放行并保留磁盘目录。
- 文件导出与版本发布独立。“发布项目版本”先保存当前编辑，一次收集项目全部已保存草稿（含尚未发布的影像）并人工确认；无草稿的影像明确提示后不纳入。服务核对完整快照列表、base_version 和幂等请求，登记/保存/发布共用项目锁；失败不改变旧版本。
- 标准项目成果是按影像分层的 GeoPackage；权威面积、包含孔洞的周长由服务端按 WGS84 椭球重算，中心点为源 CRS 质心转 EPSG:4326。外部属性不含草稿 origin 或内部文件路径。
- 内部和公共 ASGI 应用分别部署；内部应用不使用 API Key，只允许本机/受控内网访问，公共应用通过 API Key 鉴权。后端从仓库根 `.env`（或 `LCC_ENV_FILE`）用 python-dotenv 加载配置，已有环境变量优先；真实配置不得提交。公共应用只提供五个 GET 接口；指定非法或跨项目版本不得回退当前版本，后续分页必须固定 version_id。
- 文件必须准备完成后才能提交数据库当前版本指针；失败或结果不确定的目录保留供管理员核对，不得在提交异常后擅自删除。发布文件不得通过静态目录对外暴露。
- 修改项目管理时同时检查主插件停靠窗口/退出保存、对话框导出、客户端/项目页、`workflow_ui.py`、`attribute_browser.py`、`history_dialogs.py`、`recovery.py`、后端事务与路由、打包清单和 `test_project_workflow.py`、`test_workflow_ui.py`、`test_independent_workspaces.py`、`backend/tests/test_history.py` 及相关后端回归。部署和验证边界见 `docs/project_service.md` 与 `docs/project_service_validation.md`；未配置数据库时集成测试 skip 不代表已验收。


- 成果共享契约位于仅依赖标准库的 result_attributes.py：17 个标准字段，正确拼写 ygtsm，两个 tid/bz 分别合并；tid 为影像 UUID.hex，项目创建选填 ztz（规范化后最多 32 字符），mj 为最终几何 WGS84 椭球平方米面积，其他标准字段为空。公开 area_m2 归并为 mj；class_name 保留、lx 为空，不把周长或版本时间映射到 cd/rq。Shapefile 保留三公开字段后共 20 字段，mj 使用 Double(24,3)；GeoPackage/API 共 29 项、不舍入，空值完整输出。覆盖文件前校验影像/项目/草稿关联；不扩展草稿字段或 GeoTIFF/DXF。
- 退出保存为尽力保存：QtNetwork + QFile 流式上传，单个 QTimer 总期限 5 秒、超时 abort、不重试；失败/离线/忙碌不拦截 Close、Escape、dock 或 unload。QGIS 自身退出确认不受干预；同轮只尝试一次，取消退出/重新打开可重试。普通保存/切换/发布前失败保护保留。后台普通线程和快照独立持有至操作结束，不无限 wait/terminate；失败或跳过解除图层引用但保留 generation 路径，日志明确未确认成功及编辑缓冲风险，不弹阻塞确认。
- 未交付旧项目仅允许显式一次性重置，核实六表与文件路径、停写后事务清空、不用 CASCADE；只删除已登记关联文件，保留原始资产及未知文件。不得把清理或旧属性兼容放入启动和日常业务。

- 项目页成果属性预览须复用 `result_attributes.RESULT_FIELDS` 与 `build_result_attributes` 显示全部 17 个标准字段，空值在界面显示为空白，实际数据及后端记录保持 NULL，保留分类与几何信息；校验当前项目、影像和草稿一致。预览只读，不扩展草稿 schema，不提前生成发布版本元数据。

- 后端日常统一启动：Windows 根目录 `start_backend.bat`；Linux/macOS `bash start_backend.sh`。复用独立 `.venv-backend` 和 `.env`，同一终端管理两个独立服务，Ctrl+C 联动退出；不安装依赖或初始化数据库。
- 统一后端启动器默认监听 `0.0.0.0:8091` / `0.0.0.0:8092`，客户端使用服务器实际 IP；内部端口由防火墙限制在可信内网。

## 工作区 UI 与状态约定

- `workflow_ui.py` 负责工作摘要、可用条件、显示控制与运行时布局；业务状态变化通过合并刷新更新界面，不在刷新中发起网络请求或重依赖检查。
- 项目、模型扫描、推理、AI 和导出反馈分开；项目错误仅在项目页提供原操作重试，发布保持幂等请求。不得恢复跨页镜像状态或通用连接/刷新按钮。
- 一个“开始推理”入口选择全图、当前画布或绘制范围；当前画布在点击时取交集，绘制范围绑定输入影像。复用既有校验、halo、融合和取消语义，不得无交集回退全图。仅成功融合后提供撤销；融合中显示不定进度，完成发布后才置 100%。
- 零要素但有效且关联正确的草稿可保存、预览和导出。发布按项目已有草稿及待确认幂等请求判断，不要求当前影像已打开。
- 摘要区分编辑缓冲未提交、本地 generation 待同步与当前 generation 已同步服务端；后续编辑使已同步标记失效。导出与服务端同步仍独立。
- 属性预览默认显示常用字段，全部字段视图保留共享 17 个标准字段与分类、几何信息；仅调整可见顺序、列宽与显示精度，不改变 schema、NULL 或底层精度。
- 侧栏支持调整宽度并记住逻辑像素宽度，各工作区可滚动。草稿显示、轮廓和不透明度只影响画布；AI 预览使用青色半透明填充和虚线，与草稿区分。
- 回归入口为 `test/test_workflow_ui.py`；实现边界与验收清单见 `docs/ui_workspaces.md`。地图属性双向联动、版本对比和恢复管理由后续独立模块实现，见文末约定。

- UI Qt 信号生命周期：`WorkflowUi` 必须为以对话框为父对象的 QObject，外部画布信号使用 Qt 槽管理接收者寿命；刷新装饰器保留无参槽签名，避免 clicked(bool) 误传。网络请求线程在调用栈读取结果前不得 deleteLater，退出后的后台释放保持有效。AI 同影像/DEM 无操作绑定不应被自身追加忙碌状态阻止，真实切换仍保留保护。回归必须包含真实按钮点击、面板销毁后画布事件和请求返回前 DeferredDelete。


## 独立属性、版本与恢复工作区

- `attribute_browser.py` 为非模态只读成果浏览器，地图点选与草稿选择双向同步；每页至多 200 个，复用共享成果字段和椭球计算。窗口固定项目/影像身份，图层销毁或切换必须失效，generation 换代刷新，关闭恢复原地图工具；外部信号使用 QObject 槽管理寿命。
- `history_dialogs.py` 提供内部版本对比和恢复管理。版本以 `(image_id, feature_id)` 匹配，几何按 ST_Equals、属性忽略 version_id/created_at；分页固定两个版本，不做空间猜测身份，不修改发布指针。临时 QgsRubberBand 为图形项，移除场景后释放，不可调用 QObject.deleteLater。
- `recovery.py` 只登记成功激活的 generation 到 recovery.json，关联服务、项目、影像和输入路径；不得扫描任意候选作为已成功草稿。历史下载校验 ID/SHA，恢复校验 CRS、边界、UUID、分类、来源与几何，备份当前草稿后使用已有原子 generation 发布，失败保留原草稿。恢复结果只在本地待同步，保存沿用并发保护，发布仍需独立确认。不自动删除恢复文件，不承诺未提交编辑缓冲的崩溃恢复。
- 历史快照和对比仅增加内部 GET 接口，不改变对外五接口、鉴权、数据库 schema、草稿字段或成果字段。回归入口为 test/test_independent_workspaces.py 与 backend/tests/test_history.py；操作说明见 docs/ui_workspaces.md。

- 属性/历史/恢复修改的验证至少覆盖真实画布鼠标事件、窗口关闭及对象销毁、generation 换代、跨项目/影像拒绝、分页、恢复取消与失败保留、历史文件 ID/SHA-256 校验。Qt/QGIS 回归使用 QGIS Python；PostGIS 回归仅使用显式独立测试数据库，不能拿生产数据库代替测试配置，skip 不代表通过。
- 内部版本列表允许未发布项目返回空列表，公共版本查询保持既有未发布/非法版本失败语义；内部对比及快照分页上限 200。新增内部路由需重启后端，插件模块需重新加载后生效；不得为验证而未经核实重载用户正在编辑的会话。
