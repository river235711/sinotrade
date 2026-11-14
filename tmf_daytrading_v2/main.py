"""
TMF (微型臺指期) 當沖交易系統 V2 主入口
支援 backtest / paper / live 三種模式與即時外部指令。
"""
import argparse
import datetime
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from runtime.notifier import VALID_MODES, get_mode_log_dir, setup_mode_logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_this_entrypoint(arg: str, base_dir: Optional[str]) -> bool:
    """判斷參數是否指向本專案 main.py（避免誤抓 V1 tmf_daytrading/main.py）"""
    if os.path.basename(arg) != "main.py":
        return False
    if base_dir is None or os.sep not in arg and "/" not in arg:
        return True
    return os.path.abspath(arg) == os.path.abspath(os.path.join(base_dir, "main.py"))


def _parse_mode_from_args(args: List[str], base_dir: Optional[str] = None) -> Optional[str]:
    """從命令列參數串列解析運行中的機器人模式 (live / paper)"""
    if not any(_is_this_entrypoint(a, base_dir) for a in args):
        return None
    for skip_flag in ("--cmd", "--status", "--tail"):
        if skip_flag in args or any(a.startswith(f"{skip_flag}=") for a in args):
            return None
    for i, a in enumerate(args):
        if a == "--mode" and i + 1 < len(args) and args[i + 1] in VALID_MODES:
            return args[i + 1]
        if a.startswith("--mode="):
            val = a.split("=", 1)[1]
            if val in VALID_MODES:
                return val
    return None


def _iter_running_processes() -> List[Tuple[str, List[str]]]:
    """跨平台列舉目前運行中的 (pid, argv) 清單"""
    my_pid = str(os.getpid())
    processes: List[Tuple[str, List[str]]] = []
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) < 2 or not parts[0].isdigit() or parts[0] == my_pid:
                    continue
                try:
                    args = shlex.split(parts[1])
                except ValueError:
                    args = parts[1].split()
                processes.append((parts[0], args))
            return processes
    except Exception:
        pass

    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return processes
    for pid in os.listdir(proc_dir):
        if not pid.isdigit() or pid == my_pid:
            continue
        try:
            with open(os.path.join(proc_dir, pid, "cmdline"), "rb") as f:
                args = [a.decode("utf-8", "ignore") for a in f.read().split(b"\0") if a]
            processes.append((pid, args))
        except OSError:
            continue
    return processes


