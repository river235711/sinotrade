import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import copy

from compare_3_strategies_20days import load_all_20days, calc_cost
from config import InstrumentConfig, ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date

base_dir, all_df, target_dates = load_all_20days()
inst_cfg = InstrumentConfig(symbol="TMF", multiplier=10.0, commission_per_order=12.0, tax_rate=0.00001, slippage_points=1.0)
base_cfg = ORBStrategyConfig()
data_dir = os.path.join(base_dir, "data")

results_a = []
results_item3 = []
details = []

for d in target_dates:
    sub = all_df[all_df['date'] == d].copy()
    if len(sub) == 0:
        continue
    sub['ts'] = pd.to_datetime(sub['timestamp'])
    sub = sub.set_index('ts').sort_index()

    eff_cfg, atr_info = config_for_date(base_cfg, data_dir, d)

    # 1. 執行 Strategy A
    strat_a = ORBStrategy(config=eff_cfg, inst_config=inst_cfg)
    kbars_a = strat_a.prepare_indicators(sub[['open', 'high', 'low', 'close', 'volume']].copy())
    strat_a.calculate_orb_levels(kbars_a)

    prev_b = None
    for ts, b in kbars_a.iterrows():
        strat_a.execute_bar(ts, b, prev_b)
        prev_b = b

    base_trades = copy.deepcopy(strat_a.trades)

    # 2. 執行 Item 3: 準保本區「停損減半保護 (Half-Risk Protection)」
    # 規則:
    # 浮盈門檻: 當最高浮盈曾達 half_risk_trigger_pts (約一階觸發門檻之 75%，如 +60~+70 點，此處設 65 點依 ATR 等比縮放)
    # 若行情拉回反轉，將原本停損線 (-90~-105點) 縮窄減半至 -45~-52 點。
    # 其餘一階、中階、二階、移動停利與收盤平倉維持完全一致。
    ratio = atr_info.get("ratio", 1.0)
    half_risk_trigger = 65.0 * ratio
    half_risk_stop = eff_cfg.stop_loss_pts * 0.5

    strat_i3 = ORBStrategy(config=eff_cfg, inst_config=inst_cfg)
    kbars_i3 = strat_i3.prepare_indicators(sub[['open', 'high', 'low', 'close', 'volume']].copy())
    strat_i3.calculate_orb_levels(kbars_i3)

    item3_trades = []
    prev_b = None
    pos = 0
    entry_price = 0.0
    entry_time = None
    highest_profit = 0.0
    half_risk_activated = False

    for ts, b in kbars_i3.iterrows():
        t_str = ts.strftime('%H:%M:%S')
        c, h, l = float(b['close']), float(b['high']), float(b['low'])
        vwap = float(b.get('vwap', c))
        ema9 = float(b.get('ema9', c))
        ema21 = float(b.get('ema21', c))

        # 進場判定 (與 Strategy A 完全相同)
        if pos == 0 and strat_i3.trade_count < eff_cfg.max_initial_trades and t_str < "11:30:00":
            orb_range = (strat_i3.orb_high - strat_i3.orb_low) if strat_i3.orb_calculated else 0.0
            max_orb = getattr(eff_cfg, "max_orb_range_pts", 320.0)
            max_chase = getattr(eff_cfg, "max_breakout_chase_pts", 60.0)
            sb = getattr(eff_cfg, "early_super_breakout_pts", 100.0)

            # 09:01~09:15
            if "09:01:00" <= t_str <= eff_cfg.orb_end_time and strat_i3.pre_orb_high is not None:
                if c >= (strat_i3.pre_orb_high + sb) and (c > vwap and ema9 > ema21):
                    pos = 1; entry_price = c; entry_time = ts; strat_i3.trade_count += 1
                    highest_profit = 0.0; half_risk_activated = False
                elif c <= (strat_i3.pre_orb_low - sb) and (c < vwap and ema9 < ema21):
                    pos = -1; entry_price = c; entry_time = ts; strat_i3.trade_count += 1
                    highest_profit = 0.0; half_risk_activated = False

            # 09:16~11:30
            elif strat_i3.orb_calculated and orb_range <= max_orb and "09:16:00" <= t_str <= "11:30:00":
                if strat_i3.orb_high < c <= (strat_i3.orb_high + max_chase) and (c > vwap and ema9 > ema21):
                    pos = 1; entry_price = c; entry_time = ts; strat_i3.trade_count += 1
                    highest_profit = 0.0; half_risk_activated = False
                elif (strat_i3.orb_low - max_chase) <= c < strat_i3.orb_low and (c < vwap and ema9 < ema21):
                    pos = -1; entry_price = c; entry_time = ts; strat_i3.trade_count += 1
                    highest_profit = 0.0; half_risk_activated = False

        elif pos != 0:
            cur_p = (h - entry_price) if pos == 1 else (entry_price - l)
            if cur_p > highest_profit:
                highest_profit = cur_p

            # 檢查是否啟用「停損減半保護」
            if highest_profit >= half_risk_trigger:
                half_risk_activated = True

            # 1. 13:25 收盤平倉
            if t_str >= eff_cfg.force_close_time:
                pnl = (c - entry_price) if pos == 1 else (entry_price - c)
                item3_trades.append({
                    "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                    "entry_price": entry_price, "exit_price": c, "pnl_pts": pnl, "qty": 1,
                    "reason": f"13:25 收盤平倉"
                })
                pos = 0
                continue

            # 2. 三階移動停利
            tr_act = eff_cfg.trailing_activation_pts
            tr_dist = eff_cfg.trailing_distance_pts
            hit_tr = (l <= entry_price + highest_profit - tr_dist) if pos == 1 else (h >= entry_price - highest_profit + tr_dist)
            if highest_profit >= tr_act and hit_tr:
                exit_p = (entry_price + highest_profit - tr_dist) if pos == 1 else (entry_price - highest_profit + tr_dist)
                pnl = (exit_p - entry_price) if pos == 1 else (entry_price - exit_p)
                item3_trades.append({
                    "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                    "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": pnl, "qty": 1,
                    "reason": f"三階波段移動停利"
                })
                pos = 0
                continue

            # 3. 二階鎖利
            t2_trig = eff_cfg.tier2_trigger_pts
            t2_lock = eff_cfg.tier2_lock_pts
            hit_t2 = (l <= entry_price + t2_lock) if pos == 1 else (h >= entry_price - t2_lock)
            if highest_profit >= t2_trig and hit_t2:
                exit_p = (entry_price + t2_lock) if pos == 1 else (entry_price - t2_lock)
                item3_trades.append({
                    "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                    "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": t2_lock, "qty": 1,
                    "reason": f"二階鎖利 (+{t2_lock:.0f}點)"
                })
                pos = 0
                continue

            # 4. 中階鎖利 (1.5階)
            t15_trig = getattr(eff_cfg, "tier1_5_trigger_pts", 110.0)
            t15_lock = getattr(eff_cfg, "tier1_5_lock_pts", 75.0)
            hit_t15 = (l <= entry_price + t15_lock) if pos == 1 else (h >= entry_price - t15_lock)
            if highest_profit >= t15_trig and hit_t15:
                exit_p = (entry_price + t15_lock) if pos == 1 else (entry_price - t15_lock)
                item3_trades.append({
                    "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                    "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": t15_lock, "qty": 1,
                    "reason": f"中階鎖利 (+{t15_lock:.0f}點)"
                })
                pos = 0
                continue

            # 5. 一階保本鎖利
            t1_trig = eff_cfg.breakeven_trigger_pts
            t1_lock = eff_cfg.breakeven_lock_pts
            hit_t1 = (l <= entry_price + t1_lock) if pos == 1 else (h >= entry_price - t1_lock)
            if highest_profit >= t1_trig and hit_t1:
                exit_p = (entry_price + t1_lock) if pos == 1 else (entry_price - t1_lock)
                item3_trades.append({
                    "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                    "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": t1_lock, "qty": 1,
                    "reason": f"一階保本鎖利 (+{t1_lock:.0f}點)"
                })
                pos = 0
                continue

            # 6. 停損判定：若觸發減半保護，用 half_risk_stop；否則用 eff_cfg.stop_loss_pts
            eff_stop = half_risk_stop if half_risk_activated else eff_cfg.stop_loss_pts
            hit_stop = (l <= entry_price - eff_stop) if pos == 1 else (h >= entry_price + eff_stop)
            if hit_stop:
                exit_p = (entry_price - eff_stop) if pos == 1 else (entry_price + eff_stop)
                reason_str = f"準保本停損減半 (-{eff_stop:.0f}點)" if half_risk_activated else f"初始停損 (-{eff_stop:.0f}點)"
                item3_trades.append({
                    "entry_time": entry_time, "exit_time": ts, "action": "BUY" if pos == 1 else "SELL",
                    "entry_price": entry_price, "exit_price": exit_p, "pnl_pts": -eff_stop, "qty": 1,
                    "reason": reason_str
                })
                pos = 0
                continue

        prev_b = b

    def summarize(trades):
        pts = 0.0
        net = 0.0
        for t in trades:
            p = t["pnl_pts"]
            g, c, n = calc_cost(p, t["entry_price"], t["exit_price"], qty=t.get("qty", 1), inst_cfg=inst_cfg)
            pts += p
            net += n
        return pts, net

    a_pts, a_net = summarize(base_trades)
    i3_pts, i3_net = summarize(item3_trades)

    results_a.append({"date": d, "pts": a_pts, "net": a_net})
    results_item3.append({"date": d, "pts": i3_pts, "net": i3_net})

    diff = i3_net - a_net
    details.append({"date": d, "a_net": a_net, "i3_net": i3_net, "diff": diff, "trades_a": base_trades, "trades_i3": item3_trades})

