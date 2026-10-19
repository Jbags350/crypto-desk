@echo off
REM ============================================================
REM  Crypto Desk - PAPER TRADING (fake money)
REM  Leave this window open. Ctrl+C stops the bot.
REM  If the bot ever crashes, this window restarts it after 15s.
REM ============================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
:loop
".venv\Scripts\python.exe" -m desk.main %*
set CODE=%errorlevel%
if "%CODE%"=="0" goto :end
if "%CODE%"=="2" goto :end
echo.
echo *** The bot stopped unexpectedly (code %CODE%). Restarting in 15 seconds. Close this window to stop. ***
timeout /t 15 /nobreak >nul
goto loop
:end
echo.
pause
