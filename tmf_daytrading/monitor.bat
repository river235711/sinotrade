@echo off
chcp 65001 > nul
title TMF 即時報價與策略狀態串流監控
echo =================================================================
echo 📡 TMF 微型臺指期 - 實時報價與策略狀態串流 (每 3 秒自動更新)
echo 💡 提示：按 Ctrl + C 可退出監控視窗
echo =================================================================
powershell -Command "Get-Content -Path 'C:\Users\ting0\.gemini\antigravity\scratch\tmf_daytrading\logs\nohup_trader.log' -Wait -Tail 20"
