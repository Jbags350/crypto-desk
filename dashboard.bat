@echo off
REM Crypto Desk dashboard: opens http://127.0.0.1:8501 in your browser.
REM Leave this window open while you use it; close it to stop the dashboard.
REM (The dashboard only reads the bot's database. The bot itself is paper.bat.)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
echo Starting the dashboard... your browser will open in a few seconds.
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8501"
".venv\Scripts\python.exe" -m streamlit run dashboard\app.py
pause
