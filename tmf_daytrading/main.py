"""
TMF (微型臺指期) 當沖交易系統主入口
支援:
  1. 回測模式 (backtest): 載入期交所歷史行情回測
  2. 模擬下單模式 (paper): 連接永豐 Shioaji 模擬環境 (simulation=True) 自動看盤與模擬下單
  3. 正式實盤模式 (live):  連接永豐 Shioaji 正式環境 (simulation=False) 掛載憑證自動下單
"""
import os
import json
import datetime
import argparse
from run_backtest import main as run_backtest_main
from live.shioaji_trader import ShioajiTMFTrader

def send_bot_command(cmd: str):
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    cmd_path = os.path.join(log_dir, "command.json")
    with open(cmd_path, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "time": datetime.datetime.now().strftime("%H:%M:%S")}, f)
    print(f"⚡ 已發送即時指令 [{cmd}] 至運行中的機器人 (1 秒內立即執行)！")

def main():
    parser = argparse.ArgumentParser(description="TMF 微型臺指期 ORB Pro 當沖交易系統")
    parser.add_argument("--mode", choices=["backtest", "paper", "live"], default="backtest",
                        help="執行模式: backtest (歷史回測), paper (Shioaji模擬自動下單), live (正式實盤自動下單)")
    parser.add_argument("--contract", default="TMFR1", help="期貨合約代碼或月份 (預設 TMFR1 近月連續合約)")
    parser.add_argument("--qty", type=int, default=1, help="每次下單口數 (預設 1 口)")
    parser.add_argument("--cmd", choices=["close", "close_and_halt", "resume"], default=None,
                        help="向運行中的機器人發送即時指令: close(立即平倉), close_and_halt(平倉並停止今日進場), resume(恢復進場)")
    
    args = parser.parse_args()

    if args.cmd:
        send_bot_command(args.cmd)
        return

    if args.mode == "backtest":
        run_backtest_main()
    elif args.mode == "paper":
        print("🚀 啟動 Shioaji 模擬交易模式 (simulation=True，不下真單)...")
        trader = ShioajiTMFTrader(simulation=True, quantity=args.qty)
        if trader.initialize():
            if trader.load_tmf_contract(args.contract):
                trader.run_intraday_bot()
    elif args.mode == "live":
        print("⚠️ 啟動 Shioaji 正式實盤自動下單模式 (simulation=False)！")
        trader = ShioajiTMFTrader(simulation=False, quantity=args.qty)
        if trader.initialize():
            if trader.load_tmf_contract(args.contract):
                trader.run_intraday_bot()

if __name__ == "__main__":
    main()
