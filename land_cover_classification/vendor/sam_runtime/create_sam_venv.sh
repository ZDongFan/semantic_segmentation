#!/usr/bin/env bash
# 离线创建 SAM AI 编辑功能所需的 Python 虚拟环境。
# 默认在 vendor/sam_runtime/venv 下创建,wheels 来自 wheels/ 目录。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
WHEEL_DIR="${SCRIPT_DIR}/wheels"
REQUIREMENTS="${SCRIPT_DIR}/requirements-sam.txt"

SAM_PYTHON="${SAM_PYTHON:-python3}"
echo "使用解释器: ${SAM_PYTHON}"
"${SAM_PYTHON}" --version

if [ -d "${VENV_DIR}" ]; then
    echo "发现已有虚拟环境: ${VENV_DIR}"
    echo "如需重新创建,请先手动删除该目录。"
else
    echo "创建虚拟环境: ${VENV_DIR}"
    "${SAM_PYTHON}" -m venv "${VENV_DIR}"
fi

VENV_PY="${VENV_DIR}/bin/python"
if [ ! -x "${VENV_PY}" ]; then
    echo "未找到 venv 中的 python: ${VENV_PY}"
    exit 1
fi

"${VENV_PY}" -m pip install --upgrade pip --no-index --find-links "${WHEEL_DIR}"
"${VENV_PY}" -m pip install --no-index --find-links "${WHEEL_DIR}" -r "${REQUIREMENTS}"

echo "SAM 虚拟环境创建完成: ${VENV_DIR}"
