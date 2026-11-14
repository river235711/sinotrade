#!/usr/bin/env bash
# tail.sh - 即時監看機器人主日誌 (跨午夜自動換檔)
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
source "$BASE_DIR/runtime/_log_common.sh"

if [ "$1" == "stdout" ]; then
    MODE=$(resolve_mode "$2")
    echo "📡 監看 logs/$MODE/stdout.log (Ctrl+C 離開，不影響機器人)"
    exec tail -n 30 -F "$BASE_DIR/logs/$MODE/stdout.log"
fi

MODE=$(resolve_mode "$1")

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
