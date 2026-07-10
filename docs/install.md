# 运行时依赖安装

当前插件只维护一套独立子进程运行环境：

```text
land_cover_classification/vendor/sam_runtime/venv/
```

这套环境同时服务两个功能：

- PyTorch bundle 主推理
- SAM2 AI 辅助编辑

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

Windows：

```bat
land_cover_classification\vendor\sam_runtime\create_sam_venv.bat
```

Linux / macOS：

```bash
land_cover_classification/vendor/sam_runtime/create_sam_venv.sh
```

脚本会检测 `nvidia-smi`。检测到 NVIDIA GPU 时会根据驱动报告的 CUDA 能力，从 PyTorch 官方 CUDA wheel 源中按兼容顺序尝试安装 `torch` / `torchvision`；如果 CUDA wheel 安装或运行时校验失败，会回退到 CPU 版，保证插件仍可运行。未检测到 NVIDIA 环境时直接安装 CPU 版。随后会安装 SAM2、OpenCV、rasterio、segmentation-models-pytorch、timm、scipy、PyYAML 等主推理和 AI 编辑共用依赖。

如需在特殊环境中手动指定 PyTorch wheel 源，可设置 `SAM_TORCH_CUDA_INDEX` 或 `SAM_TORCH_CUDA_INDEXES`；如需指定包版本范围，可设置 `SAM_TORCH_PACKAGES`。

已有 `venv/` 时脚本会停止，避免覆盖本机环境。如需重建，可先手动删除 `venv/`，或设置：

```bat
set SAM_RECREATE=1
land_cover_classification\vendor\sam_runtime\create_sam_venv.bat
```

验证 PyTorch 主推理依赖：

Windows：

```bat
.\land_cover_classification\vendor\sam_runtime\venv\Scripts\python.exe land_cover_classification\pytorch_deps_check.py --json
```

Linux / macOS：

```bash
land_cover_classification/vendor/sam_runtime/venv/bin/python land_cover_classification/pytorch_deps_check.py --json
```

验证 SAM2 AI 编辑依赖：

Windows：

```bat
.\land_cover_classification\vendor\sam_runtime\venv\Scripts\python.exe land_cover_classification\sam_deps_check.py --backend sam2
```

Linux / macOS：

```bash
land_cover_classification/vendor/sam_runtime/venv/bin/python land_cover_classification/sam_deps_check.py --backend sam2
```

## 三、准备 PyTorch Bundle

将训练仓导出的 bundle 子目录放入：

```text
land_cover_classification/models/semantic_segmentation/
```

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
3. 运行推理时必须选择输入影像和对应 DEM 文件。
