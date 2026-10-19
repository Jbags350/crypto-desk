@echo off
REM ============================================================
REM  Crypto Desk - WATCHDOG (optional, run next to paper.bat)
REM  Sends you a Telegram message if the bot stops checking
REM  prices for 10+ minutes, and again when it comes back.
REM  It can't message you if this whole PC is off or asleep:
REM  for that, see HEALTHCHECKS_URL in the README.
REM ============================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
echo Watchdog running. Leave this window open (Ctrl+C stops it).
".venv\Scripts\python.exe" -m desk.watchdog %*
echo.
pause
