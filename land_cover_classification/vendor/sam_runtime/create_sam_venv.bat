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
        echo Recreating existing unified plugin venv: %VENV_DIR%
        rmdir /s /q "%VENV_DIR%"
    ) else (
        echo Existing unified plugin venv found: %VENV_DIR%
        echo Delete it manually, or set SAM_RECREATE=1 and run again.
        exit /b 1
    )
)

echo Creating unified plugin venv: %VENV_DIR%
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

if "%SAM_TORCH_CPU_INDEX%"=="" set "SAM_TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu"
if "%SAM_TORCH_PACKAGES%"=="" set "SAM_TORCH_PACKAGES=torch torchvision"

set "USE_CUDA_TORCH=0"
set "TORCH_INSTALL_MODE=cpu"
where nvidia-smi >nul 2>nul
if not errorlevel 1 (
    set "USE_CUDA_TORCH=1"
    if "%SAM_TORCH_CUDA_INDEX%"=="" (
        if "%SAM_TORCH_CUDA_INDEXES%"=="" (
            for /f "usebackq delims=" %%I in (`"%VENV_PY%" -c "import re, subprocess; candidates=[((12,8),'cu128'),((12,6),'cu126'),((12,4),'cu124'),((12,1),'cu121'),((11,8),'cu118')]; p=subprocess.run(['nvidia-smi'],capture_output=True,text=True,errors='ignore'); m=re.search(r'CUDA Version:\s*(\d+)\.(\d+)', p.stdout); v=tuple(map(int,m.groups())) if m else (99,99); print(' '.join('https://download.pytorch.org/whl/'+name for req,name in candidates if v >= req))"`) do (
                set "SAM_TORCH_CUDA_INDEXES=%%I"
            )
        )
    ) else (
        set "SAM_TORCH_CUDA_INDEXES=%SAM_TORCH_CUDA_INDEX%"
    )
)

if "%USE_CUDA_TORCH%"=="1" (
    echo NVIDIA environment detected. Trying CUDA PyTorch wheels first...
    set "TORCH_INSTALLED=0"
    for %%I in (!SAM_TORCH_CUDA_INDEXES!) do (
        if "!TORCH_INSTALLED!"=="0" (
            echo Trying PyTorch wheel index: %%I
            "%VENV_PY%" -m pip install --force-reinstall %SAM_TORCH_PACKAGES% --index-url "%%I"
            if not errorlevel 1 (
                "%VENV_PY%" -c "import sys, torch; print('torch', torch.__version__, 'cuda_runtime', torch.version.cuda, 'cuda_available', torch.cuda.is_available()); sys.exit(0 if torch.version.cuda and torch.cuda.is_available() else 1)"
                if not errorlevel 1 (
                    set "TORCH_INSTALLED=1"
                    set "TORCH_INSTALL_MODE=cuda"
                )
            )
        )
    )
    if not "!TORCH_INSTALLED!"=="1" (
        echo CUDA PyTorch installation failed or CUDA is unavailable at runtime. Falling back to CPU PyTorch wheels...
        "%VENV_PY%" -m pip install --force-reinstall %SAM_TORCH_PACKAGES% --index-url "%SAM_TORCH_CPU_INDEX%"
        if errorlevel 1 (
            echo Failed to install PyTorch.
            exit /b 1
        )
    )
) else (
    echo NVIDIA environment not detected. Installing CPU PyTorch wheels...
    "%VENV_PY%" -m pip install --force-reinstall %SAM_TORCH_PACKAGES% --index-url "%SAM_TORCH_CPU_INDEX%"
    if errorlevel 1 (
        echo Failed to install PyTorch.
        exit /b 1
    )
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

echo Installing unified plugin runtime dependencies...
"%VENV_PY%" -m pip install sam2 opencv-contrib-python numpy Pillow segmentation-models-pytorch==0.4.* timm rasterio scipy PyYAML
if errorlevel 1 (
    echo Failed to install plugin runtime dependencies.
    exit /b 1
)

echo Verifying the unified plugin runtime...
"%VENV_PY%" -c "import torch, torchvision, sam2, cv2, numpy, rasterio, scipy, yaml, timm, segmentation_models_pytorch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available()); print('plugin runtime ok')"
if errorlevel 1 (
    echo Unified plugin runtime verification failed.
    exit /b 1
)
if "!TORCH_INSTALL_MODE!"=="cuda" (
    "%VENV_PY%" -c "import sys, torch; sys.exit(0 if torch.version.cuda and torch.cuda.is_available() else 1)"
    if errorlevel 1 (
        echo Unified plugin runtime verification failed: PyTorch was expected to use CUDA, but it is running as CPU-only.
        exit /b 1
    )
)

echo Unified plugin venv created successfully: %VENV_DIR%
endlocal
