#!/usr/bin/env bash
# status.sh - 檢查 TMF 交易機器人即時運行狀態
BASE_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"
cd "$BASE_DIR"

PID=$(pgrep -f "main\.py --mode" | head -n 1)

echo "================================================================="
echo "📊 TMF 微型臺指期當沖機器人 - 運行狀態巡檢"
echo "================================================================="

if [ -n "$PID" ]; then
    echo "🟢 執行狀態: 【正在背景運行中】"
    echo "📌 行程 PID: $PID"
else
    echo "🔴 執行狀態: 【未運行 / 已停止】"
fi

TODAY=$(date +%Y%m%d)
STATE_FILE="$BASE_DIR/logs/state_${TODAY}.json"

if [ -f "$STATE_FILE" ]; then
    echo "-----------------------------------------------------------------"
    echo "📈 今日策略核心狀態 ($TODAY):"
    /mnt/c/Users/ting0/.gemini/antigravity/scratch/sinotrade_trader/venv/bin/python -c "
import json
try:
    with open('$STATE_FILE', 'r', encoding='utf-8') as f:
        d = json.load(f)
    pos = d.get('position', 0)
    pos_str = '0 口 (空手零曝險)' if pos == 0 else f'{pos:+d} 口'
    ol = d.get('orb_low')
    oh = d.get('orb_high')
    orb_str = f'{ol:.0f} ~ {oh:.0f}' if (ol and oh and d.get('orb_calculated')) else '尚未鎖定'
    halt = ' (🛑已停開新倉)' if d.get('entries_halted') else ''
    print(f' • 券商/策略部位: {pos_str}')
    print(f' • 今日交易次數: {d.get(\"trade_count\", 0)} 次{halt}')
    print(f' • 今日開盤ORB : {orb_str}')
    print(f' • 當前最高浮盈: +{d.get(\"highest_profit\", 0):.0f} 點')
    print(f' • 最後更新時間: {d.get(\"updated_at\", \"--\")}')
except Exception as e:
    print(f'讀取狀態出錯: {e}')
"
fi

LOG_FILE="$BASE_DIR/logs/nohup_trader.log"
if [ -f "$LOG_FILE" ]; then
    echo "-----------------------------------------------------------------"
    echo "⏱️ 最新日誌輸出 (最後 5 行):"
    tail -n 5 "$LOG_FILE"
fi

echo "================================================================="
