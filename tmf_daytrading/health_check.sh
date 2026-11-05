#!/usr/bin/env bash
# health_check.sh - 執行 TMF 當沖系統開盤前健康巡檢 (Linux / WSL)
cd "$(dirname "$0")"
/mnt/c/Users/ting0/.gemini/antigravity/scratch/sinotrade_trader/venv/bin/python health_check.py "$@"
