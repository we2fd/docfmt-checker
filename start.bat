@echo off
REM ============================================================
REM  DocFmtChecker - one-click launcher (Windows)
REM  Usage: double-click this file, or run: start.bat --port 8301
REM ============================================================
chcp 65001 >nul
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.10+ and make sure "python" works in cmd.
    echo         Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

python run.py %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo [INFO] Service exited with code %EXITCODE%.
    pause
)

endlocal
exit /b %EXITCODE%
