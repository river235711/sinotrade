#!/usr/bin/env python3
"""
compare_30days_overnight.py - 現行策略 A (基準模型) vs 機制二：防撞牆保護 (Wall Filter) 前 30 個交易日量化回測對比
"""
import os
import sys
import copy
import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import InstrumentConfig, ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date, load_daily_history
from backtester.metrics import BacktestMetrics

DATA_DIR = os.path.join(BASE_DIR, "data")

def load_data():
    kbars_path = os.path.join(DATA_DIR, "tmf_30days_1m_kbars.csv")
    levels_path = os.path.join(DATA_DIR, "overnight_levels_30days.csv")
    df_kbars = pd.read_csv(kbars_path)
    df_levels = pd.read_csv(levels_path)
    daily_history = load_daily_history(DATA_DIR)
    
    levels_map = {}
    for _, row in df_levels.iterrows():
        levels_map[row['trading_date']] = row
        
    return df_kbars, levels_map, daily_history

def run_simulation(df_kbars, levels_map, daily_history, mode="baseline", wall_threshold=40.0):
    """
    mode:
    - 'baseline': 現行策略 A (基準模型，純日盤 ORB + 20日動態 ATR)
    - 'wall_filter': 機制二：防撞牆保護 (若 ORB 突破但前方 wall_threshold 點內有夜高/夜低阻擋，需突破夜盤極值才進場)
    """
    dates = sorted(df_kbars['date'].unique())
    base_cfg = ORBStrategyConfig()
    inst_cfg = InstrumentConfig()
    
    all_trades = []
    daily_results = []
    
    for date_str in dates:
        day_kbars = df_kbars[df_kbars['date'] == date_str].copy()
        day_kbars['timestamp'] = pd.to_datetime(day_kbars['timestamp'])
        day_kbars = day_kbars.set_index('timestamp').sort_index()
        
        # 1. 20日動態 ATR 縮放
        strat_cfg, info = config_for_date(base_cfg, DATA_DIR, date_str, daily=daily_history)
        
        # 2. 獲取當日夜盤高低點
        night_info = levels_map.get(date_str)
        n_high = float(night_info['night_high']) if night_info is not None else None
        n_low = float(night_info['night_low']) if night_info is not None else None
        n_close = float(night_info['night_close']) if night_info is not None else None
        
        strategy = ORBStrategy(config=strat_cfg)
        df_indicators = strategy.prepare_indicators(day_kbars)
        strategy.calculate_orb_levels(df_indicators)
        
        trades_today = []
        orb_h = strategy.orb_high
        orb_l = strategy.orb_low
        orb_range = (orb_h - orb_l) if (orb_h and orb_l) else 0.0
        max_orb_range = getattr(strat_cfg, "max_orb_range_pts", 320.0)
        max_chase = getattr(strat_cfg, "max_breakout_chase_pts", 60.0)
        
        # 若振幅過大，直接跳過今日 (風控防守)
        if orb_range > max_orb_range:
            daily_results.append({
                "date": date_str, "trades": 0, "pts": 0.0, "net_ntd": 0.0, "reason": f"ORB振幅({orb_range:.0f})>風控上限({max_orb_range:.0f})"
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
            
            # --- 出場檢查 ---
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
            # 3A. 09:01 ~ 09:15 早盤強勢爆發 (非 ORB，純動能)
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
                
                can_buy = (orb_h < c <= (orb_h + max_chase)) and long_filter
                can_sell = ((orb_l - max_chase) <= c < orb_l) and short_filter
                
                # 機制二：防撞牆保護 (Wall Filter)
                if mode == "wall_filter" and n_high is not None and n_low is not None:
                    # 若 orb_h < n_high 且兩者距離 <= wall_threshold (前方直接撞牆)，則必須站上 n_high 才做多
                    if orb_h < n_high and (n_high - orb_h) <= wall_threshold:
                        can_buy = (n_high < c <= (n_high + max_chase)) and long_filter
                    # 若 orb_l > n_low 且兩者距離 <= wall_threshold (前方直接撞地)，則必須跌破 n_low 才做空
                    if orb_l > n_low and (orb_l - n_low) <= wall_threshold:
                        can_sell = ((n_low - max_chase) <= c < n_low) and short_filter

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
                    
        all_trades.extend(trades_today)
        day_pts = sum(t['pnl_pts'] for t in trades_today)
        # 精準計算日淨損益
        if trades_today:
            day_metrics = BacktestMetrics.calculate(trades_today, inst_cfg)
            day_net_ntd = day_metrics['net_profit_ntd']
        else:
            day_net_ntd = 0.0
            
        daily_results.append({
            "date": date_str,
            "trades": len(trades_today),
            "pts": day_pts,
            "net_ntd": day_net_ntd,
            "reason": trades_today[0]['reason'] if trades_today else "無觸發進場"
        })
        
    metrics = BacktestMetrics.calculate(all_trades, inst_cfg)
    return metrics, all_trades, daily_results

def main():
    df_kbars, levels_map, daily_history = load_data()
    dates = sorted(df_kbars['date'].unique())
    
    print("=" * 100)
    print(f"🔬 TMF 微型臺指期 前 30 個交易日量化對比：現行策略 A (基準模型) vs 機制二：防撞牆保護 (Wall Filter)")
    print(f"📅 回測期間: {dates[0]} ~ {dates[-1]} (共 {len(dates)} 個完整交易日)")
    print("=" * 100)
    
    # 執行基準模型與不同門檻的防撞牆保護
    experiments = [
        ("baseline", "現行策略 A (基準模型：純日盤 ORB + 20日動態 ATR)", 0.0),
        ("wall_filter_30", "機制二：防撞牆保護 (門檻 30 點)", 30.0),
        ("wall_filter_40", "機制二：防撞牆保護 (門檻 40 點，推薦預設)", 40.0),
        ("wall_filter_50", "機制二：防撞牆保護 (門檻 50 點)", 50.0),
        ("wall_filter_60", "機制二：防撞牆保護 (門檻 60 點)", 60.0),
    ]
    
    results = {}
    for exp_id, exp_name, thresh in experiments:
        mode = "baseline" if exp_id == "baseline" else "wall_filter"
        metrics, trades, daily = run_simulation(df_kbars, levels_map, daily_history, mode=mode, wall_threshold=thresh)
        results[exp_id] = {
            "name": exp_name,
            "metrics": metrics,
            "trades": trades,
            "daily": daily
        }
        
    # 印出整體績效對比表
    print(f"\n{'策略模型':<38} | {'淨損益 (NTD)':<13} | {'勝率':<8} | {'獲利因子':<8} | {'最大拉回 (MDD)':<14} | {'交易筆數':<8} | {'平均每筆(NTD)'}")
    print("-" * 110)
    for exp_id, exp_name, _ in experiments:
        m = results[exp_id]["metrics"]
        pnl_str = f"NT$ {m['net_profit_ntd']:+,.0f}"
        wr_str = f"{m['win_rate']:.1f}%"
        pf_str = f"{m['profit_factor']:.2f}"
        mdd_str = f"NT$ {m['max_drawdown_ntd']:,.0f}"
        tr_str = f"{m['total_trades']} 筆"
        avg_str = f"NT$ {m['avg_trade_ntd']:+,.0f}"
        print(f"{exp_name:<37} | {pnl_str:<13} | {wr_str:<8} | {pf_str:<8} | {mdd_str:<14} | {tr_str:<8} | {avg_str}")
    print("=" * 110)
    
    # 核心二選一深入逐日對比 (Baseline vs Wall Filter 40pt)
    base_res = results["baseline"]
    wall_res = results["wall_filter_40"]
    
    print("\n📊 【現行策略 A vs 機制二 (40pt) 前 30 個交易日逐日損益明細】")
    print(f"{'交易日期':<12} | {'策略 A 點數':<10} | {'策略 A 淨額':<12} | {'機制二 點數':<10} | {'機制二 淨額':<12} | {'損益差異 (NTD)':<14} | {'差異原因 / 說明'}")
    print("-" * 105)
    
    diff_days = []
    for i, date_str in enumerate(dates):
        b_d = base_res["daily"][i]
        w_d = wall_res["daily"][i]
        diff_ntd = w_d["net_ntd"] - b_d["net_ntd"]
        diff_pts = w_d["pts"] - b_d["pts"]
        
        flag = ""
        if diff_ntd > 0:
            flag = f"🟢 機制二獲利優於策略 A (+NT$ {diff_ntd:,.0f})"
            diff_days.append((date_str, diff_ntd, "WALL_BETTER"))
        elif diff_ntd < 0:
            flag = f"🔴 策略 A 優於機制二 (-NT$ {abs(diff_ntd):,.0f})"
            diff_days.append((date_str, diff_ntd, "BASE_BETTER"))
        else:
            if b_d["trades"] > 0:
                flag = f"⚪ 兩者交易與損益完全相同 ({b_d['reason']})"
            else:
                flag = f"⚪ 兩者今日皆無進場 ({b_d['reason']})"
                
        print(f"{date_str:<12} | {b_d['pts']:>+8.0f} pt | NT$ {b_d['net_ntd']:>+8.0f} | {w_d['pts']:>+8.0f} pt | NT$ {w_d['net_ntd']:>+8.0f} | NT$ {diff_ntd:>+10.0f} | {flag}")
        
    print("-" * 105)
    
    # 輸出差異日詳細交易對比
    if diff_days:
        print(f"\n🔍 【關鍵分歧日 ({len(diff_days)} 天) 深度交易行為解析】")
        for date_str, diff_ntd, cat in diff_days:
            b_trades = [t for t in base_res["trades"] if t["date"] == date_str]
            w_trades = [t for t in wall_res["trades"] if t["date"] == date_str]
            n_row = levels_map.get(date_str)
            print(f"\n📌 日期: {date_str} (淨損益差異: NT$ {diff_ntd:+,.0f})")
            if n_row is not None:
                print(f"   夜盤關鍵位: 夜高 {n_row['night_high']:.0f} | 夜低 {n_row['night_low']:.0f} | 振幅 {n_row['night_range']:.0f} 點")
            print(f"   策略 A (基準):")
            if b_trades:
                for bt in b_trades:
                    print(f"     -> 方向: {bt['action']}, 進場價: {bt['entry_price']:.0f} ({bt['entry_time']}), 出場價: {bt['exit_price']:.0f} ({bt['exit_time']}), 點數: {bt['pnl_pts']:+.0f} pt, 原因: {bt['reason']}")
            else:
                print(f"     -> 無進場")
                
            print(f"   機制二 (防撞牆):")
            if w_trades:
                for wt in w_trades:
                    print(f"     -> 方向: {wt['action']}, 進場價: {wt['entry_price']:.0f} ({wt['entry_time']}), 出場價: {wt['exit_price']:.0f} ({wt['exit_time']}), 點數: {wt['pnl_pts']:+.0f} pt, 原因: {wt['reason']}")
            else:
                print(f"     -> 無進場 (被防撞牆濾網成功過濾)")

if __name__ == "__main__":
    main()
