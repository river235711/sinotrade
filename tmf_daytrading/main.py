"""
TMF (微型臺指期) 當沖交易系統主入口
支援:
  1. 回測模式 (backtest): 載入期交所歷史行情回測
  2. 模擬下單模式 (paper): 連接永豐 Shioaji 模擬環境 (simulation=True) 自動看盤與模擬下單
  3. 正式實盤模式 (live):  連接永豐 Shioaji 正式環境 (simulation=False) 掛載憑證自動下單
"""
import argparse
from run_backtest import main as run_backtest_main
from live.shioaji_trader import ShioajiTMFTrader

def main():
    parser = argparse.ArgumentParser(description="TMF 微型臺指期 ORB Pro 當沖交易系統")
    parser.add_argument("--mode", choices=["backtest", "paper", "live"], default="backtest",
                        help="執行模式: backtest (歷史回測), paper (Shioaji模擬自動下單), live (正式實盤自動下單)")
    parser.add_argument("--contract", default="TMFR1", help="期貨合約代碼或月份 (預設 TMFR1 近月連續合約)")
    parser.add_argument("--qty", type=int, default=1, help="每次下單口數 (預設 1 口)")
    
    args = parser.parse_args()

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
