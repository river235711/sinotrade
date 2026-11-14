#!/usr/bin/env bash
# start_bg.sh - 在背景 (nohup) 獨立啟動 TMF V2 交易機器人
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/runtime/_log_common.sh"

PID=$(bot_pid)

echo "================================================================="
echo "🚀 啟動 TMF 微型臺指期當沖機器人 V2 (背景常駐守護模式)"
echo "================================================================="

if [ -n "$PID" ]; then
    RUNNING_MODE=$(ps -o args= -p "$PID" 2>/dev/null | sed -n 's/.*--mode \([a-z]*\).*/\1/p')
    echo "⚠️ 機器人已經在背景運行中！(PID: $PID, 模式: ${RUNNING_MODE:-未知})"
    echo "如需重啟或切換模式，請先執行 ./stop.sh 停止後再啟動。"
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
        live|paper)
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

if [[ "$MODE" != "live" && "$MODE" != "paper" ]]; then
    echo "❌ 不支援的模式: [$MODE]，背景常駐僅支援 live 或 paper。"
    echo "================================================================="
    exit 1
fi

LOG_DIR="logs/$MODE"
mkdir -p "$LOG_DIR"
STDOUT_LOG="$LOG_DIR/stdout.log"

if [ -f "$STDOUT_LOG" ] && [ "$(wc -c < "$STDOUT_LOG")" -gt 5242880 ]; then
    mv -f "$STDOUT_LOG" "$STDOUT_LOG.1"
fi

echo "啟動參數: 模式=[$MODE], 下單口數=[$QTY]"
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 啟動 main.py --mode $MODE --qty $QTY ==========" >> "$STDOUT_LOG"

PYTHONDONTWRITEBYTECODE=1 nohup "$PYTHON_BIN" -B main.py --mode "$MODE" --qty "$QTY" >> "$STDOUT_LOG" 2>&1 &
NEW_PID=$!

sleep 2
if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "✅ 機器人已成功在背景啟動！"
    echo "📌 背景 PID: $NEW_PID"
    echo "📂 主日誌 (每日分檔): $LOG_DIR/trader_$(date +%Y%m%d).log"
    echo "📂 SDK/錯誤輸出:     $STDOUT_LOG"
    echo "💡 即時監看: ./tail.sh  |  狀態: ./status.sh  |  停止: ./stop.sh"
else
    echo "❌ 啟動失敗，請檢查 $STDOUT_LOG 與 $LOG_DIR/trader_$(date +%Y%m%d).log 查看錯誤原因。"
fi
echo "================================================================="
