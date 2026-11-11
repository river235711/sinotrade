#!/usr/bin/env bash
# update_20days.sh - 盤後手動滾動更新 20/60 日資料庫、重跑回測與推送 GitHub (跨平台相容 macOS / Linux / WSL)
# 用法: ./update_20days.sh               (更新今日資料、重跑回測並推上 GitHub)
#       ./update_20days.sh 2026-11-11    (指定特定日期補更新)
#       ./update_20days.sh --no-push     (僅本地更新與回測，不自動 push)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/live/_log_common.sh"
exec "$PYTHON_BIN" -B live/update_20days.py "$@"
