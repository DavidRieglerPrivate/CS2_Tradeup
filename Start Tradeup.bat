@echo off
title CS2 Trade-Up Helper
cd /d "%~dp0"
chcp 65001 >nul

rem A .venv copied from another computer still has python.exe but points to a Python that isn't here,
rem so check that it actually runs and rebuild it if not.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import cs2tradeup" >nul 2>nul
    if errorlevel 1 (
        echo The setup folder is broken or from another computer. Rebuilding it...
        rmdir /s /q ".venv"
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo First start: setting things up. This takes a minute and only happens once...
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Python is not installed. Please install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during installation. Then start this file again.
        pause
        exit /b 1
    )
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e .
    if errorlevel 1 (
        echo Setup failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m cs2tradeup
pause
