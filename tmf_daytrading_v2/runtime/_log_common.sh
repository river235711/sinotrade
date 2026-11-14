#!/usr/bin/env bash
# _log_common.sh - 供各腳本共用：自動定位 V2 專案根目錄、Python 虛擬環境與運行模式
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

find_python() {
    local c
    for c in \
        "$BASE_DIR/venv/bin/python" \
        "$BASE_DIR/.venv/bin/python" \
        "$BASE_DIR/../sinotrade_trader/venv/bin/python" \
        "$BASE_DIR/../venv/bin/python" \
        "$(command -v python3 2>/dev/null)" \
        "$(command -v python 2>/dev/null)"; do
        if [ -n "$c" ] && [ -x "$c" ]; then
            echo "$c"
            return
        fi
    done
    echo "python3"
}

PYTHON_BIN="$(find_python)"

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
        m=$(ps -o args= -p "$pid" 2>/dev/null | sed -n 's/.*--mode \([a-z]*\).*/\1/p')
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