df_a = pd.DataFrame(results_a)
df_i3 = pd.DataFrame(results_item3)

print("="*85)
print("📊 最新 20 個交易日: 策略 A vs Item 3 (準保本停損減半保護) 績效對照表")
print("="*85)
print(f"指標名稱                      | 策略 A (目前策略，1口)    | Item 3 (停損減半保護，1口)")
print("-"*85)
a_net_tot = df_a['net'].sum()
i3_net_tot = df_i3['net'].sum()
print(f"扣除成本總淨損益 (NTD)          | NT$ {a_net_tot:>10,.0f} 元      | NT$ {i3_net_tot:>10,.0f} 元")

a_pts_tot = df_a['pts'].sum()
i3_pts_tot = df_i3['pts'].sum()
print(f"累積總獲利點數                   | {a_pts_tot:>12.0f} 點      | {i3_pts_tot:>12.0f} 點")

# MDD
cum_a = df_a['net'].cumsum()
mdd_a = (cum_a.cummax() - cum_a).max()
cum_i3 = df_i3['net'].cumsum()
mdd_i3 = (cum_i3.cummax() - cum_i3).max()
print(f"最大區間回撤 (MDD)              | NT$ {mdd_a:>10,.0f} 元      | NT$ {mdd_i3:>10,.0f} 元")

# PF
def get_pf(series):
    pos = series[series > 0].sum()
    neg = abs(series[series < 0].sum())
    return pos / neg if neg > 0 else 99.9

print(f"獲利因子 (Profit Factor)        | {get_pf(df_a['net']):>14.2f}        | {get_pf(df_i3['net']):>14.2f}")
print(f"獲利天數 / 虧損天數             | {(df_a['net']>0).sum()}勝 / {(df_a['net']<0).sum()}負              | {(df_i3['net']>0).sum()}勝 / {(df_i3['net']<0).sum()}負")
print("="*85)

print("\n📅 【Item 3 差異影響明細】")
has_diff = False
for r in details:
    if abs(r['diff']) > 1.0:
        has_diff = True
        print(f"• 日期: {r['date']} | 策略A: NT$ {r['a_net']:+,.0f} 元 -> Item 3: NT$ {r['i3_net']:+,.0f} 元 (差異: {r['diff']:+,.0f}元)")
        print(f"  策略 A 動作: {[t.get('reason') for t in r['trades_a']]}")
        print(f"  Item 3 動作: {[t.get('reason') for t in r['trades_i3']]}")

if not has_diff:
    print("各交易日表現無差異。")
