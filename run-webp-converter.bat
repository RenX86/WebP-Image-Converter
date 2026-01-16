@echo off
setlocal enabledelayedexpansion

:: Set the path to your Python script
set "SCRIPT_PATH=%~dp0WebP-Image-Converter.py"

:: Check if Python is installed and in PATH
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python is not installed or not in your PATH.
    echo Please install Python and add it to your PATH.
    pause
    exit /b 1
)

:: Run the Python script in a loop
:loop
python "%SCRIPT_PATH%"
echo.
echo Script finished. Restarting...
echo Press Ctrl+C to exit.
echo.
goto loop