# 插件统一运行环境

本目录用于创建插件唯一的独立 Python 虚拟环境。插件固定通过以下路径访问环境：

```text
land_cover_classification/vendor/sam_runtime/venv
```

Windows 下为规避 QGIS 插件深层目录触发的传统 `MAX_PATH` 限制，脚本默认将 venv 实体创建在 `%LOCALAPPDATA%\LCCRuntime\venv`，再自动用目录联接接入上述插件路径。无需管理员权限，也不需要手工移动整个插件。

这套环境同时服务：

- `pytorch_inference_runner.py`：PyTorch bundle 主推理
- `sam_worker.py`：SAM2 AI 辅助编辑

插件主进程不使用 QGIS 自带 Python 加载 `torch`、`sam2`、`rasterio` 或 `segmentation_models_pytorch`，只通过子进程调用这些能力。这样可以避免 QGIS / OSGeo4W 的 Python 环境变量污染运行时。

## 默认 SAM 后端

- 默认后端: `sam2`
- 默认模型: SAM2.1 Base+
- 默认权重: `land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt`
- 默认配置: `configs/sam2.1/sam2.1_hiera_b+.yaml`

## 创建环境

Windows:

```bat
create_sam_venv.bat
```

Linux/macOS:

```bash
./create_sam_venv.sh
```

插件要求 QGIS 3.44+。脚本先检查显式 `SAM_PYTHON`，然后按当前 QGIS 环境、其他 QGIS 安装、独立 Python 的顺序发现解释器。通常无需额外安装独立 Python，不固定 Python 版本；依赖安装和最终导入验证决定兼容性。显式解释器无效时直接失败，自动发现的候选失败时继续回退。

Windows 入口调用同目录的 `create_sam_venv.ps1`，检查 `apps/Python3*/python.exe` 并按数字版本排序，禁止自动选择 QGIS `bin/python.exe` 包装器。Linux 检查 QGIS 前缀、QGIS 所在发行版的系统 Python 和独立 Python；缺少 venv/ensurepip 时可能需要安装 `python3-venv`。macOS 检查系统和用户 Applications 下的 QGIS 应用包。隔离打包内部 Python 不可用时，可通过 `SAM_PYTHON` 指定外部 Python：

```bat
set "SAM_PYTHON=<可用 Python 的完整路径>"
create_sam_venv.bat
```

两个入口复用标准库辅助脚本 `runtime_setup.py`，只向独立 venv 安装依赖，验证 `include-system-site-packages=false`。安装开始后不会切换基础解释器。

通用包默认从清华 PyPI 镜像 `https://pypi.tuna.tsinghua.edu.cn/simple` 安装；用户已有的 `PIP_INDEX_URL` 优先。失败时不自动换源，可显式设置 `PIP_INDEX_URL=https://pypi.org/simple` 或其他可信镜像后重试。日志只显示默认/用户索引类型，避免输出 URL 凭据。PyTorch 通过清除 pip 环境变量、禁用配置文件和 `--isolated --index-url` 使用独立 wheel 源；通用镜像及额外索引均不影响 PyTorch。后续通用依赖使用精确版本约束及已有构建环境，版本冲突直接失败，并验证 torch/torchvision 未被替换。

如需把 venv 实体放到其他短路径，可设置 `SAM_VENV_DIR`；脚本仍会自动创建插件目录联接：

```bat
set "SAM_VENV_DIR=D:\qgis_runtime\lcc_venv"
create_sam_venv.bat
```

Linux/macOS 也支持 `SAM_VENV_DIR`，自定义实体目录通过符号链接接入固定入口。

对应的通用联接命令为：

```bat
mklink /J "<插件目录>\vendor\sam_runtime\venv" "<venv 实体目录>"
```

正常情况下无需手工执行该命令。

已有 runtime 时脚本会停止。QGIS 移动、卸载或升级使原环境失效时，请显式重建；删除前会验证具体 venv 路径及标记，拒绝不明目录。失败时保留实体环境和诊断。设置:

```bat
set SAM_RECREATE=1
create_sam_venv.bat
```

## 依赖范围

脚本会安装主推理和 AI 编辑共用依赖，包括：

- `torch`
- `torchvision`
- `sam2`
- `opencv-contrib-python`
- `numpy`
- `Pillow`
- `segmentation-models-pytorch`
- `timm`
- `rasterio`
- `scipy`
- `PyYAML`

## CUDA 策略

脚本分两层判断 CUDA:

- 检测到 NVIDIA 环境时，会根据驱动报告的 CUDA 能力按兼容顺序尝试 PyTorch 官方 CUDA wheel 源；安装失败或运行时不可用时回退到 CPU 版 PyTorch。
- CPU wheel 可通过 `SAM_TORCH_CPU_INDEX` 指定，默认 `https://download.pytorch.org/whl/cpu`。
- 可通过 `SAM_TORCH_CUDA_INDEX` 指定单个 CUDA wheel 源，或通过 `SAM_TORCH_CUDA_INDEXES` 指定多个候选源；可通过 `SAM_TORCH_PACKAGES` 指定 `torch` / `torchvision` 的版本范围。
- 只有同时检测到 `nvcc` 和 C/C++ 编译工具链且未显式设置 `SAM2_BUILD_CUDA=0` 时，才设置 `SAM2_BUILD_CUDA=1` 构建 SAM2 CUDA 扩展。
- 缺少 CUDA 编译工具链时会设置 `SAM2_BUILD_CUDA=0`，仍允许使用 GPU PyTorch 或 CPU 推理。

## 环境检查

可在插件目录外直接运行：

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

检查逻辑会通过 `venv` 内的 Python 子进程导入依赖，不会在当前 Python 进程中导入 SAM/PyTorch。

主推理与 SAM 启动时都会用此环境中的 `rasterio` 对输入执行有限窗口探测。ENVI/HFA 源文件若不能直接读取，会由 QGIS GDAL 转换为临时 tiled BigTIFF 后再次探测；SAM 只读取当前活动 crop，不在 `set_image` 时解码整幅影像。
