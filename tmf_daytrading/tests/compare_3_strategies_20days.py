#!/usr/bin/env python3
"""
比較三種策略在最新 20 個交易日 (2026-10-06 ~ 2026-11-05) 的收益表現:
1. 目前策略 (Baseline, 1口, 每日1次)
2. Item 1 策略 (多口階梯分批停利, 3口)
3. Item 2 策略 (一階保本後順勢二次上車, 1口)
"""
import os
import sys
import copy
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import InstrumentConfig, ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date
from backtester.metrics import BacktestMetrics

def calc_cost(points, entry_p, exit_p, qty=1, inst_cfg=None):
    if inst_cfg is None:
        inst_cfg = InstrumentConfig(symbol="TMF", multiplier=10.0, commission_per_order=12.0, tax_rate=0.00001)
    gross_ntd = points * inst_cfg.multiplier * qty
    comm_ntd = inst_cfg.commission_per_order * 2.0 * qty
    tax_ntd = (entry_p + exit_p) * inst_cfg.multiplier * inst_cfg.tax_rate * qty
    net_ntd = gross_ntd - comm_ntd - tax_ntd
    return gross_ntd, comm_ntd + tax_ntd, net_ntd

def load_all_20days():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_path = os.path.join(base_dir, "data", "tmf_20days_1m_kbars.csv")
    df = pd.read_csv(cache_path)
    dates = sorted(df['date'].unique())[-20:]
    return base_dir, df, dates

