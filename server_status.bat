@echo off
REM Shows what the bot on the SERVER is doing (same as status.bat, but for the server).
chcp 65001 >nul
ssh crypto-desk "cd /root/crypto-desk && docker compose ps --format 'table {{.Service}}\t{{.Status}}' && echo && docker compose exec -T -u desk bot python -m desk.status"
echo.
pause
