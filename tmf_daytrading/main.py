"""
TMF (微型臺指期) 當沖交易系統主入口
支援:
  1. 回測模式 (backtest): 載入期交所歷史行情回測
  2. 模擬下單模式 (paper): 連接永豐 Shioaji 模擬環境 (simulation=True) 自動看盤與模擬下單
  3. 正式實盤模式 (live):  連接永豐 Shioaji 正式環境 (simulation=False) 掛載憑證自動下單

日誌位置 (paper / live 分開)：logs/<mode>/trader_YYYYMMDD.log，詳見 live/log_setup.py
"""
import os
import sys
import json
import datetime
import argparse
from typing import Optional
from live.log_setup import get_mode_log_dir, setup_mode_logging, VALID_MODES


def _detect_running_mode() -> Optional[str]:
    """從 /proc 找出正在運行的機器人模式 (live / paper)，找不到回傳 None"""
    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return None
    my_pid = str(os.getpid())
    for pid in os.listdir(proc_dir):
        if not pid.isdigit() or pid == my_pid:
            continue
        try:
            with open(os.path.join(proc_dir, pid, "cmdline"), "rb") as f:
                args = [a.decode("utf-8", "ignore") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not any(a.endswith("main.py") for a in args) or "--mode" not in args:
            continue
        idx = args.index("--mode")
        if idx + 1 < len(args) and args[idx + 1] in VALID_MODES:
            return args[idx + 1]
    return None


def send_bot_command(cmd: str, mode: Optional[str] = None):
    """把即時指令寫入對應模式的 logs/<mode>/command.json"""
    target = mode or _detect_running_mode()
    if target not in VALID_MODES:
        print("❌ 找不到正在運行的機器人，請加上 --mode live 或 --mode paper 指定要送給哪一支。")
        sys.exit(1)
    log_dir = get_mode_log_dir(target)
    os.makedirs(log_dir, exist_ok=True)
    cmd_path = os.path.join(log_dir, "command.json")
    with open(cmd_path, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "time": datetime.datetime.now().strftime("%H:%M:%S")}, f)
    print(f"⚡ 已發送即時指令 [{cmd}] 至運行中的 {target} 機器人 (1 秒內立即執行)！")


def main():
    parser = argparse.ArgumentParser(description="TMF 微型臺指期 ORB Pro 當沖交易系統")
    parser.add_argument("--mode", choices=["backtest", "paper", "live"], default="backtest",
                        help="執行模式: backtest (歷史回測), paper (Shioaji模擬自動下單), live (正式實盤自動下單)")
    parser.add_argument("--contract", default="TMFR1", help="期貨合約代碼或月份 (預設 TMFR1 近月連續合約)")
    parser.add_argument("--qty", type=int, default=1, help="每次下單口數 (預設 1 口)")
    parser.add_argument("--cmd", choices=["close", "close_and_halt", "resume"], default=None,
                        help="向運行中的機器人發送即時指令: close(立即平倉), close_and_halt(平倉並停止今日進場), resume(恢復進場)；"
                             "未指定 --mode 時自動偵測正在運行的模式")

    args = parser.parse_args()

    if args.cmd:
        explicit_mode = args.mode if "--mode" in sys.argv and args.mode in VALID_MODES else None
        send_bot_command(args.cmd, explicit_mode)
        return

    if args.mode == "backtest":
        from backtester.run_20days_backtest import main as run_backtest_main
        run_backtest_main()
        return

    # paper / live：先設定該模式的每日分檔日誌，再載入交易模組
    log_dir = setup_mode_logging(args.mode)
    from live.shioaji_trader import ShioajiTMFTrader

    if args.mode == "paper":
        print(f"🚀 啟動 Shioaji 模擬交易模式 (simulation=True，不下真單)... 日誌: {log_dir}")
        trader = ShioajiTMFTrader(simulation=True, quantity=args.qty)
    else:
        print(f"⚠️ 啟動 Shioaji 正式實盤自動下單模式 (simulation=False)！日誌: {log_dir}")
        trader = ShioajiTMFTrader(simulation=False, quantity=args.qty)

    if trader.initialize():
        if trader.load_tmf_contract(args.contract):
            trader.run_intraday_bot()


if __name__ == "__main__":
    main()
