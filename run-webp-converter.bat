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

:: Run the Python script in a loop (extra arguments are passed through, e.g. --delete-originals)
:loop
python "%SCRIPT_PATH%" %*
echo.
if %errorlevel% equ 1 echo Finished with errors - some files failed or were skipped. Check the log above.
if %errorlevel% geq 2 echo Conversion did not run - see the error above.
echo.
set "CHOICE="
set /p "CHOICE=Press Enter to convert another folder, or type Q to quit: "
if /i "!CHOICE!"=="q" exit /b 0
echo.
goto loop
