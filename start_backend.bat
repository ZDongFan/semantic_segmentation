@echo off
setlocal
set "BACKEND_PYTHON=%LCC_BACKEND_PYTHON%"
if not defined BACKEND_PYTHON set "BACKEND_PYTHON=%~dp0.venv-backend\Scripts\python.exe"
if not exist "%BACKEND_PYTHON%" (
    echo Backend Python not found: "%BACKEND_PYTHON%"
    echo Prepare the backend environment with:
    echo   py -3.12 -m venv "%~dp0.venv-backend"
    echo   "%~dp0.venv-backend\Scripts\python.exe" -m pip install -r "%~dp0backend\requirements.txt"
    echo Configure "%~dp0.env" using "%~dp0.env.example" as a template.
    echo Or set LCC_BACKEND_PYTHON to an existing backend Python executable.
    pause
    exit /b 1
)
"%BACKEND_PYTHON%" -X utf8 "%~dp0backend\start.py" %*
set "BACKEND_EXIT=%ERRORLEVEL%"
if not "%BACKEND_EXIT%"=="0" pause
exit /b %BACKEND_EXIT%
