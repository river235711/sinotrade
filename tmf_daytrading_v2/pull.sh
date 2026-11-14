#!/usr/bin/env bash
# pull.sh - 從 GitHub (river235711/sinotrade) 拉取 V2 最新更動並自動清理多餘檔案
# 保護名單: .env, *.pfx, *.pem, logs/, venv/, .venv/, push.sh
set -e

DEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEPARATE_REPO="$DEST_DIR/../sinotrade_repo"

echo "================================================================="
echo "🔄 正在從 GitHub 拉取 V2 最新程式碼並同步清理目錄..."
echo "================================================================="

if [ -d "$SEPARATE_REPO/.git" ]; then
    cd "$SEPARATE_REPO"
    git pull origin main
    git clean -fd

    echo ""
    echo "🧹 正在將 V2 最新檔案同步至執行目錄..."
    rsync -av --delete \
      --exclude='.git/' \
      --exclude='.env' \
      --exclude='certs/' \
      --exclude='*.pfx' \
      --exclude='*.pem' \
      --exclude='logs/' \
      --exclude='venv/' \
      --exclude='.venv/' \
      --exclude='Daily_*.csv' \
      --exclude='push.sh' \
      "$SEPARATE_REPO/tmf_daytrading_v2/" "$DEST_DIR/"
elif git -C "$DEST_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    cd "$DEST_DIR"
    git pull origin main
    git clean -fdx \
      -e '.env' \
      -e 'certs/' \
      -e '*.pfx' \
      -e '*.pem' \
      -e 'logs/' \
      -e 'venv/' \
      -e '.venv/' \
      -e 'push.sh'
else
    echo "❌ 找不到 Git 倉庫 ($SEPARATE_REPO 或 $DEST_DIR)！"
    exit 1
fi

chmod +x "$DEST_DIR"/*.sh "$DEST_DIR"/runtime/*.sh 2>/dev/null || true
find "$DEST_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "================================================================="
echo "✅ V2 更新與目錄清理完成！目前本地與 GitHub 完全一致。"
echo "================================================================="
