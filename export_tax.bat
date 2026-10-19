@echo off
REM Australian tax CSV. Example: export_tax.bat --fy 2026  (add --include-paper to include paper trades)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m desk.tax_export %*
echo.
pause