def simulate_baseline_and_items():
    base_dir, all_df, target_dates = load_all_20days()
    inst_cfg = InstrumentConfig(symbol="TMF", multiplier=10.0, commission_per_order=12.0, tax_rate=0.00001, slippage_points=1.0)
    base_cfg = ORBStrategyConfig()
    data_dir = os.path.join(base_dir, "data")

    results_baseline = []
    results_item1 = []
    results_item2 = []

    daily_detail = []

    for d in target_dates:
        sub = all_df[all_df['date'] == d].copy()
        if len(sub) == 0:
            continue
        sub['ts'] = pd.to_datetime(sub['timestamp'])
        sub = sub.set_index('ts').sort_index()

        eff_cfg, atr_info = config_for_date(base_cfg, data_dir, d)

        # -------------------------------------------------------------
        # 1. 執行基準策略 (Baseline, 1口)
        # -------------------------------------------------------------
        strat_base = ORBStrategy(config=eff_cfg, inst_config=inst_cfg)
        kbars_base = strat_base.prepare_indicators(sub[['open', 'high', 'low', 'close', 'volume']].copy())
        strat_base.calculate_orb_levels(kbars_base)

        prev_b = None
        for ts, b in kbars_base.iterrows():
            strat_base.execute_bar(ts, b, prev_b)
            prev_b = b

        base_trades = copy.deepcopy(strat_base.trades)

        # -------------------------------------------------------------
        # 2. 執行 Item 1 (多口階梯分批停利, 3口)
        # 規則:
        # 進場 3 口。
        # 口數 1: 一階保本鎖利 (+30~+35 點平倉 1 口)
        # 口數 2: 中階/二階鎖利 (+75~+120 點平倉 1 口)
        # 口數 3: 奔馳主力單 (移動停利 +400點回撤70點，或 13:25 收盤平倉)
        # 停損保護:
        # 若未達一階保本直接打到停損，3 口皆以初始停損出場 (-90~-105點)。
        # 一旦口數 1 達到一階鎖利出場，剩餘口數 (口2, 口3) 之停損自動拉至成本價 (Entry Price，0 風險)。
        # 若後續拉回跌破/漲破成本價，剩餘口數以成本價 (0 點) 保本出場。
        # -------------------------------------------------------------
        strat_i1 = ORBStrategy(config=eff_cfg, inst_config=inst_cfg)
        kbars_i1 = strat_i1.prepare_indicators(sub[['open', 'high', 'low', 'close', 'volume']].copy())
        strat_i1.calculate_orb_levels(kbars_i1)

        # 自行模擬 3 口分批狀態
        item1_trades = []
        pos = 0 # 1 or -1
        entry_price = 0.0
        entry_time = None
        highest_p = 0.0
        remaining_qty = 0
        tier1_done = False
        tier2_done = False
        trade_count = 0

        prev_b = None
        for ts, b in kbars_i1.iterrows():
            t_str = ts.strftime('%H:%M:%S')
            c, h, l = float(b['close']), float(b['high']), float(b['low'])
            vwap = float(b.get('vwap', c))
            ema9 = float(b.get('ema9', c))
            ema21 = float(b.get('ema21', c))

            # 檢查進場 (沿用策略本身的進場邏輯)
            if pos == 0 and trade_count < eff_cfg.max_initial_trades and t_str < "11:30:00":
                # 早盤跳水或標準 ORB 突破
                orb_range = (strat_i1.orb_high - strat_i1.orb_low) if strat_i1.orb_calculated else 0.0
                max_orb = getattr(eff_cfg, "max_orb_range_pts", 320.0)
                max_chase = getattr(eff_cfg, "max_breakout_chase_pts", 60.0)
                sb = getattr(eff_cfg, "early_super_breakout_pts", 100.0)

                # 早盤 09:01~09:15
                if "09:01:00" <= t_str <= eff_cfg.orb_end_time and strat_i1.pre_orb_high is not None:
                    if c >= (strat_i1.pre_orb_high + sb) and (c > vwap and ema9 > ema21):
                        pos = 1; entry_price = c; entry_time = ts; trade_count += 1
                        remaining_qty = 3; highest_p = 0.0; tier1_done = False; tier2_done = False
                    elif c <= (strat_i1.pre_orb_low - sb) and (c < vwap and ema9 < ema21):
                        pos = -1; entry_price = c; entry_time = ts; trade_count += 1
                        remaining_qty = 3; highest_p = 0.0; tier1_done = False; tier2_done = False

                # 09:16~11:30 標準 ORB
                elif strat_i1.orb_calculated and orb_range <= max_orb and "09:16:00" <= t_str <= "11:30:00":
                    if strat_i1.orb_high < c <= (strat_i1.orb_high + max_chase) and (c > vwap and ema9 > ema21):
                        pos = 1; entry_price = c; entry_time = ts; trade_count += 1
                        remaining_qty = 3; highest_p = 0.0; tier1_done = False; tier2_done = False
                    elif (strat_i1.orb_low - max_chase) <= c < strat_i1.orb_low and (c < vwap and ema9 < ema21):
                        pos = -1; entry_price = c; entry_time = ts; trade_count += 1
                        remaining_qty = 3; highest_p = 0.0; tier1_done = False; tier2_done = False

            elif pos != 0:
                # 更新最高浮盈
                cur_p = (h - entry_price) if pos == 1 else (entry_price - l)
                if cur_p > highest_p:
                    highest_p = cur_p

                # 檢查 13:25 收盤平倉
                if t_str >= eff_cfg.force_close_time:
                    pnl = (c - entry_price) if pos == 1 else (entry_price - c)
                    item1_trades.append({
                        "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                        "entry_price": entry_price, "exit_price": c, "pnl_pts": pnl, "qty": remaining_qty,
                        "reason": f"13:25 收盤強平剩餘{remaining_qty}口"
                    })
                    pos = 0; remaining_qty = 0
                    continue

                # 檢查未達保本前的初始停損
                if not tier1_done:
                    hit_stop = (l <= entry_price - eff_cfg.stop_loss_pts) if pos == 1 else (h >= entry_price + eff_cfg.stop_loss_pts)
                    if hit_stop:
                        exit_p = (entry_price - eff_cfg.stop_loss_pts) if pos == 1 else (entry_price + eff_cfg.stop_loss_pts)
                        item1_trades.append({
                            "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                            "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": -eff_cfg.stop_loss_pts, "qty": 3,
                            "reason": f"初始停損 3口 (-{eff_cfg.stop_loss_pts:.0f}點)"
                        })
                        # SAR 判斷 (若符合反手)
                        pos = 0; remaining_qty = 0
                        continue

                # (1) 口數 1: 一階保本鎖利
                t1_trig = eff_cfg.breakeven_trigger_pts
                t1_lock = eff_cfg.breakeven_lock_pts
                if not tier1_done:
                    hit_t1 = (l <= entry_price + t1_lock) if pos == 1 else (h >= entry_price - t1_lock)
                    if highest_p >= t1_trig and hit_t1:
                        exit_p = (entry_price + t1_lock) if pos == 1 else (entry_price - t1_lock)
                        item1_trades.append({
                            "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                            "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": t1_lock, "qty": 1,
                            "reason": f"第1口 一階鎖利 (+{t1_lock:.0f}點)"
                        })
                        remaining_qty -= 1
                        tier1_done = True

                # (2) 若口數 1 已出場，檢查保護：若拉回跌破進場成本價，剩餘口數以成本價 (0點) 保本平倉
                if tier1_done and remaining_qty > 0:
                    hit_cost = (l <= entry_price) if pos == 1 else (h >= entry_price)
                    # 先看是否達到更高目標
                    t2_trig = eff_cfg.tier1_5_trigger_pts
                    t2_lock = eff_cfg.tier1_5_lock_pts
                    
                    # 口數 2: 二階/中階鎖利
                    if not tier2_done:
                        hit_t2 = (l <= entry_price + t2_lock) if pos == 1 else (h >= entry_price - t2_lock)
                        if highest_p >= t2_trig and hit_t2:
                            exit_p = (entry_price + t2_lock) if pos == 1 else (entry_price - t2_lock)
                            item1_trades.append({
                                "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                                "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": t2_lock, "qty": 1,
                                "reason": f"第2口 中階鎖利 (+{t2_lock:.0f}點)"
                            })
                            remaining_qty -= 1
                            tier2_done = True

                    # 口數 3 (主力單): 三階大波段移動停利 (+400點, 回撤70點)
                    if remaining_qty > 0:
                        tr_act = eff_cfg.trailing_activation_pts
                        tr_dist = eff_cfg.trailing_distance_pts
                        hit_tr = (l <= entry_price + highest_p - tr_dist) if pos == 1 else (h >= entry_price - highest_p + tr_dist)
                        if highest_p >= tr_act and hit_tr:
                            exit_p = (entry_price + highest_p - tr_dist) if pos == 1 else (entry_price - highest_p + tr_dist)
                            pnl = (exit_p - entry_price) if pos == 1 else (entry_price - exit_p)
                            item1_trades.append({
                                "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                                "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": pnl, "qty": remaining_qty,
                                "reason": f"第3口 三階移動停利 (極值+{highest_p:.0f}點, 鎖定+{pnl:.0f}點)"
                            })
                            remaining_qty = 0
                            pos = 0
                            continue

                    # 若未達更高目標且被打回成本價 (拉回原點)
                    if remaining_qty > 0 and hit_cost:
                        item1_trades.append({
                            "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                            "entry_price": entry_price, "exit_price": entry_price, "pnl_pts": 0.0, "qty": remaining_qty,
                            "reason": f"剩餘{remaining_qty}口 觸及成本價保本平倉 (+0點)"
                        })
                        remaining_qty = 0
                        pos = 0
                        continue

            prev_b = b

        # -------------------------------------------------------------
        # 3. 執行 Item 2 (順勢二次上車, 1口)
        # 規則:
        # 第一筆交易同基準策略。
        # 若第一筆交易為「一階保本鎖利」出場，記錄該筆交易創下的極值 (多單最高, 空單最低)。
        # 在 11:00:00 前，若後續 1 分 K 收盤價再次向外突破極值 5 點，且 VWAP 與 EMA9/EMA21 同向共振:
        # 啟動第 2 筆進場 (1口)，以標準 ATR 規則管理至收盤。
        # -------------------------------------------------------------
        item2_trades = copy.deepcopy(base_trades)
        if len(base_trades) == 1 and "一階保本鎖利" in base_trades[0].get("reason", ""):
            t1 = base_trades[0]
            t1_exit_time = t1["exit_time"]
            t1_action = t1["action"] # BUY or SELL
            t1_entry_p = t1["entry_price"]
            
            # 計算 Trade 1 期間的極值
            t1_bars = kbars_base.loc[t1["entry_time"]:t1_exit_time]
            if t1_action == "BUY": # 做多
                ext_price = t1_bars['high'].max()
            else: # 做空
                ext_price = t1_bars['low'].min()

            # 從出場後尋找 11:00 前是否再破極值
            after_bars = kbars_base.loc[t1_exit_time:]
            reentry_triggered = False
            re_pos = 0
            re_entry_p = 0.0
            re_entry_time = None
            re_highest_p = 0.0

            for ts, b in after_bars.iloc[1:].iterrows():
                t_str = ts.strftime('%H:%M:%S')
                c, h, l = float(b['close']), float(b['high']), float(b['low'])
                vwap = float(b.get('vwap', c))
                ema9 = float(b.get('ema9', c))
                ema21 = float(b.get('ema21', c))

                if re_pos == 0 and not reentry_triggered and t_str <= "11:00:00":
                    if t1_action == "BUY" and c >= (ext_price + 5.0) and (c > vwap and ema9 > ema21):
                        re_pos = 1; re_entry_p = c; re_entry_time = ts; reentry_triggered = True; re_highest_p = 0.0
                    elif t1_action == "SELL" and c <= (ext_price - 5.0) and (c < vwap and ema9 < ema21):
                        re_pos = -1; re_entry_p = c; re_entry_time = ts; reentry_triggered = True; re_highest_p = 0.0

                elif re_pos != 0:
                    cur_p = (h - re_entry_p) if re_pos == 1 else (re_entry_p - l)
                    if cur_p > re_highest_p:
                        re_highest_p = cur_p

                    # 13:25 收盤平倉
                    if t_str >= eff_cfg.force_close_time:
                        pnl = (c - re_entry_p) if re_pos == 1 else (re_entry_p - c)
                        item2_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": c, "pnl_pts": pnl, "qty": 1,
                            "reason": f"二次上車 13:25 收盤平倉 (+{pnl:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 停損
                    hit_stop = (l <= re_entry_p - eff_cfg.stop_loss_pts) if re_pos == 1 else (h >= re_entry_p + eff_cfg.stop_loss_pts)
                    if hit_stop:
                        exit_p = (re_entry_p - eff_cfg.stop_loss_pts) if re_pos == 1 else (re_entry_p + eff_cfg.stop_loss_pts)
                        item2_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": -eff_cfg.stop_loss_pts, "qty": 1,
                            "reason": f"二次上車 初始停損 (-{eff_cfg.stop_loss_pts:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 移動停利
                    tr_act = eff_cfg.trailing_activation_pts
                    tr_dist = eff_cfg.trailing_distance_pts
                    hit_tr = (l <= re_entry_p + re_highest_p - tr_dist) if re_pos == 1 else (h >= re_entry_p - re_highest_p + tr_dist)
                    if re_highest_p >= tr_act and hit_tr:
                        exit_p = (re_entry_p + re_highest_p - tr_dist) if re_pos == 1 else (re_entry_p - re_highest_p + tr_dist)
                        pnl = (exit_p - re_entry_p) if re_pos == 1 else (re_entry_p - exit_p)
                        item2_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": pnl, "qty": 1,
                            "reason": f"二次上車 三階移動停利 (+{pnl:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 二階鎖利
                    t2_trig = eff_cfg.tier2_trigger_pts
                    t2_lock = eff_cfg.tier2_lock_pts
                    hit_t2 = (l <= re_entry_p + t2_lock) if re_pos == 1 else (h >= re_entry_p - t2_lock)
                    if re_highest_p >= t2_trig and hit_t2:
                        exit_p = (re_entry_p + t2_lock) if re_pos == 1 else (re_entry_p - t2_lock)
                        item2_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": t2_lock, "qty": 1,
                            "reason": f"二次上車 二階鎖利 (+{t2_lock:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 一階鎖利
                    t1_trig = eff_cfg.breakeven_trigger_pts
                    t1_lock = eff_cfg.breakeven_lock_pts
                    hit_t1 = (l <= re_entry_p + t1_lock) if re_pos == 1 else (h >= re_entry_p - t1_lock)
                    if re_highest_p >= t1_trig and hit_t1:
                        exit_p = (re_entry_p + t1_lock) if re_pos == 1 else (re_entry_p - t1_lock)
                        item2_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": t1_lock, "qty": 1,
                            "reason": f"二次上車 一階鎖利 (+{t1_lock:.0f}點)"
                        })
                        re_pos = 0
                        break

        # 計算當日三種策略損益
        def summarize_trades(trades, is_item1=False):
            tot_pts = 0.0
            tot_net = 0.0
            for t in trades:
                q = t.get("qty", 1)
                pts = t["pnl_pts"]
                gross, cost, net = calc_cost(pts, t["entry_price"], t["exit_price"], qty=q, inst_cfg=inst_cfg)
                tot_pts += pts * q
                tot_net += net
            return tot_pts, tot_net

        b_pts, b_net = summarize_trades(base_trades)
        i1_pts, i1_net = summarize_trades(item1_trades, is_item1=True)
        i2_pts, i2_net = summarize_trades(item2_trades)

        results_baseline.append({"date": d, "trades": len(base_trades), "pts": b_pts, "net": b_net})
        results_item1.append({"date": d, "trades": len(item1_trades), "pts": i1_pts, "net": i1_net, "per_contract_net": i1_net / 3.0})
        results_item2.append({"date": d, "trades": len(item2_trades), "pts": i2_pts, "net": i2_net})

        daily_detail.append({
            "date": d,
            "b_pts": b_pts, "b_net": b_net,
            "i1_pts": i1_pts, "i1_net": i1_net,
            "i2_pts": i2_pts, "i2_net": i2_net,
            "i2_extra": len(item2_trades) > len(base_trades)
        })

    return results_baseline, results_item1, results_item2, daily_detail

