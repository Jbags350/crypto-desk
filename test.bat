@echo off
REM Runs the automated tests. Everything should say "passed".
REM Add --network to also run tests that call the real public APIs.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m pytest -o addopts="" -q %*
echo.
pause
