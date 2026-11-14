"""
TMF (微型臺指期) 當沖交易系統 V2 主入口
支援 backtest / paper / live 三種模式與即時外部指令。
"""
import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from typing import List, Optional

from config import ShioajiConfig
from gateways.shioaji_live import ShioajiLiveGateway
from gateways.shioaji_paper import ShioajiPaperGateway
from runtime.notifier import VALID_MODES, get_mode_log_dir, setup_mode_logging
from runtime.session_fsm import SessionFSM

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _parse_mode_from_args(args: List[str]) -> Optional[str]:
    """從命令列參數串列解析運行中的機器人模式 (live / paper)"""
    if not any(os.path.basename(a) == "main.py" for a in args):
        return None
    if "--cmd" in args or any(a.startswith("--cmd=") for a in args):
        return None
    for i, a in enumerate(args):
        if a == "--mode" and i + 1 < len(args) and args[i + 1] in VALID_MODES:
            return args[i + 1]
        if a.startswith("--mode="):
            val = a.split("=", 1)[1]
            if val in VALID_MODES:
                return val
    return None


def _detect_running_mode() -> Optional[str]:
    """跨平台 (macOS / Linux / WSL) 偵測運行中的機器人模式"""
    my_pid = os.getpid()
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) == my_pid:
                    continue
                try:
                    args = shlex.split(parts[1])
                except ValueError:
                    args = parts[1].split()
                mode = _parse_mode_from_args(args)
                if mode:
                    return mode
            return None
    except Exception:
        pass

    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return None
    my_pid_str = str(my_pid)
    for pid in os.listdir(proc_dir):
        if not pid.isdigit() or pid == my_pid_str:
            continue
        try:
            with open(os.path.join(proc_dir, pid, "cmdline"), "rb") as f:
                args = [a.decode("utf-8", "ignore") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        mode = _parse_mode_from_args(args)
        if mode:
            return mode
    return None


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

    args = parser.parse_args()

    if args.cmd:
        explicit_mode = args.mode if "--mode" in sys.argv and args.mode in VALID_MODES else None
        send_bot_command(args.cmd, explicit_mode)
        return

    if args.mode == "backtest":
        from backtester.run_20days_backtest import main as run_backtest_main
        run_backtest_main()
        return

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
