@echo off
REM Switches: control.bat pause / resume / resume --confirm / kill / kill --close-all / close-all
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.control %*
echo.
pause
