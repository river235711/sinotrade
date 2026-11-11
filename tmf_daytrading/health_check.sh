#!/usr/bin/env bash
# health_check.sh - 執行 TMF 當沖系統開盤前健康巡檢 (跨平台相容 macOS / Linux / WSL)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/live/_log_common.sh"
"$PYTHON_BIN" -B health_check.py "$@"
