@echo off
chcp 65001 > nul
title TMF 即時報價與策略狀態串流監控
echo =================================================================
echo 📡 TMF 微型臺指期 - 實時報價與策略狀態串流 (自動判斷 live / paper，跨日自動換檔)
echo 💡 提示：按 Ctrl + C 可退出監控視窗 (不影響機器人)
echo =================================================================
wsl bash -c "cd /mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading && ./tail.sh %*"
