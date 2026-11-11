#!/usr/bin/env bash
# pull.sh - 從 GitHub (river235711/sinotrade) 拉取最新更動並自動清理多餘檔案 (跨平台相容 macOS / Linux / WSL)
# 保護名單 (絕不刪除): .env, *.pfx, *.pem, logs/, venv/, .venv/, push.sh
set -e

DEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEPARATE_REPO="$DEST_DIR/../sinotrade_repo"

echo "================================================================="
echo "🔄 正在從 GitHub 拉取最新程式碼並同步清理目錄..."
echo "================================================================="

if [ -d "$SEPARATE_REPO/.git" ]; then
    # 架構 A：分離式 Git 倉庫 (sinotrade_repo -> tmf_daytrading)
    cd "$SEPARATE_REPO"
    git pull origin main
    git clean -fd

    echo ""
    echo "🧹 正在將最新檔案同步至執行目錄 (並自動清除比 Git 多出的舊檔)..."
    rsync -av --delete \
      --exclude='.git/' \
      --exclude='.env' \
      --exclude='*.pfx' \
      --exclude='*.pem' \
      --exclude='logs/' \
      --exclude='venv/' \
      --exclude='.venv/' \
      --exclude='Daily_*.csv' \
      --exclude='push.sh' \
      "$SEPARATE_REPO/tmf_daytrading/" "$DEST_DIR/"
elif git -C "$DEST_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # 架構 B：直接在 Git 倉庫內執行 (例如 Mac 直接 git clone 之目錄)
    cd "$DEST_DIR"
    git pull origin main
    echo ""
    echo "🧹 正在清理本地多餘的未追蹤檔案與快取 (保留 .env, 憑證與 logs/)..."
    git clean -fdx \
      -e '.env' \
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

# 確保所有 .sh 腳本具備執行權限，且不殘留 __pycache__
chmod +x "$DEST_DIR"/*.sh "$DEST_DIR"/live/*.sh 2>/dev/null || true
find "$DEST_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "================================================================="
echo "✅ 更新與目錄清理完成！目前本地與 GitHub 完全一致。"
echo "================================================================="
