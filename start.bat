@echo off
setlocal
set "PYEXE=%COMFY_CONSOLE_PYTHON%"
if not defined PYEXE set "PYEXE=python"
set APPSCRIPT=%~dp0app.py
echo.
echo === ComfyUI Web Frontend ===
echo Python : %PYEXE%
echo App    : %APPSCRIPT%
echo.
"%PYEXE%" "%APPSCRIPT%" %*
