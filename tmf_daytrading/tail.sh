#!/usr/bin/env bash
# tail.sh - 即時監看機器人主日誌 (等同 tail -f，且過午夜會自動切換到新一天的日誌檔)
# 用法: ./tail.sh            (自動判斷 live / paper)
#       ./tail.sh live|paper (指定模式)
#       ./tail.sh stdout     (查看目前模式的 SDK/錯誤輸出 stdout.log)
BASE_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"
source "$BASE_DIR/live/_log_common.sh"

if [ "$1" == "stdout" ]; then
    MODE=$(resolve_mode "$2")
    echo "📡 監看 logs/$MODE/stdout.log (Ctrl+C 離開，不影響機器人)"
    exec tail -n 30 -F "$BASE_DIR/logs/$MODE/stdout.log"
fi

MODE=$(resolve_mode "$1")
trap 'echo; exit 0' INT

while true; do
    TODAY=$(date +%Y%m%d)
    FILE="$BASE_DIR/logs/$MODE/trader_${TODAY}.log"
    SECS_TO_MIDNIGHT=$(( $(date -d 'tomorrow 00:00:00' +%s) - $(date +%s) + 2 ))
    echo "📡 監看 logs/$MODE/trader_${TODAY}.log (Ctrl+C 離開，不影響機器人)"
    # --foreground：讓 tail 留在終端機前景行程群組，Ctrl+C 才收得到 (否則 timeout 會另開行程群組，Ctrl+C 無效)
    timeout --foreground "$SECS_TO_MIDNIGHT" tail -n 20 -F "$FILE" 2>/dev/null
    [ $? -ne 124 ] && exit 0   # 非逾時結束 (例如 Ctrl+C) 就離開；逾時代表跨日，切換到新檔
done
