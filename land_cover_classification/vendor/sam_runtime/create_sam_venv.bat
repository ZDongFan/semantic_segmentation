@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%venv"

if "%SAM_PYTHON%"=="" (
    if exist "C:\Python312\python.exe" (
        set "SAM_PYTHON=C:\Python312\python.exe"
    ) else (
        for /f "usebackq delims=" %%P in (`py -3.12 -c "import sys; print(sys.executable)" 2^>nul`) do (
            set "SAM_PYTHON=%%P"
        )
        if "!SAM_PYTHON!"=="" set "SAM_PYTHON=python"
    )
)

echo Using Python: %SAM_PYTHON%
"%SAM_PYTHON%" --version
if errorlevel 1 (
    echo Failed to run the selected Python interpreter.
    echo Install Python 3.12 first, or set SAM_PYTHON to a valid Python 3.12 path.
    exit /b 1
)

if exist "%VENV_DIR%" (
    if "%SAM_RECREATE%"=="1" (
        echo Recreating existing venv: %VENV_DIR%
        rmdir /s /q "%VENV_DIR%"
    ) else (
        echo Existing venv found: %VENV_DIR%
        echo Delete it manually, or set SAM_RECREATE=1 and run again.
        exit /b 1
    )
)

echo Creating venv: %VENV_DIR%
"%SAM_PYTHON%" -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo Failed to create the venv.
    exit /b 1
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo python.exe was not created inside the venv: %VENV_PY%
    exit /b 1
)

echo Upgrading pip, setuptools, and wheel...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1

set "USE_CUDA_TORCH=0"
where nvidia-smi >nul 2>nul
if not errorlevel 1 set "USE_CUDA_TORCH=1"

if "%USE_CUDA_TORCH%"=="1" (
    echo NVIDIA environment detected. Installing CUDA PyTorch wheels...
    "%VENV_PY%" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124 --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple
) else (
    echo NVIDIA environment not detected. Installing CPU PyTorch wheels...
    "%VENV_PY%" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple
)
if errorlevel 1 (
    echo Failed to install PyTorch.
    exit /b 1
)

set "SAM2_BUILD_CUDA=0"
where nvcc >nul 2>nul
if not errorlevel 1 (
    where cl >nul 2>nul
    if not errorlevel 1 set "SAM2_BUILD_CUDA=1"
)

if "%SAM2_BUILD_CUDA%"=="1" (
    echo nvcc and cl detected. SAM2 may build CUDA extensions when needed.
) else (
    echo nvcc or cl not detected. Skipping SAM2 CUDA extension build.
)

echo Installing SAM2 and image dependencies...
"%VENV_PY%" -m pip install sam2 opencv-contrib-python numpy Pillow
if errorlevel 1 (
    echo Failed to install SAM2 dependencies.
    exit /b 1
)

echo Verifying the SAM2 runtime...
"%VENV_PY%" -c "import torch, torchvision, sam2, cv2, numpy; print('torch', torch.__version__, 'cuda', torch.cuda.is_available()); print('sam2 ok')"
if errorlevel 1 (
    echo SAM2 runtime verification failed.
    exit /b 1
)

echo SAM venv created successfully: %VENV_DIR%
endlocal
