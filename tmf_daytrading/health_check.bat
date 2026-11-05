@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo =================================================================
echo 🏥 執行 TMF 微型臺指期當沖系統 - 開盤前健康巡檢
echo =================================================================
wsl bash -c "cd /mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading && /mnt/c/Users/ting0/.gemini/antigravity/scratch/sinotrade_trader/venv/bin/python health_check.py %*"
echo.
pause
