@echo off
REM Risk Manager demo: sample trade ideas checked against LIVE CoinSpot prices.
REM No AI, no orders. Try another coin with: risk_demo.bat --coin ETH
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.risk_demo %*
echo.
pause
