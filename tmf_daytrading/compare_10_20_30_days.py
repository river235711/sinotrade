#!/usr/bin/env python3
"""
compare_10_20_30_days.py - 比較現行策略 A (基準模型) vs 機制二：防撞牆保護 (40pt) 在近 10 日、20 日、30 日之完整量化表現
"""
import os
import sys
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import InstrumentConfig
from backtester.metrics import BacktestMetrics
from compare_30days_overnight import load_data, run_simulation

def main():
    df_kbars, levels_map, daily_history = load_data()
    all_dates = sorted(df_kbars['date'].unique())
    inst_cfg = InstrumentConfig()
    
    windows = [10, 20, 30]
    
    print("=" * 115)
    print("🔬 TMF 微型臺指期 多週期 (近 10 日 / 20 日 / 30 日) 量化回測總對比：現行策略 A vs 機制二：防撞牆保護 (40點)")
    print("=" * 115)
    
    for w in windows:
        target_dates = all_dates[-w:]
        sub_kbars = df_kbars[df_kbars['date'].isin(target_dates)].copy()
        
        m_base, t_base, d_base = run_simulation(sub_kbars, levels_map, daily_history, mode="baseline", wall_threshold=0.0)
        m_wall, t_wall, d_wall = run_simulation(sub_kbars, levels_map, daily_history, mode="wall_filter", wall_threshold=40.0)
        
        print(f"\n📌 【近 {w} 個交易日回測】 ({target_dates[0]} ~ {target_dates[-1]})")
        print("-" * 115)
        print(f"{'策略模型':<38} | {'總淨損益 (NTD)':<14} | {'累積點數':<10} | {'勝率':<12} | {'獲利因子':<8} | {'最大拉回(MDD)':<13} | {'交易筆數':<8} | {'平均每筆'}")
        print("-" * 115)
        
        for name, m in [("現行策略 A (基準模型)", m_base), ("機制二：防撞牆保護 (40點門檻)", m_wall)]:
            pnl_s = f"NT$ {m['net_profit_ntd']:+,.0f}"
            pts_s = f"{m['total_points']:+.0f} pt"
            wr_s = f"{m['win_rate']:.1f}% ({m['win_count']}勝{m['loss_count']}敗)"
            pf_s = f"{m['profit_factor']:.2f}"
            mdd_s = f"NT$ {m['max_drawdown_ntd']:,.0f}"
            tr_s = f"{m['total_trades']} 筆"
            avg_s = f"NT$ {m['avg_trade_ntd']:+,.0f}"
            print(f"{name:<37} | {pnl_s:<14} | {pts_s:<10} | {wr_s:<12} | {pf_s:<8} | {mdd_s:<13} | {tr_s:<8} | {avg_s}")
            
        diff_pnl = m_wall['net_profit_ntd'] - m_base['net_profit_ntd']
        diff_wr = m_wall['win_rate'] - m_base['win_rate']
        print("-" * 115)
        print(f"💡 機制二 vs 策略 A 差異: 淨損益 {diff_pnl:+,.0f} NTD | 勝率 {diff_wr:+.1f}% | 風控空倉天數: {sum(1 for d in d_base if d['trades']==0)}/{w} 天")

if __name__ == "__main__":
    main()
