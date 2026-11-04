@echo off
chcp 65001 > nul
wsl bash -c "cd /mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading && ./start_bg.sh %*"
pause
