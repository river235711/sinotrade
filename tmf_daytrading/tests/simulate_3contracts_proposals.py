import os
import sys
import pandas as pd
import numpy as np

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from config import InstrumentConfig, ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date

df_raw = pd.read_csv(os.path.join(base_dir, 'data', 'tmf_20days_1m_kbars.csv'))
dates = sorted(df_raw['date'].unique())
inst_cfg = InstrumentConfig()

def calc_cost(entry_p, exit_p, action, qty=1):
    multiplier = inst_cfg.multiplier  # 10
    comm = inst_cfg.commission_per_order * 2 * qty  # 24 * qty
    tax_rate = inst_cfg.tax_rate  # 0.00001
    slip = inst_cfg.slippage_points * 2  # 2 pts per contract
    if action == 'BUY':
        pts = exit_p - entry_p
    else:
        pts = entry_p - exit_p
    gross = (pts - slip) * multiplier * qty
    tax = (entry_p + exit_p) * multiplier * tax_rate * qty
    net = gross - comm - tax
    return pts, net

# -------------------------------------------------------------
# 1. 跑現行 v2.1 基準回測 (單口與三口全進全出)
# -------------------------------------------------------------
base_cfg = ORBStrategyConfig()

trades_1qty = []
for d in dates:
    day_df = df_raw[df_raw['date'] == d].copy()
    day_df['timestamp'] = pd.to_datetime(day_df['timestamp'])
    day_df = day_df.set_index('timestamp').sort_index()
    cfg, info = config_for_date(base_cfg, 'data', d)
    strat = ORBStrategy(config=cfg, inst_config=inst_cfg)
    strat.load_overnight_levels_from_file('data/overnight_levels_30days.csv', d)
    day_df = strat.prepare_indicators(day_df)
    
    prev_bar = None
    bars_list = list(day_df.iterrows())
    for idx, (ts, bar) in enumerate(bars_list):
        t_str = ts.strftime('%H:%M:%S')
        wall_clock_str = (ts + pd.Timedelta(minutes=1)).strftime('%H:%M:%S')
        if wall_clock_str >= cfg.orb_end_time and not strat.orb_calculated:
            slice_df = day_df.loc[:ts].copy()
            if idx + 1 < len(bars_list):
                next_ts, next_bar = bars_list[idx + 1]
                if next_ts.strftime('%H:%M:%S') == cfg.orb_end_time:
                    open_p = float(next_bar['open'])
                    open_row = next_bar.copy()
                    open_row['high'] = open_p
                    open_row['low'] = open_p
                    open_row['close'] = open_p
                    slice_df.loc[next_ts] = open_row
            strat.calculate_orb_levels(slice_df)
        elif t_str == cfg.orb_end_time:
            strat.calculate_orb_levels(day_df.loc[:ts])
            
        strat.execute_bar(ts, bar, prev_bar)
        prev_bar = bar
        
    for t in strat.trades:
        t_copy = dict(t)
        t_copy['date'] = d
        pts, net = calc_cost(t['entry_price'], t['exit_price'], t['action'], qty=1)
        t_copy['net_1qty'] = net
        pts3, net3 = calc_cost(t['entry_price'], t['exit_price'], t['action'], qty=3)
        t_copy['net_3qty'] = net3
        trades_1qty.append(t_copy)

print(f"現行 v2.1 交易筆數: {len(trades_1qty)} 筆")
net_1_total = sum(t['net_1qty'] for t in trades_1qty)
net_3_total = sum(t['net_3qty'] for t in trades_1qty)
pts_total = sum(t['pnl_pts'] for t in trades_1qty)
print(f"現行 v2.1 (1口全進全出): +{pts_total:.0f} 點 | NT$ {net_1_total:+,.0f}")
print(f"現行 v2.1 (3口全進全出): NT$ {net_3_total:+,.0f}")
