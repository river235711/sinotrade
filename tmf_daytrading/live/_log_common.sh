#!/usr/bin/env bash
# _log_common.sh - 供 status.sh / tail.sh 共用：判斷目前要看的模式 (live / paper)
# 優先順序：1. 命令列參數  2. 正在運行的機器人  3. 最近有寫入日誌的模式
BASE_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"

bot_pid() {
    pgrep -f "main\.py --mode" | head -n 1
}

resolve_mode() {
    local arg="$1"
    if [[ "$arg" == "live" || "$arg" == "paper" ]]; then
        echo "$arg"
        return
    fi
    local pid
    pid=$(bot_pid)
    if [ -n "$pid" ]; then
        local m
        m=$(ps -o args= -p "$pid" | sed -n 's/.*--mode \([a-z]*\).*/\1/p')
        if [[ "$m" == "live" || "$m" == "paper" ]]; then
            echo "$m"
            return
        fi
    fi
    local latest
    latest=$(ls -t "$BASE_DIR"/logs/live/trader_*.log "$BASE_DIR"/logs/paper/trader_*.log 2>/dev/null | head -n 1)
    if [ -n "$latest" ]; then
        basename "$(dirname "$latest")"
    else
        echo "live"
    fi
}
