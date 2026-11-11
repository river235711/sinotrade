#!/usr/bin/env bash
# query_account.sh - 查詢永豐金期貨實體帳戶部位與保證金 (跨平台相容 macOS / Linux / WSL)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/live/_log_common.sh"
"$PYTHON_BIN" -B live/query_account.py "$@"
