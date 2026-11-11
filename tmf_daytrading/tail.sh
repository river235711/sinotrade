#!/usr/bin/env bash
# tail.sh - 即時監看機器人主日誌 (等同 tail -f，且過午夜會自動切換到新一天的日誌檔；相容 macOS / Linux / WSL)
# 用法: ./tail.sh            (自動判斷 live / paper)
#       ./tail.sh live|paper (指定模式)
#       ./tail.sh stdout     (查看目前模式的 SDK/錯誤輸出 stdout.log)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/live/_log_common.sh"

if [ "$1" == "stdout" ]; then
    MODE=$(resolve_mode "$2")
    echo "📡 監看 logs/$MODE/stdout.log (Ctrl+C 離開，不影響機器人)"
    exec tail -n 30 -F "$BASE_DIR/logs/$MODE/stdout.log"
fi

MODE=$(resolve_mode "$1")

# 使用內建 Python 計算距離午夜秒數並管理 tail 子程序，確保 macOS / Linux / WSL 皆可不中斷跨日自動換檔與 Ctrl+C 秒退
exec "$PYTHON_BIN" -B -c '
import datetime, os, signal, subprocess, sys, time

base_dir, mode = sys.argv[1], sys.argv[2]
proc = None

def _stop(signum=None, frame=None):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except Exception:
            proc.kill()
    print("", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)

while True:
    now = datetime.datetime.now()
    today = now.strftime("%Y%m%d")
    log_file = os.path.join(base_dir, "logs", mode, f"trader_{today}.log")
    print(f"📡 監看 logs/{mode}/trader_{today}.log (Ctrl+C 離開，不影響機器人)", flush=True)
    tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=2, microsecond=0)

    # 等待當日檔案建立後再啟動 tail -F (防止 macOS BSD tail 在跨午夜新檔尚未寫入前直接退出)
    while not os.path.exists(log_file):
        if datetime.datetime.now() >= tomorrow:
            break
        time.sleep(0.5)

    if datetime.datetime.now() >= tomorrow:
        print("\n🌅 已跨午夜，自動切換至今日日誌...", flush=True)
        continue

    secs = max(1.0, (tomorrow - datetime.datetime.now()).total_seconds())
    proc = subprocess.Popen(["tail", "-n", "20", "-F", log_file], stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=secs)
        break
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n🌅 已跨午夜，自動切換至今日日誌...", flush=True)
    except KeyboardInterrupt:
        _stop()
' "$BASE_DIR" "$MODE"
