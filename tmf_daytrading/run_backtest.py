#!/usr/bin/env python3
"""
TMF (微型臺指期) 當沖交易程式 - 上一交易日回測腳本
回測日期: 2026-10-30 (五)
商品: 微型臺指期 (TMF 202611)
策略: ORB 開盤區間突破策略 + 移動停利 (Trailing Stop)
"""
import os
import shutil
import pandas as pd
from config import InstrumentConfig, ORBStrategyConfig
from backtester.engine import BacktestEngine

def main():
    print("=" * 70)
    print("🚀 TMF 微型臺指期貨 - 當沖量化交易回測 (上一交易日: 2026-10-30)")
    print("=" * 70)

    # 1. 初始化設定
    inst_cfg = InstrumentConfig(
        symbol="TMF",
        multiplier=10.0,            # 1 點 = 10 元台幣
        commission_per_order=12.0,  # 永豐等券商微台單邊手續費估計 ~12 元
        tax_rate=0.00001,           # 期交稅 10/100,000
        slippage_points=1.0         # 單邊滑價 1 點
    )

    strat_cfg = ORBStrategyConfig()

    # 2. 載入資料
    base_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(base_dir, "data", "Daily_2026_10_30.csv")

    engine = BacktestEngine(data_path=csv_path, inst_config=inst_cfg)
    print(f"📊 正在載入期交所逐筆行情資料: {csv_path} ...")
    kbars = engine.load_taifex_csv(target_date="20261030", contract_month="202611")
    print(f"✅ 日盤行情資料載入完成，共 {len(kbars)} 根 1分K 棒。")
    print(f"   開盤價: {kbars.iloc[0]['open']:.0f} | 最高價: {kbars['high'].max():.0f} | 最低價: {kbars['low'].min():.0f} | 收盤價: {kbars.iloc[-1]['close']:.0f}")

    # 3. 執行策略回測
    print("\n⚡ 正在執行 ORB 開盤區間突破當沖策略回測...")
    metrics = engine.run_orb_strategy(strat_cfg)

    orb_h = metrics["orb_high"]
    orb_l = metrics["orb_low"]
    print(f"📌 開盤 15 分鐘區間 (08:45 ~ 09:00):")
    print(f"   - ORB 上界 (做多關卡): {orb_h:.0f} 點")
    print(f"   - ORB 下界 (做空關卡): {orb_l:.0f} 點")
    print(f"   - 開盤區間振幅: {orb_h - orb_l:.0f} 點")

    # 4. 輸出交易紀錄明細
    trades_df = metrics["trades_df"]
    print("\n" + "=" * 70)
    print("📋 當日逐筆交易紀錄明細 (Trade Logs):")
    print("=" * 70)

    if trades_df.empty:
        print("當日無觸發任何交易信號。")
    else:
        for i, row in trades_df.iterrows():
            pos_label = "🟢 做多 (BUY)" if row['action'] == "BUY" else "🔴 做空 (SELL)"
            e_t = pd.to_datetime(row['entry_time']).strftime('%H:%M:%S')
            x_t = pd.to_datetime(row['exit_time']).strftime('%H:%M:%S')
            pnl_sign = "+" if row['pnl_pts'] > 0 else ""
            ntd_sign = "+" if row['net_ntd'] > 0 else ""
            
            print(f"第 {i+1} 筆交易:")
            print(f"  • 方向: {pos_label}")
            print(f"  • 進場時間: {e_t}  |  進場點位: {row['entry_price']:.0f}")
            print(f"  • 出場時間: {x_t}  |  出場點位: {row['exit_price']:.0f}")
            print(f"  • 出場原因: {row['reason']}")
            print(f"  • 原始點數損益: {pnl_sign}{row['pnl_pts']:.0f} 點")
            print(f"  • 扣除成本淨損益: {ntd_sign}{row['net_ntd']:,.0f} 元 (含手續費、期交稅、滑價)")
            print("-" * 50)

    # 5. 輸出整體績效指標
    print("\n" + "=" * 70)
    print("📈 當日回測整體績效摘要 (Performance Summary):")
    print("=" * 70)
    print(f"• 交易筆數 (Total Trades):      {metrics['total_trades']} 筆")
    print(f"• 勝率 (Win Rate):              {metrics['win_rate']:.1f} % ({metrics['win_count']} 勝 / {metrics['loss_count']} 負)")
    print(f"• 總獲利點數 (Total Points):     +{metrics['total_points']:.0f} 點")
    print(f"• 獲利因子 (Profit Factor):     {metrics['profit_factor']:.2f}")
    print(f"• 當日淨損益 (Net Profit NTD):  +NT$ {metrics['net_profit_ntd']:,.0f} 元")
    print(f"• 每筆平均淨利 (Avg Trade):     +NT$ {metrics['avg_trade_ntd']:,.0f} 元")
    print(f"• 最大盤中回撤 (Max Drawdown):   NT$ {metrics['max_drawdown_ntd']:,.0f} 元")
    print("=" * 70)

    # 6. 繪製圖表並儲存
    plot_path = os.path.join(base_dir, "tmf_backtest_20261030.png")
    engine.plot_results(metrics, output_path=plot_path)
    print(f"\n🖼️ 回測走勢與損益圖表已生成: {plot_path}")

    # 複製至 artifact 資料夾以便展示
    artifact_dir = "/Users/willhuang/.gemini/antigravity/brain/b8b63ccf-f85f-4e1c-88b5-ed46261f6ae5"
    if os.path.exists(artifact_dir):
        dest_chart = os.path.join(artifact_dir, "tmf_backtest_20261030.png")
        shioaji_chart = shutil.copy(plot_path, dest_chart)
        print(f"📌 圖表已同步至 Artifact 目錄: {dest_chart}")

if __name__ == "__main__":
    main()
