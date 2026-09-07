@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem 在独立 PowerShell 进程中发现 QGIS Python，环境清理不会影响调用方。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_sam_venv.ps1"
exit /b %errorlevel%
