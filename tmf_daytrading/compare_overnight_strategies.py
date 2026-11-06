#!/usr/bin/env python3
"""
compare_overnight_strategies.py - 比較現行策略與夜盤關鍵高低點 (Overnight S/R) 機制之 20 日量化績效
"""
import os
import sys
import copy
import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

from config import ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date
from backtester.metrics import BacktestMetrics

def load_data():
    kbars_path = os.path.join(DATA_DIR, "tmf_20days_1m_kbars.csv")
    levels_path = os.path.join(DATA_DIR, "overnight_levels_summary.csv")
    df_kbars = pd.read_csv(kbars_path)
    df_levels = pd.read_csv(levels_path)
    
    # 建構每日常見夜盤極值查找表: {date: row}
    levels_map = {}
    for _, row in df_levels.iterrows():
        levels_map[row['trading_date']] = row
        
    return df_kbars, levels_map

def run_simulation(df_kbars, levels_map, mode="baseline"):
    """
    mode:
    - 'baseline': 現行策略 A (不參考夜盤)
    - 'dual_breakout': 雙突破共振 (做多需破 max(ORB_H, 夜高); 做空需破 min(ORB_L, 夜低))
    - 'wall_filter': 防撞牆濾網 (若 ORB 突破但前方 40 點內有夜高/夜低阻擋，需突破夜盤極值才進場)
    - 'direction_confluence': 夜盤收盤順向共振 (做多需在夜盤收盤之上，做空需在夜盤收盤之下)
    """
    dates = sorted(df_kbars['date'].unique())
    data_dir = os.path.join(BASE_DIR, "data")
    daily_history = pd.read_csv(os.path.join(data_dir, "daily_bars.csv"))
    
    base_cfg = ORBStrategyConfig()
    all_trades = []
    daily_results = []
    
    for date_str in dates:
        day_kbars = df_kbars[df_kbars['date'] == date_str].copy()
        day_kbars['timestamp'] = pd.to_datetime(day_kbars['timestamp'])
        day_kbars = day_kbars.set_index('timestamp').sort_index()
        
        # 1. 動態 ATR 縮放
        strat_cfg, info = config_for_date(base_cfg, DATA_DIR, date_str)
        
        # 2. 獲取當日夜盤高低點
        night_info = levels_map.get(date_str)
        n_high = float(night_info['night_high']) if night_info is not None else None
        n_low = float(night_info['night_low']) if night_info is not None else None
        n_close = float(night_info['night_close']) if night_info is not None else None
        
        strategy = ORBStrategy(config=strat_cfg)
        df_indicators = strategy.prepare_indicators(day_kbars)
        strategy.calculate_orb_levels(df_indicators)
        
        # 每日變數
        trades_today = []
        prev_bar = None
        
        orb_h = strategy.orb_high
        orb_l = strategy.orb_low
        orb_range = (orb_h - orb_l) if (orb_h and orb_l) else 0.0
        max_orb_range = getattr(strat_cfg, "max_orb_range_pts", 320.0)
        max_chase = getattr(strat_cfg, "max_breakout_chase_pts", 60.0)
        
        # 若振幅過大，直接跳過今日 (風控)
        if orb_range > max_orb_range:
            daily_results.append({
                "date": date_str, "trades": 0, "pts": 0, "pnl": 0, "reason": "ORB振幅過大防守"
            })
            continue
            
        for ts, bar in df_indicators.iterrows():
            t_str = ts.strftime('%H:%M:%S')
            c = float(bar['close'])
            h = float(bar['high'])
            l = float(bar['low'])
            vwap = float(bar.get('vwap', c))
            ema9 = float(bar.get('ema9', c))
            ema21 = float(bar.get('ema21', c))
            
            # --- 出場檢查 (與原策略相同) ---
            # 1. 13:25 收盤強平
            if t_str >= strat_cfg.force_close_time:
                if strategy.position != 0:
                    pnl_pts = (c - strategy.entry_price) if strategy.position == 1 else (strategy.entry_price - c)
                    rec = {
                        "date": date_str, "entry_time": strategy.entry_time, "exit_time": ts,
                        "action": "BUY" if strategy.position == 1 else "SELL",
                        "entry_price": strategy.entry_price, "exit_price": c,
                        "pnl_pts": pnl_pts, "reason": "收盤前安全平倉"
                    }
                    trades_today.append(rec)
                    strategy.position = 0
                break
                
            # 2. 持倉風控出場
            if strategy.position != 0:
                if strategy.position == 1:
                    cur_profit = h - strategy.entry_price
                    if cur_profit > strategy.highest_profit:
                        strategy.highest_profit = cur_profit
                    # 移動停利
                    if strategy.highest_profit >= strat_cfg.trailing_activation_pts and l <= (strategy.entry_price + strategy.highest_profit - strat_cfg.trailing_distance_pts):
                        exit_p = strategy.entry_price + strategy.highest_profit - strat_cfg.trailing_distance_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "BUY", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": exit_p - strategy.entry_price, "reason": "三階移動停利"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 二階鎖利
                    if strategy.highest_profit >= strat_cfg.tier2_trigger_pts and l <= (strategy.entry_price + strat_cfg.tier2_lock_pts):
                        exit_p = strategy.entry_price + strat_cfg.tier2_lock_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "BUY", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": strat_cfg.tier2_lock_pts, "reason": "二階鎖利"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 中階鎖利
                    if strategy.highest_profit >= getattr(strat_cfg, "tier1_5_trigger_pts", 160.0) and l <= (strategy.entry_price + getattr(strat_cfg, "tier1_5_lock_pts", 100.0)):
                        exit_p = strategy.entry_price + getattr(strat_cfg, "tier1_5_lock_pts", 100.0)
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "BUY", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": getattr(strat_cfg, "tier1_5_lock_pts", 100.0), "reason": "中階鎖利"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 一階保本
                    if strategy.highest_profit >= strat_cfg.breakeven_trigger_pts and l <= (strategy.entry_price + strat_cfg.breakeven_lock_pts):
                        exit_p = strategy.entry_price + strat_cfg.breakeven_lock_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "BUY", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": strat_cfg.breakeven_lock_pts, "reason": "一階保本"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 停損
                    if l <= (strategy.entry_price - strat_cfg.stop_loss_pts):
                        exit_p = strategy.entry_price - strat_cfg.stop_loss_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "BUY", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": -strat_cfg.stop_loss_pts, "reason": "初始停損"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                elif strategy.position == -1:
                    cur_profit = strategy.entry_price - l
                    if cur_profit > strategy.highest_profit:
                        strategy.highest_profit = cur_profit
                    # 移動停利
                    if strategy.highest_profit >= strat_cfg.trailing_activation_pts and h >= (strategy.entry_price - strategy.highest_profit + strat_cfg.trailing_distance_pts):
                        exit_p = strategy.entry_price - strategy.highest_profit + strat_cfg.trailing_distance_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "SELL", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": strategy.entry_price - exit_p, "reason": "三階移動停利"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 二階鎖利
                    if strategy.highest_profit >= strat_cfg.tier2_trigger_pts and h >= (strategy.entry_price - strat_cfg.tier2_lock_pts):
                        exit_p = strategy.entry_price - strat_cfg.tier2_lock_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "SELL", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": strat_cfg.tier2_lock_pts, "reason": "二階鎖利"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 中階鎖利
                    if strategy.highest_profit >= getattr(strat_cfg, "tier1_5_trigger_pts", 160.0) and h >= (strategy.entry_price - getattr(strat_cfg, "tier1_5_lock_pts", 100.0)):
                        exit_p = strategy.entry_price - getattr(strat_cfg, "tier1_5_lock_pts", 100.0)
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "SELL", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": getattr(strat_cfg, "tier1_5_lock_pts", 100.0), "reason": "中階鎖利"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 一階保本
                    if strategy.highest_profit >= strat_cfg.breakeven_trigger_pts and h >= (strategy.entry_price - strat_cfg.breakeven_lock_pts):
                        exit_p = strategy.entry_price - strat_cfg.breakeven_lock_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "SELL", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": strat_cfg.breakeven_lock_pts, "reason": "一階保本"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue
                    # 停損
                    if h >= (strategy.entry_price + strat_cfg.stop_loss_pts):
                        exit_p = strategy.entry_price + strat_cfg.stop_loss_pts
                        rec = {"date": date_str, "entry_time": strategy.entry_time, "exit_time": ts, "action": "SELL", "entry_price": strategy.entry_price, "exit_price": exit_p, "pnl_pts": -strat_cfg.stop_loss_pts, "reason": "初始停損"}
                        trades_today.append(rec)
                        strategy.position = 0
                        continue

            # --- 進場檢查 ---
            # 3A. 09:01 ~ 09:15 早盤強勢爆發
            if (strategy.position == 0 and strategy.pre_orb_high is not None and 
                strategy.trade_count < strat_cfg.max_initial_trades and
                "09:01:00" <= t_str <= strat_cfg.orb_end_time):
                
                sb = strat_cfg.early_super_breakout_pts
                long_filter = (c > vwap and ema9 > ema21)
                short_filter = (c < vwap and ema9 < ema21)
                
                if c >= (strategy.pre_orb_high + sb) and long_filter:
                    strategy.position = 1
                    strategy.entry_price = c
                    strategy.entry_time = ts
                    strategy.trade_count += 1
                    strategy.highest_profit = 0.0
                elif c <= (strategy.pre_orb_low - sb) and short_filter:
                    strategy.position = -1
                    strategy.entry_price = c
                    strategy.entry_time = ts
                    strategy.trade_count += 1
                    strategy.highest_profit = 0.0

            # 3B. 09:16 ~ 11:30 標準 ORB 突破進場
            if (strategy.position == 0 and strategy.orb_calculated and
                strategy.trade_count < strat_cfg.max_initial_trades and
                strat_cfg.entry_start_time <= t_str <= strat_cfg.entry_end_time):
                
                long_filter = (c > vwap and ema9 > ema21)
                short_filter = (c < vwap and ema9 < ema21)
                
                # 計算進場門檻
                buy_trigger = orb_h
                sell_trigger = orb_l
                
                can_buy = (orb_h < c <= (orb_h + max_chase)) and long_filter
                can_sell = ((orb_l - max_chase) <= c < orb_l) and short_filter
                
                # 依 mode 調整夜盤機制
                if mode == "dual_breakout" and n_high is not None and n_low is not None:
                    # 做多必須同時突破 ORB 上界與夜高；做空必須同時跌破 ORB 下界與夜低
                    dual_buy_lvl = max(orb_h, n_high)
                    dual_sell_lvl = min(orb_l, n_low)
                    can_buy = (dual_buy_lvl < c <= (dual_buy_lvl + max_chase)) and long_filter
                    can_sell = ((dual_sell_lvl - max_chase) <= c < dual_sell_lvl) and short_filter
                    
                elif mode == "wall_filter" and n_high is not None and n_low is not None:
                    # 若 orb_h < n_high 且兩者距離小於 40 點 (直接撞牆)，則必須站上 n_high 才做多
                    if orb_h < n_high and (n_high - orb_h) <= 40:
                        can_buy = (n_high < c <= (n_high + max_chase)) and long_filter
                    # 若 orb_l > n_low 且兩者距離小於 40 點 (直接撞地)，則必須跌破 n_low 才做空
                    if orb_l > n_low and (orb_l - n_low) <= 40:
                        can_sell = ((n_low - max_chase) <= c < n_low) and short_filter

                elif mode == "direction_confluence" and n_close is not None:
                    # 順勢共振: 做多時需高於夜盤收盤價，做空時需低於夜盤收盤價
                    can_buy = can_buy and (c > n_close)
                    can_sell = can_sell and (c < n_close)
                    
                if can_buy:
                    strategy.position = 1
                    strategy.entry_price = c
                    strategy.entry_time = ts
                    strategy.trade_count += 1
                    strategy.highest_profit = 0.0
                elif can_sell:
                    strategy.position = -1
                    strategy.entry_price = c
                    strategy.entry_time = ts
                    strategy.trade_count += 1
                    strategy.highest_profit = 0.0
                    
            prev_bar = bar
            
        all_trades.extend(trades_today)
        day_pts = sum(t['pnl_pts'] for t in trades_today)
        # 扣除手續費與稅 (單口約 27 元每趟，一進一出約 54 元)
        day_pnl = sum(t['pnl_pts'] * 10 - 54 for t in trades_today) if trades_today else 0
        daily_results.append({
            "date": date_str, "trades": len(trades_today), "pts": day_pts, "pnl": day_pnl
        })
        
    metrics = BacktestMetrics.calculate(all_trades)
    return metrics, all_trades, daily_results

