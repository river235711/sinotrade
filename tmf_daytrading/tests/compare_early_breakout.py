import os
import sys
import pandas as pd
import numpy as np
import dataclasses

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from config import InstrumentConfig, ORBStrategyConfig
from backtester.engine import BacktestEngine
from strategies.orb_strategy import ORBStrategy

def run_backtest_with_flag(enable_early: bool):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_path = os.path.join(base_dir, "data", "tmf_20days_1m_kbars.csv")
    cached_df = pd.read_csv(cache_path)
    all_dates = sorted(cached_df['date'].unique())
    target_dates = all_dates[-20:]

    inst_cfg = InstrumentConfig(
        symbol="TMF",
        multiplier=10.0,
        commission_per_order=12.0,
        tax_rate=0.00001,
        slippage_points=1.0
    )

    all_trades = []
    daily_summaries = []

    for d in target_dates:
        file_date = d.replace('-', '_')
        target_date = d.replace('-', '')
        csv_path = os.path.join(base_dir, "data", f"Daily_{file_date}.csv")
        engine = BacktestEngine(data_path=csv_path, inst_config=inst_cfg)
        kbars = engine.load_taifex_csv(target_date=target_date, contract_month="AUTO")
        cfg = ORBStrategyConfig(enable_early_super_breakout=enable_early)
        metrics = engine.run_orb_strategy(cfg)

        tdf = metrics["trades_df"]
        day_pts = metrics["total_points"] if not tdf.empty else 0.0
        day_ntd = metrics["net_profit_ntd"] if not tdf.empty else 0.0

        daily_summaries.append({
            "date": d,
            "trades": metrics["total_trades"],
            "points_pnl": day_pts,
            "net_ntd": day_ntd
        })

        if not tdf.empty:
            for _, row in tdf.iterrows():
                r_dict = row.to_dict()
                r_dict["date"] = d
                all_trades.append(r_dict)

    return pd.DataFrame(daily_summaries), pd.DataFrame(all_trades)

if __name__ == "__main__":
    print("Running Model A (Early Breakout Enabled)...")
    daily_a, trades_a = run_backtest_with_flag(enable_early=True)

    print("Running Model B (Early Breakout Disabled)...")
    daily_b, trades_b = run_backtest_with_flag(enable_early=False)

    print("\n" + "="*80)
    print("🔍 【20日每日對比：Model A (啟用 Early Breakout) vs Model B (關閉 Early Breakout)】")
    print("="*80)
    print(f"{'日期':<12} | {'Model A (現行啟用)':<25} | {'Model B (建議關閉)':<25} | {'差異 (B - A)':<12}")
    print("-" * 80)
    
    merged = pd.merge(daily_a, daily_b, on="date", suffixes=('_A', '_B'))
    for _, row in merged.iterrows():
        diff_ntd = row['net_ntd_B'] - row['net_ntd_A']
        diff_str = f"{diff_ntd:+,.0f} 元" if diff_ntd != 0 else "--"
        str_a = f"{row['trades_A']}筆 | {row['points_pnl_A']:+4.0f}點 | {row['net_ntd_A']:+6,.0f}元"
        str_b = f"{row['trades_B']}筆 | {row['points_pnl_B']:+4.0f}點 | {row['net_ntd_B']:+6,.0f}元"
        marker = " ⭐️" if diff_ntd > 0 else (" 🔻" if diff_ntd < 0 else "")
        print(f"{row['date']:<12} | {str_a:<25} | {str_b:<25} | {diff_str:<12}{marker}")

    print("="*80)
    
    # Summary stats
    tot_pts_a = daily_a["points_pnl"].sum()
    tot_ntd_a = daily_a["net_ntd"].sum()
    tot_trades_a = len(trades_a)
    wins_a = (trades_a["net_ntd"] > 0).sum() if not trades_a.empty else 0
    wr_a = (wins_a / tot_trades_a * 100) if tot_trades_a > 0 else 0
    gross_win_a = trades_a[trades_a["net_ntd"] > 0]["net_ntd"].sum() if not trades_a.empty else 0
    gross_loss_a = abs(trades_a[trades_a["net_ntd"] <= 0]["net_ntd"].sum()) if not trades_a.empty else 1
    pf_a = gross_win_a / gross_loss_a if gross_loss_a > 0 else 999

    tot_pts_b = daily_b["points_pnl"].sum()
    tot_ntd_b = daily_b["net_ntd"].sum()
    tot_trades_b = len(trades_b)
    wins_b = (trades_b["net_ntd"] > 0).sum() if not trades_b.empty else 0
    wr_b = (wins_b / tot_trades_b * 100) if tot_trades_b > 0 else 0
    gross_win_b = trades_b[trades_b["net_ntd"] > 0]["net_ntd"].sum() if not trades_b.empty else 0
    gross_loss_b = abs(trades_b[trades_b["net_ntd"] <= 0]["net_ntd"].sum()) if not trades_b.empty else 1
    pf_b = gross_win_b / gross_loss_b if gross_loss_b > 0 else 999

    print(f"\n📊 【20日總績效對比】")
    print(f"指標                       Model A (現行啟用)       Model B (建議關閉)       改善幅度")
    print(f"--------------------------------------------------------------------------------")
    print(f"總交易筆數                 {tot_trades_a:>8} 筆             {tot_trades_b:>8} 筆")
    print(f"獲利勝筆數                 {wins_a:>8} 筆             {wins_b:>8} 筆")
    print(f"勝率 (Win Rate)            {wr_a:>8.1f} %              {wr_b:>8.1f} %          {wr_b - wr_a:+5.1f}%")
    print(f"總獲利點數                 {tot_pts_a:>8.0f} 點             {tot_pts_b:>8.0f} 點          {tot_pts_b - tot_pts_a:+5.0f} 點")
    print(f"總淨損益 (NTD)             NT${tot_ntd_a:>7,.0f} 元          NT${tot_ntd_b:>7,.0f} 元       NT${tot_ntd_b - tot_ntd_a:+6,.0f} 元")
    print(f"獲利因子 (Profit Factor)   {pf_a:>8.2f}                {pf_b:>8.2f}            {pf_b - pf_a:+5.2f}")
    print("="*80)

    print("\n📋 Model B 逐筆交易明細:")
    for idx, t in enumerate(trades_b.to_dict('records'), 1):
        act_str = "BUY " if t["action"] == "BUY" else "SELL"
        e_t = pd.to_datetime(t["entry_time"]).strftime("%H:%M:%S")
        x_t = pd.to_datetime(t["exit_time"]).strftime("%H:%M:%S")
        print(f"#{idx:02d} | {t['date']} | {act_str} {e_t}@{t['entry_price']:.0f} -> {x_t}@{t['exit_price']:.0f} | {t['pnl_pts']:+.0f}點 ({t['net_ntd']:+,.0f}元) | {t['reason']}")
