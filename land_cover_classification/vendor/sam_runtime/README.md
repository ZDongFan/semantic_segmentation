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

脚本默认在线安装依赖。Windows 下会依次尝试当前用户默认安装位置 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`、`C:\Python312\python.exe`、`py -3.12`、PATH 中的 `python3.12`，最后才使用 `python`。最后的回退解释器不是 Python 3.12 时，脚本会提示警告但仍继续执行。需要明确指定解释器时可设置 `SAM_PYTHON`:

```bat
set "SAM_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
create_sam_venv.bat
```

如需把 venv 实体放到其他短路径，可设置 `SAM_VENV_DIR`；脚本仍会自动创建插件目录联接：

```bat
set "SAM_VENV_DIR=D:\qgis_runtime\lcc_venv"
create_sam_venv.bat
```

对应的通用联接命令为：

```bat
mklink /J "<插件目录>\vendor\sam_runtime\venv" "<venv 实体目录>"
```

正常情况下无需手工执行该命令。

已有 `venv/` 时脚本会停止，避免覆盖本机环境。如需重建，可先手动删除 `venv/`，或设置:

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
- 可通过 `SAM_TORCH_CUDA_INDEX` 指定单个 CUDA wheel 源，或通过 `SAM_TORCH_CUDA_INDEXES` 指定多个候选源；可通过 `SAM_TORCH_PACKAGES` 指定 `torch` / `torchvision` 的版本范围。
- 只有同时检测到 `nvcc` 和 C/C++ 编译工具链时，才设置 `SAM2_BUILD_CUDA=1` 构建 SAM2 CUDA 扩展。
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
