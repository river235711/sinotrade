#!/usr/bin/env bash
set -e

MSG="${1:-Update TMF daytrading system}"
SRC_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"
REPO_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/sinotrade_repo"

echo "🔄 同步檔案至 Git 倉庫..."
rsync -av --delete \
  --exclude='.env' \
  --exclude='*.pfx' \
  --exclude='*.pem' \
  --exclude='logs/' \
  --exclude='__pycache__/' \
  --exclude='*.log' \
  --exclude='push.sh' \
  "$SRC_DIR/" "$REPO_DIR/tmf_daytrading/"

cd "$REPO_DIR"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git add -A
if git diff --staged --quiet; then
  echo "✅ 沒有需要提交的更動 (已是最新狀態，分支: $CURRENT_BRANCH)。"
else
  git commit -m "$MSG"
  echo "🚀 推送更動至 GitHub 分支 [$CURRENT_BRANCH]..."
  git push origin "$CURRENT_BRANCH"
  echo "🎉 成功推送到 GitHub (river235711/sinotrade，分支: $CURRENT_BRANCH)！"
fi
