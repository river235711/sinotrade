#!/usr/bin/env bash
# start_bg.sh - 在背景 (nohup) 獨立啟動 TMF 交易機器人 (不怕關閉終端機/IDE)
BASE_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"
cd "$BASE_DIR"

PID=$(pgrep -f "python.*main.py" | head -n 1)

echo "================================================================="
echo "🚀 啟動 TMF 微型臺指期當沖機器人 (背景常駐守護模式)"
echo "================================================================="

if [ -n "$PID" ]; then
    echo "⚠️ 機器人已經在背景運行中！(PID: $PID)"
    echo "如需重啟，請先執行 ./stop.sh 停止後再啟動。"
    echo "================================================================="
    exit 0
fi

MODE="${1:-paper}"
QTY="${2:-1}"

echo "啟動參數: 模式=[$MODE], 下單口數=[$QTY]"
mkdir -p logs

nohup /mnt/c/Users/ting0/.gemini/antigravity/scratch/sinotrade_trader/venv/bin/python main.py --mode "$MODE" --qty "$QTY" > logs/nohup_trader.log 2>&1 &
NEW_PID=$!

sleep 2
if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "✅ 機器人已成功在背景啟動！"
    echo "📌 背景 PID: $NEW_PID"
    echo "📂 即時日誌檔: logs/nohup_trader.log"
    echo "💡 您可以隨時執行 ./status.sh 查看狀態，或 ./stop.sh 停止程式。"
else
    echo "❌ 啟動失敗，請檢查 logs/nohup_trader.log 查看錯誤原因。"
fi
echo "================================================================="
