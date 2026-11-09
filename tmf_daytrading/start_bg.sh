#!/usr/bin/env bash
# start_bg.sh - 在背景 (nohup) 獨立啟動 TMF 交易機器人 (不怕關閉終端機/IDE)
BASE_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"
cd "$BASE_DIR"

PID=$(pgrep -f "main\.py --mode" | head -n 1)

echo "================================================================="
echo "🚀 啟動 TMF 微型臺指期當沖機器人 (背景常駐守護模式)"
echo "================================================================="

if [ -n "$PID" ]; then
    echo "⚠️ 機器人已經在背景運行中！(PID: $PID)"
    echo "如需重啟，請先執行 ./stop.sh 停止後再啟動。"
    echo "================================================================="
    exit 0
fi

MODE="paper"
QTY="1"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode|-m)
            MODE="$2"
            shift 2
            ;;
        --qty|-q)
            QTY="$2"
            shift 2
            ;;
        live|paper|backtest)
            MODE="$1"
            shift
            ;;
        [0-9]*)
            QTY="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

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
