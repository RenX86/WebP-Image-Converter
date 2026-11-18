@echo off
setlocal

set "SCRIPT_PATH=%~dp0WebP-Image-Converter.py"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python is not installed or not in your PATH.
    echo Please install Python and add it to your PATH.
    pause
    exit /b 1
)

python "%SCRIPT_PATH%" %*
if %errorlevel% neq 0 (
    echo.
    echo Exited with code %errorlevel%.
    pause
)
