#!/usr/bin/env bash
# 创建插件统一 Python 3.12 虚拟环境。
# 默认位置固定为 land_cover_classification/vendor/sam_runtime/venv，供主推理和 SAM AI 编辑共用。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"

if [[ -z "${SAM_PYTHON:-}" ]]; then
    if command -v python3.12 >/dev/null 2>&1; then
        SAM_PYTHON="python3.12"
    elif command -v python3 >/dev/null 2>&1; then
        SAM_PYTHON="python3"
    else
        SAM_PYTHON="python"
    fi
fi

echo "使用解释器: ${SAM_PYTHON}"
"${SAM_PYTHON}" --version

if [[ -d "${VENV_DIR}" ]]; then
    if [[ "${SAM_RECREATE:-0}" == "1" ]]; then
        echo "SAM_RECREATE=1，正在重建插件统一虚拟环境: ${VENV_DIR}"
        rm -rf "${VENV_DIR}"
    else
        echo "发现已有插件统一虚拟环境: ${VENV_DIR}"
        echo "如需重新创建，请先手动删除该目录，或设置 SAM_RECREATE=1 后重试。"
        exit 1
    fi
fi

echo "创建插件统一虚拟环境: ${VENV_DIR}"
"${SAM_PYTHON}" -m venv "${VENV_DIR}"

VENV_PY="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
    echo "未找到 venv 中的 python: ${VENV_PY}"
    exit 1
fi

echo "升级 pip/setuptools/wheel..."
"${VENV_PY}" -m pip install --upgrade pip setuptools wheel

USE_CUDA_TORCH=0
if command -v nvidia-smi >/dev/null 2>&1; then
    USE_CUDA_TORCH=1
elif [[ -d /proc/driver/nvidia || -d /usr/local/cuda ]]; then
    USE_CUDA_TORCH=1
fi

if [[ "${USE_CUDA_TORCH}" == "1" ]]; then
    echo "检测到 NVIDIA 环境，安装 CUDA 版 PyTorch。"
    "${VENV_PY}" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
else
    echo "未检测到 NVIDIA 环境，安装 CPU 版 PyTorch。"
    "${VENV_PY}" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
fi

SAM2_BUILD_CUDA=0
if command -v nvcc >/dev/null 2>&1; then
    if command -v gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1; then
        SAM2_BUILD_CUDA=1
    fi
fi
export SAM2_BUILD_CUDA

if [[ "${SAM2_BUILD_CUDA}" == "1" ]]; then
    echo "检测到 nvcc 和 C/C++ 编译工具链，SAM2 可在需要时构建 CUDA 扩展。"
else
    echo "未检测到完整 CUDA 编译工具链，禁用 SAM2 CUDA 扩展构建。"
fi

echo "安装插件统一运行环境依赖..."
"${VENV_PY}" -m pip install \
    sam2 \
    opencv-contrib-python \
    numpy \
    Pillow \
    segmentation-models-pytorch==0.4.* \
    timm \
    rasterio \
    scipy \
    PyYAML

echo "验证插件统一运行环境..."
"${VENV_PY}" - <<'PY'
import torch
import torchvision
import sam2
import cv2
import numpy
import rasterio
import scipy
import yaml
import timm
import segmentation_models_pytorch

print("torch", torch.__version__, "cuda", torch.cuda.is_available())
print("plugin runtime ok")
PY

echo "插件统一虚拟环境创建完成: ${VENV_DIR}"
