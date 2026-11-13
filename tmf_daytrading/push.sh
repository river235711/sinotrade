#!/usr/bin/env bash
# push.sh - 將工作目錄更動同步至 Git 倉庫並推送至 GitHub (跨平台相容 macOS / Linux / WSL)
set -e

DATA_ONLY=0
if [ "${1:-}" = "--data-only" ]; then
    DATA_ONLY=1
    shift
fi

MSG="${1:-Update TMF daytrading system}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEPARATE_REPO="$SRC_DIR/../sinotrade_repo"
SUB_PREFIX=""

if [ -d "$SEPARATE_REPO/.git" ]; then
    REPO_DIR="$SEPARATE_REPO"
    SUB_PREFIX="tmf_daytrading/"
    if [ "$DATA_ONLY" = "1" ]; then
        echo "🔄 僅同步 data/ 與 reports/ 至 Git 倉庫 (--data-only)..."
        if [ -d "$SRC_DIR/data" ]; then
            mkdir -p "$REPO_DIR/tmf_daytrading/data"
            rsync -av --exclude='Daily_*.csv' --exclude='*.log' "$SRC_DIR/data/" "$REPO_DIR/tmf_daytrading/data/"
        fi
        if [ -d "$SRC_DIR/reports" ]; then
            mkdir -p "$REPO_DIR/tmf_daytrading/reports"
            rsync -av --exclude='*.log' "$SRC_DIR/reports/" "$REPO_DIR/tmf_daytrading/reports/"
        fi
    else
        echo "🔄 同步檔案至 Git 倉庫..."
        rsync -av --delete \
          --exclude='.git/' \
          --exclude='.env' \
          --exclude='certs/' \
          --exclude='*.pfx' \
          --exclude='*.pem' \
          --exclude='logs/' \
          --exclude='venv/' \
          --exclude='.venv/' \
          --exclude='__pycache__/' \
          --exclude='Daily_*.csv' \
          --exclude='*.log' \
          "$SRC_DIR/" "$REPO_DIR/tmf_daytrading/"
    fi
else
    REPO_DIR="$SRC_DIR"
fi

cd "$REPO_DIR"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$DATA_ONLY" = "1" ]; then
    if [ -d "${SUB_PREFIX}data" ]; then
        git add -- "${SUB_PREFIX}data"
    fi
    if [ -d "${SUB_PREFIX}reports" ]; then
        git add -- "${SUB_PREFIX}reports"
    fi
    git reset -q -- "${SUB_PREFIX}data/Daily_*.csv" 2>/dev/null || true
else
    git add -A
fi
if git diff --staged --quiet; then
  echo "✅ 沒有需要提交的更動 (已是最新狀態，分支: $CURRENT_BRANCH)。"
else
  git commit -m "$MSG"
  echo "🔄 推送前同步遠端最新更動 (rebase 防止 non-fast-forward)..."
  git pull --rebase origin "$CURRENT_BRANCH"
  echo "🚀 推送更動至 GitHub 分支 [$CURRENT_BRANCH]..."
  git push origin "$CURRENT_BRANCH"
  echo "🎉 成功推送到 GitHub (river235711/sinotrade，分支: $CURRENT_BRANCH)！"
fi
