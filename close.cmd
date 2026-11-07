@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\close_app.ps1" %*
set "CLOSE_EXIT=%ERRORLEVEL%"
if not "%CLOSE_EXIT%"=="0" pause
exit /b %CLOSE_EXIT%
