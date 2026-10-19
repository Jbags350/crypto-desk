@echo off
REM Telegram setup: checks your bot token, finds your chat ID, sends a test message.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.telegram_setup
echo.
pause