def _find_running_bot(base_dir: str = BASE_DIR, target_mode: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """回傳運行中的 V2 (pid, mode)，優先查 logs/bot.pid 再掃描行程"""
    pid_file = os.path.join(base_dir, "logs", "bot.pid")
    if os.path.exists(pid_file):
        try:
            pid_str = open(pid_file, encoding="utf-8").read().strip()
            if pid_str.isdigit():
                pid_int = int(pid_str)
                os.kill(pid_int, 0)
                for proc_pid, args in _iter_running_processes():
                    if proc_pid == pid_str:
                        mode = _parse_mode_from_args(args, base_dir=base_dir)
                        if mode and (target_mode is None or mode == target_mode):
                            return pid_str, mode
        except OSError:
            pass

    for proc_pid, args in _iter_running_processes():
        mode = _parse_mode_from_args(args, base_dir=base_dir)
        if mode and (target_mode is None or mode == target_mode):
            return proc_pid, mode
    return None, None


def _detect_running_mode() -> Optional[str]:
    """跨平台 (macOS / Linux / WSL) 偵測運行中的 V2 機器人模式"""
    _, mode = _find_running_bot(base_dir=BASE_DIR)
    return mode


def resolve_target_mode(explicit_mode: Optional[str] = None, base_dir: str = BASE_DIR) -> str:
    """決定 status / tail 目標模式：明示 > 運行中 > 最近日誌 > live"""
    if explicit_mode in VALID_MODES:
        return explicit_mode
    _, running_mode = _find_running_bot(base_dir=base_dir)
    if running_mode in VALID_MODES:
        return running_mode

    today = datetime.date.today().strftime("%Y%m%d")
    candidates: List[Tuple[float, str]] = []
    for m in VALID_MODES:
        log_dir = get_mode_log_dir(m, base_dir=os.path.join(base_dir, "logs"))
        for fname in (f"trader_{today}.log", "stdout.log", f"state_{today}.json"):
            fpath = os.path.join(log_dir, fname)
            if os.path.exists(fpath):
                candidates.append((os.path.getmtime(fpath), m))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return "live"


def _read_last_lines(path: str, count: int) -> List[str]:
    """安全讀取文字檔最後 N 行"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return lines[-count:]


def print_bot_status(mode: str, base_dir: str = BASE_DIR, pid: Optional[str] = None) -> None:
    """輸出指定模式機器人的運行狀態、當日部位、最新日誌與錯誤摘要"""
    live_pid = pid if pid else _find_running_bot(base_dir=base_dir, target_mode=mode)[0]
    today = datetime.date.today().strftime("%Y%m%d")
    log_dir = get_mode_log_dir(mode, base_dir=os.path.join(base_dir, "logs"))
    state_path = os.path.join(log_dir, f"state_{today}.json")
    trader_log = os.path.join(log_dir, f"trader_{today}.log")
    stdout_log = os.path.join(log_dir, "stdout.log")

    print("=" * 60)
    print(f"📊 TMF 交易機器人 V2 狀態 ([{mode.upper()}] 模式)")
    print(f"{'🟢 運行中 (PID: ' + live_pid + ')' if live_pid else '🔴 未運行'}")
    print(f"📁 日誌目錄: {log_dir}")

    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                st = json.load(f)
            print(
                f"📌 當日狀態 ({today}): 持倉={st.get('position', 0)} | "
                f"成交={st.get('trade_count', 0)} | 停單={st.get('entries_halted', False)}"
            )
            print(
                f"📐 ORB 區間: low={st.get('orb_low')}, high={st.get('orb_high')} "
                f"(calculated={st.get('orb_calculated', False)})"
            )
            print(f"📈 最高浮盈={st.get('highest_profit', 0)} | 更新時間={st.get('updated_at', '-')}")
        except Exception as exc:
            print(f"⚠️ 讀取狀態檔失敗 ({state_path}): {exc}")
    else:
        print(f"📌 當日狀態 ({today}): 尚無 state_{today}.json")

    recent_logs = _read_last_lines(trader_log, 5)
    if recent_logs:
        print(f"\n📜 最新交易日誌 ({os.path.basename(trader_log)} 末 {len(recent_logs)} 行):")
        for line in recent_logs:
            print(f"  {line}")
    else:
        print(f"\n📜 最新交易日誌: 尚無 {os.path.basename(trader_log)}")

    stdout_tail = _read_last_lines(stdout_log, 200)
    err_markers = ("Traceback", "[ERROR]", "[CRITICAL]")
    errors = [ln for ln in stdout_tail if any(mk in ln for mk in err_markers)]
    if errors:
        print(f"\n⚠️ stdout.log 近期異常 ({len(errors)} 筆，顯示末 3 行):")
        for line in errors[-3:]:
            print(f"  {line}")
    elif os.path.exists(stdout_log):
        print("\n✅ stdout.log 末 200 行無明顯異常")
    print("=" * 60)


def tail_bot_logs(mode: str, base_dir: str = BASE_DIR, stdout_only: bool = False) -> None:
    """即時追蹤機器人日誌 (支援 trader_YYYYMMDD.log 跨日無縫切換)"""
    log_dir = get_mode_log_dir(mode, base_dir=os.path.join(base_dir, "logs"))
    os.makedirs(log_dir, exist_ok=True)
    stop_requested = False

    def _on_signal(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _on_signal)

    def _target_for_date(date_str: str) -> str:
        return os.path.join(log_dir, "stdout.log" if stdout_only else f"trader_{date_str}.log")

    current_date = datetime.date.today().strftime("%Y%m%d")
    current_path = _target_for_date(current_date)
    waited_notice = False
    handle = None

    print(f"👀 即時追蹤 [{mode.upper()}] 日誌: {current_path} (Ctrl+C 結束)")
    try:
        while not stop_requested:
            today = datetime.date.today().strftime("%Y%m%d")
            if not stdout_only and today != current_date:
                next_path = _target_for_date(today)
                if os.path.exists(next_path):
                    if handle is not None:
                        handle.close()
                        handle = None
                    current_date = today
                    current_path = next_path
                    waited_notice = False
                    print(f"\n🔄 跨日自動切換日誌: {current_path}")

            if handle is None:
                if not os.path.exists(current_path):
                    if not waited_notice:
                        print(f"⏳ 等待日誌檔建立: {current_path}")
                        waited_notice = True
                    time.sleep(0.5)
                    continue
                for line in _read_last_lines(current_path, 30):
                    print(line, flush=True)
                handle = open(current_path, encoding="utf-8", errors="replace")
                handle.seek(0, os.SEEK_END)
                waited_notice = False

            chunk = handle.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                try:
                    if os.path.exists(current_path) and os.path.getsize(current_path) < handle.tell():
                        handle.seek(0)
                except OSError:
                    pass
                time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        if handle is not None:
            handle.close()


def send_bot_command(cmd: str, mode: Optional[str] = None):
    """把即時指令寫入 logs/<mode>/command.json"""
    target = mode or _detect_running_mode()
    if target not in VALID_MODES:
        print("❌ 找不到正在運行的機器人，請加上 --mode live 或 --mode paper 指定目標。")
        sys.exit(1)
    log_dir = get_mode_log_dir(target, base_dir=os.path.join(BASE_DIR, "logs"))
    os.makedirs(log_dir, exist_ok=True)
    cmd_path = os.path.join(log_dir, "command.json")
    with open(cmd_path, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "time": datetime.datetime.now().strftime("%H:%M:%S")}, f)
    print(f"⚡ 已發送即時指令 [{cmd}] 至運行中的 {target} 機器人 (1 秒內立即執行)！")


def main():
    parser = argparse.ArgumentParser(description="TMF 微型臺指期 ORB Pro 當沖交易系統 (V2)")
    parser.add_argument(
        "--mode",
        choices=["backtest", "paper", "live"],
        default="backtest",
        help="執行模式: backtest (歷史回測), paper (Shioaji模擬自動下單), live (正式實盤自動下單)",
    )
    parser.add_argument("--contract", default="TMFR1", help="期貨合約代碼或月份 (預設 TMFR1)")
    parser.add_argument("--qty", type=int, default=1, help="每次下單口數 (預設 1 口)")
    parser.add_argument(
        "--cmd",
        choices=["close", "close_and_halt", "resume"],
        default=None,
        help="向運行中的機器人發送即時指令",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="查詢機器人運行狀態、當日部位與最新日誌摘要",
    )
    parser.add_argument(
        "--tail",
        nargs="?",
        const="log",
        choices=["log", "stdout"],
        default=None,
        help="即時追蹤日誌 (預設 log 追蹤每日交易日誌並支援跨日切換；stdout 追蹤標準輸出)",
    )
    parser.add_argument(
        "--pid",
        default=None,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()
    has_explicit_mode = any(a == "--mode" or a.startswith("--mode=") for a in sys.argv[1:])
    explicit_mode = args.mode if has_explicit_mode and args.mode in VALID_MODES else None

    if args.cmd:
        send_bot_command(args.cmd, explicit_mode)
        return

    if args.status:
        target_mode = resolve_target_mode(explicit_mode, base_dir=BASE_DIR)
        print_bot_status(target_mode, base_dir=BASE_DIR, pid=args.pid)
        return

    if args.tail:
        target_mode = resolve_target_mode(explicit_mode, base_dir=BASE_DIR)
        tail_bot_logs(target_mode, base_dir=BASE_DIR, stdout_only=(args.tail == "stdout"))
        return

    if args.mode == "backtest":
        from backtester.run_20days_backtest import main as run_backtest_main
        run_backtest_main()
        return

    from config import ShioajiConfig
    from gateways.shioaji_live import ShioajiLiveGateway
    from gateways.shioaji_paper import ShioajiPaperGateway
    from runtime.session_fsm import SessionFSM

    log_dir = setup_mode_logging(args.mode, base_dir=os.path.join(BASE_DIR, "logs"))
    sj_cfg = ShioajiConfig()
    if args.mode == "paper":
        print(f"🚀 啟動 V2 Shioaji 模擬交易模式... 日誌: {log_dir}")
        gateway = ShioajiPaperGateway(sj_cfg=sj_cfg, contract_code=args.contract)
    else:
        print(f"⚠️ 啟動 V2 Shioaji 正式實盤自動下單模式！日誌: {log_dir}")
        gateway = ShioajiLiveGateway(sj_cfg=sj_cfg, contract_code=args.contract)

    fsm = SessionFSM(
        gateway=gateway,
        base_dir=BASE_DIR,
        log_dir=log_dir,
        quantity=args.qty,
    )
    fsm.run()


if __name__ == "__main__":
    main()
