@echo off
REM Market scan: indicators, setups, news and (if flagged) the AI news score.
REM Read-only - places no orders. Try: scan.bat --score-all
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.scan %*
echo.
pause
