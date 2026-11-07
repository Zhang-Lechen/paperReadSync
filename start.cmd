@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Local Python environment not found. See README.md for installation.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\start_app.py" %*
if errorlevel 1 pause
