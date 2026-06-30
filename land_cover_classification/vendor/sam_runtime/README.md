# 插件统一运行环境

本目录用于创建插件唯一的独立 Python 虚拟环境。虚拟环境固定放在：

```text
land_cover_classification/vendor/sam_runtime/venv
```

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

脚本默认在线安装依赖。Windows 下会优先使用 `C:\Python312\python.exe`，然后尝试 `py -3.12`，最后才使用 `python`。如需指定解释器，可设置 `SAM_PYTHON`:

```bat
set SAM_PYTHON=C:\Python312\python.exe
create_sam_venv.bat
```

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

- 检测到 NVIDIA 环境时，优先安装 CUDA 版 PyTorch。
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
