@echo off
rem 离线创建 SAM AI 编辑功能所需的 Python 虚拟环境。
rem 默认在 vendor/sam_runtime/venv 下创建,wheels 来自 wheels/ 目录。

setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%venv"
set "WHEEL_DIR=%SCRIPT_DIR%wheels"
set "REQUIREMENTS=%SCRIPT_DIR%requirements-sam.txt"

if "%SAM_PYTHON%"=="" (
    set "SAM_PYTHON=python"
)

echo 使用解释器: %SAM_PYTHON%
"%SAM_PYTHON%" --version
if errorlevel 1 (
    echo 无法运行指定的 Python 解释器,请设置 SAM_PYTHON 环境变量后重试。
    exit /b 1
)

if exist "%VENV_DIR%" (
    echo 发现已有虚拟环境: %VENV_DIR%
    echo 如需重新创建,请先手动删除该目录。
) else (
    echo 创建虚拟环境: %VENV_DIR%
    "%SAM_PYTHON%" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo 虚拟环境创建失败。
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo 未找到 venv 中的 python.exe: %VENV_PY%
    exit /b 1
)

"%VENV_PY%" -m pip install --upgrade pip --no-index --find-links "%WHEEL_DIR%"
"%VENV_PY%" -m pip install --no-index --find-links "%WHEEL_DIR%" -r "%REQUIREMENTS%"
if errorlevel 1 (
    echo 离线安装依赖失败,请确认 wheels 目录内是否包含所有所需 wheel。
    exit /b 1
)

echo SAM 虚拟环境创建完成: %VENV_DIR%
endlocal
