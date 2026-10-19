@echo off
REM System check: live CoinSpot prices, global price comparison,
REM candle freshness and API key tests. Read-only - places no orders.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.check %*
echo.
pause