def main():
    df_kbars, levels_map = load_data()
    print("=" * 90)
    print("🔬 TMF 微型臺指期 20 日量化回測對比：現行策略 A vs 夜盤關鍵高低點 (Overnight S/R) 機制")
    print("=" * 90)
    
    modes = [
        ("baseline", "現行策略 A (基準：純日盤 ORB + ATR 動態縮放)"),
        ("dual_breakout", "機制一：雙突破共振 (破 max(ORB_H, 夜高) 多 / 破 min(ORB_L, 夜低) 空)"),
        ("wall_filter", "機制二：防撞牆保護 (前方 40 點有夜高/夜低阻擋時，需清空障礙才進場)"),
        ("direction_confluence", "機制三：夜盤收盤順勢共振 (做多需 > 夜盤收盤 / 做空需 < 夜盤收盤)")
    ]
    
    results = {}
    for mode_key, mode_desc in modes:
        metrics, trades, daily = run_simulation(df_kbars, levels_map, mode=mode_key)
        results[mode_key] = {
            "desc": mode_desc,
            "metrics": metrics,
            "trades": trades,
            "daily": daily
        }
        
    print(f"\n{'策略模型':<36} | {'總淨損益 (NTD)':<14} | {'勝率':<8} | {'獲利因子':<8} | {'最大拉回':<12} | {'交易筆數':<8}")
    print("-" * 95)
    for mode_key, mode_desc in modes:
        m = results[mode_key]["metrics"]
        pnl_str = f"NT$ {m['net_profit_ntd']:+,.0f}"
        wr_str = f"{m['win_rate']:.1f}%"
        pf_str = f"{m['profit_factor']:.2f}"
        mdd_str = f"NT$ {m['max_drawdown_ntd']:,.0f}"
        tr_str = f"{m['total_trades']} 筆"
        print(f"{mode_desc:<35} | {pnl_str:<14} | {wr_str:<8} | {pf_str:<8} | {mdd_str:<12} | {tr_str:<8}")
        
    print("=" * 95)
    
    # 針對表現突出的機制做進一步逐日對比
    print("\n🔍 【各模型關鍵交易日損益差異明細】")
    dates = [d["date"] for d in results["baseline"]["daily"]]
    df_diff = pd.DataFrame({"日期": dates})
    for mode_key, _ in modes:
        df_diff[mode_key] = [d["pnl"] for d in results[mode_key]["daily"]]
        
    print(df_diff.to_string(index=False))

if __name__ == "__main__":
    main()
