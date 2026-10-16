@echo off
REM ===================================================================
REM  DocFmtChecker - one-click publish to GitHub
REM  Put this file in the project root and double-click it.
REM  Content is English-only on purpose (avoids console encoding issues).
REM ===================================================================
setlocal
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 goto NOGIT

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 goto NOREPO

echo ============================================================
echo   DocFmtChecker  -  publish to GitHub
echo ============================================================
echo.

echo [1/4] Current changes:
git status --short
echo.

echo [2/4] Staging all changes ...
git add -A

set "MSG="
set /p MSG=Commit message (press Enter to use "update"): 
if "%MSG%"=="" set "MSG=update"

echo.
echo [3/4] Committing ...
git commit -m "%MSG%"
if errorlevel 1 (
    echo.
    echo [INFO] Nothing new to commit. Will try to push anyway.
)

echo.
echo [4/4] Pushing to GitHub ...
git push
if errorlevel 1 goto PUSHFAIL

echo.
echo ============================================================
echo   DONE. Refresh your GitHub page to see the changes.
echo ============================================================
echo.
pause
exit /b 0

:NOGIT
echo.
echo [ERROR] "git" was not found on this computer.
echo         Please install "Git for Windows" first:
echo             https://git-scm.com/download/win
echo         During setup you can click "Next" all the way through.
echo.
pause
exit /b 1

:NOREPO
echo.
echo [ERROR] This folder is not a Git repository.
echo         Make sure this .bat file sits in the project root,
echo         next to README.md and run.py
echo.
pause
exit /b 1

:PUSHFAIL
echo.
echo [ERROR] Push failed. Common causes:
echo   - No remote configured yet. Run this once:
echo         git remote add origin https://github.com/YOUR_NAME/YOUR_REPO.git
echo         git push -u origin main
echo   - Not logged in yet. Let the browser window finish the login.
echo   - Network blocked. Try again later or use a proxy.
echo.
pause
exit /b 1
