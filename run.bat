@echo off
rem Starts the weekly sync. In Windows Task Scheduler, set "Program/script" to the full path of this file.
rem Arguments are passed on, e.g.  run.bat --dry-run
setlocal
cd /d "%~dp0"

rem Task Scheduler may not have the Python launcher on PATH: then use the place it installs itself
set "PY=py"
where py >nul 2>nul || set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not "%PY%"=="py" if not exist "%PY%" (
    echo Python launcher "py" not found. Install Python 3.12+ from python.org, with the launcher.
    exit /b 9009
)

"%PY%" merchant_sync.py %*
exit /b %ERRORLEVEL%
