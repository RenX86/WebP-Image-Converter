@echo off
setlocal

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "SCRIPT_PATH=%~dp0WebP-Image-Converter.py"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%SCRIPT_PATH%" %*
    goto check_exit
)

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python is not installed or not in your PATH.
    echo Please install Python and add it to your PATH.
    pause
    exit /b 1
)

python "%SCRIPT_PATH%" %*

:check_exit
if %errorlevel% neq 0 (
    echo.
    echo Exited with code %errorlevel%.
    pause
)
