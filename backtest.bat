@echo off
REM Backtest: replays past prices through the scanner, Risk Manager and exits.
REM First run downloads price history (a minute or two). Opens the report when done.
REM Options: backtest.bat --days 365   or   backtest.bat --coins BTC,ETH --spread 0.1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.backtest --open %*
echo.
pause
