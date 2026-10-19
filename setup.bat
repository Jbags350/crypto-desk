@echo off
REM ============================================================
REM  Crypto Desk - one-time setup (safe to run again any time)
REM  Creates a private Python environment in .venv and installs
REM  everything the bot needs. Also creates your .env file.
REM ============================================================
cd /d "%~dp0"
echo.
echo === Crypto Desk setup ===
echo.

set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3.14"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)

echo Installing / updating packages (this can take a few minutes the first time) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt --quiet
if errorlevel 1 goto :fail

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created your .env file. Open it with Notepad later to add API keys.
)

echo.
echo Setup complete. Next: double-click test.bat, then check.bat
echo.
pause
exit /b 0

:fail
echo.
echo *** SETUP FAILED - read the messages above. ***
echo.
pause
exit /b 1