if __name__ == "__main__":
    b, i1, i2, details = simulate_baseline_and_items()

    df_b = pd.DataFrame(b)
    df_i1 = pd.DataFrame(i1)
    df_i2 = pd.DataFrame(i2)

    print("=" * 90)
    print("📊 最新 20 個交易日 (2026-10-06 ~ 2026-11-05) 三種策略績效對比總表")
    print("=" * 90)
    print(f"{'指標名稱':<25} | {'目前策略 (1口)':<18} | {'Item 1 (3口階梯分批)':<22} | {'Item 2 (1口二次上車)':<18}")
    print("-" * 90)

    # 總淨損益
    b_tot_net = df_b['net'].sum()
    i1_tot_net = df_i1['net'].sum()
    i2_tot_net = df_i2['net'].sum()
    print(f"{'扣除成本總淨損益 (NTD)':<23} | NT$ {b_tot_net:>10,.0f} 元 | NT$ {i1_tot_net:>14,.0f} 元 | NT$ {i2_tot_net:>10,.0f} 元")

    # 單口等權效益
    print(f"{'平均單口淨損益貢獻 (NTD)':<23} | NT$ {b_tot_net:>10,.0f} 元 | NT$ {(i1_tot_net/3.0):>14,.0f} 元 | NT$ {i2_tot_net:>10,.0f} 元")

    # 累積獲利點數
    b_tot_pts = df_b['pts'].sum()
    i1_tot_pts = df_i1['pts'].sum()
    i2_tot_pts = df_i2['pts'].sum()
    print(f"{'累積總獲利點數':<25} | {b_tot_pts:>12.0f} 點 | {i1_tot_pts:>16.0f} 點 | {i2_tot_pts:>12.0f} 點")

    # 獲利天數 / 虧損天數
    b_win_days = (df_b['net'] > 0).sum()
    b_loss_days = (df_b['net'] < 0).sum()
    i1_win_days = (df_i1['net'] > 0).sum()
    i1_loss_days = (df_i1['net'] < 0).sum()
    i2_win_days = (df_i2['net'] > 0).sum()
    i2_loss_days = (df_i2['net'] < 0).sum()
    print(f"{'獲利天數 / 虧損天數':<24} | {b_win_days}勝 / {b_loss_days}負        | {i1_win_days}勝 / {i1_loss_days}負          | {i2_win_days}勝 / {i2_loss_days}負")

    # MDD (最大區間回撤)
    def calc_mdd(series):
        cum = series.cumsum()
        peak = cum.cummax()
        dd = peak - cum
        return dd.max()

    b_mdd = calc_mdd(df_b['net'])
    i1_mdd = calc_mdd(df_i1['net'])
    i2_mdd = calc_mdd(df_i2['net'])
    print(f"{'最大回撤 (MDD NTD)':<24} | NT$ {b_mdd:>10,.0f} 元 | NT$ {i1_mdd:>14,.0f} 元 | NT$ {i2_mdd:>10,.0f} 元")

    # Profit Factor
    def calc_pf(series):
        pos = series[series > 0].sum()
        neg = abs(series[series < 0].sum())
        return (pos / neg) if neg > 0 else 99.9

    b_pf = calc_pf(df_b['net'])
    i1_pf = calc_pf(df_i1['net'])
    i2_pf = calc_pf(df_i2['net'])
    print(f"{'獲利因子 (Profit Factor)':<24} | {b_pf:>14.2f}   | {i1_pf:>18.2f}   | {i2_pf:>14.2f}")
    print("=" * 90)

    print("\n📅 【各交易日損益詳細對照表 (NTD)】")
    print(f"{'日期':<12} | {'目前策略(1口)':<14} | {'Item 1(3口分批)':<16} | {'Item 2(1口二次)':<16} | {'Item2觸發二次上車?'}")
    print("-" * 80)
    for row in details:
        star = "⚡ 是 (觸發順勢再進場)" if row["i2_extra"] else "否"
        print(f"{row['date']:<12} | NT$ {row['b_net']:>8,.0f} 元 | NT$ {row['i1_net']:>10,.0f} 元 | NT$ {row['i2_net']:>10,.0f} 元 | {star}")
