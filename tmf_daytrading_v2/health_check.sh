#!/usr/bin/env bash
# health_check.sh - 執行 TMF V2 當沖系統開盤前健康巡檢
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/runtime/_log_common.sh"
"$PYTHON_BIN" -B runtime/health_check.py "$@"
