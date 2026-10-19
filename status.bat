@echo off
REM Shows what the bot is doing (reads the database only; safe while the bot runs).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.status %*
echo.
pause
