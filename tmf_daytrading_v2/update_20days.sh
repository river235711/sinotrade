#!/usr/bin/env bash
# update_20days.sh - 盤後手動滾動更新 20/60 日資料庫、重跑回測與推送 GitHub (V2)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/runtime/_log_common.sh"
exec "$PYTHON_BIN" -B runtime/update_20days.py "$@"
