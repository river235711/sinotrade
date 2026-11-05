#!/usr/bin/env bash
# pull.sh - 從 GitHub (river235711/sinotrade) 拉取最新更動並同步至工作目錄
set -e

REPO_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/sinotrade_repo"
DEST_DIR="/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading"

echo "================================================================="
echo "🔄 正在從 GitHub 拉取最新程式碼..."
echo "================================================================="

cd "$REPO_DIR"
git pull origin main

echo ""
echo "🔄 正在同步更新至執行目錄..."
rsync -av \
  --exclude='.env' \
  --exclude='*.pfx' \
  --exclude='*.pem' \
  --exclude='logs/' \
  --exclude='__pycache__/' \
  --exclude='*.log' \
  "$REPO_DIR/tmf_daytrading/" "$DEST_DIR/"

echo "================================================================="
echo "✅ 更新完成！目前已是 GitHub 最新版本。"
echo "================================================================="
