@echo off
REM ============================================================
REM  Crypto Desk - open the SERVER's dashboard on this PC
REM  Makes a secure tunnel to the server (the dashboard is never
REM  open to the internet), then opens it in your browser.
REM  Leave this window open while you use the dashboard;
REM  close it when you're done. The bot keeps running either way.
REM ============================================================
echo Connecting to the server... your browser opens in a few seconds.
echo Leave this window open while you use the dashboard. Close it when done.
start "" cmd /c "timeout /t 4 /nobreak >/dev/null & start http://127.0.0.1:8502"
ssh -N -o ExitOnForwardFailure=yes -L 8502:127.0.0.1:8501 crypto-desk
echo.
echo The connection to the server closed (or could not be made).
pause
