@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>nul
if %errorlevel%==0 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if %errorlevel%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo Python 3 was not found on this computer.
    echo Install Python 3.10+ and try again.
    pause
    exit /b 1
)

%PYTHON_CMD% -u "%~dp0app\uvp_teleprompter.py"
