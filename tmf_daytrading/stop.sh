#!/usr/bin/env bash
# stop.sh - 安全停止 TMF 交易機器人
BASE_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"
cd "$BASE_DIR"

PIDS=$(pgrep -f "python.*main.py")

echo "================================================================="
echo "🛑 安全停止 TMF 微型臺指期當沖機器人"
echo "================================================================="

if [ -z "$PIDS" ]; then
    echo "ℹ️ 目前沒有正在運行的交易機器人程序。"
    echo "================================================================="
    exit 0
fi

echo "正在向機器人程序 (PID: $PIDS) 發送安全關閉信號 (SIGINT)..."
kill -INT $PIDS 2>/dev/null

for i in {1..5}; do
    sleep 1
    PIDS_LEFT=$(pgrep -f "python.*main.py")
    if [ -z "$PIDS_LEFT" ]; then
        break
    fi
done

PIDS_LEFT=$(pgrep -f "python.*main.py")
if [ -n "$PIDS_LEFT" ]; then
    echo "⚠️ 程式未於 5 秒內優雅退出，強制終止 (PID: $PIDS_LEFT)..."
    kill -9 $PIDS_LEFT 2>/dev/null
    sleep 1
fi

echo "✅ 機器人程序已成功停止！"
pkill -f "update_dashboard.py" 2>/dev/null
echo "================================================================="
