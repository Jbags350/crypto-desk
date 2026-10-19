@echo off
REM ============================================================
REM  Crypto Desk - PROP FIRM RESEARCH (Tradeify 247 2-Step)
REM  Replays the challenge from every week of the last 6 years
REM  with the recommended strategy and shows the pass rate,
REM  time to pass and funded payouts. Takes a few minutes.
REM  Add --compare to also see why day trading is not used.
REM ============================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.prop.research %*
echo.
pause
