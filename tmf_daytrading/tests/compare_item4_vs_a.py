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
results_item4 = []
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

    # 2. 執行 Item 4: 洗盤重新追回 (Stop-Hunt Recovery)
    # 規則:
    # 第一筆同 Strategy A。
    # 若第一筆在 10:00 前被停損出場 (且未觸發 SAR 反手):
    # 監控在 10:30 前，若價格重新向上突破 ORB 上界 + 10 點 (多頭) 或向下跌破 ORB 下界 - 10 點 (空頭)
    # 且滿足 VWAP 與 EMA9 > EMA21 (多頭) / EMA9 < EMA21 (空頭):
    # 啟動第 2 筆洗盤追回進場 (1口)，以標準 ATR 規則管理至收盤。
    item4_trades = copy.deepcopy(base_trades)
    item4_triggered = False

    if len(base_trades) == 1 and "初始停損" in base_trades[0].get("reason", ""):
        t1 = base_trades[0]
        t1_exit_time = t1["exit_time"]
        t1_action = t1["action"]

        if t1_exit_time.strftime("%H:%M:%S") <= "10:00:00":
            # 尋找 10:30 前是否真突破重回 ORB 外
            after_bars = kbars_a.loc[t1_exit_time:]
            orb_h = strat_a.orb_high
            orb_l = strat_a.orb_low

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

                if re_pos == 0 and not item4_triggered and t_str <= "10:30:00":
                    # 若原為做多被洗掉，且價格重新站回 ORB 上界上方 + 10點
                    if t1_action == "BUY" and c >= (orb_h + 10.0) and (c > vwap and ema9 > ema21):
                        re_pos = 1; re_entry_p = c; re_entry_time = ts; item4_triggered = True; re_highest_p = 0.0
                    # 若原為做空被洗掉，且價格重新跌破 ORB 下界下方 - 10點
                    elif t1_action == "SELL" and c <= (orb_l - 10.0) and (c < vwap and ema9 < ema21):
                        re_pos = -1; re_entry_p = c; re_entry_time = ts; item4_triggered = True; re_highest_p = 0.0

                elif re_pos != 0:
                    cur_p = (h - re_entry_p) if re_pos == 1 else (re_entry_p - l)
                    if cur_p > re_highest_p:
                        re_highest_p = cur_p

                    # 13:25 收盤平倉
                    if t_str >= eff_cfg.force_close_time:
                        pnl = (c - re_entry_p) if re_pos == 1 else (re_entry_p - c)
                        item4_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": c, "pnl_pts": pnl, "qty": 1,
                            "reason": f"洗盤追回 13:25 收盤平倉 (+{pnl:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 停損
                    hit_stop = (l <= re_entry_p - eff_cfg.stop_loss_pts) if re_pos == 1 else (h >= re_entry_p + eff_cfg.stop_loss_pts)
                    if hit_stop:
                        exit_p = (re_entry_p - eff_cfg.stop_loss_pts) if re_pos == 1 else (re_entry_p + eff_cfg.stop_loss_pts)
                        item4_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": -eff_cfg.stop_loss_pts, "qty": 1,
                            "reason": f"洗盤追回 初始停損 (-{eff_cfg.stop_loss_pts:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 三階移動停利
                    tr_act = eff_cfg.trailing_activation_pts
                    tr_dist = eff_cfg.trailing_distance_pts
                    hit_tr = (l <= re_entry_p + re_highest_p - tr_dist) if re_pos == 1 else (h >= re_entry_p - re_highest_p + tr_dist)
                    if re_highest_p >= tr_act and hit_tr:
                        exit_p = (re_entry_p + re_highest_p - tr_dist) if re_pos == 1 else (re_entry_p - re_highest_p + tr_dist)
                        pnl = (exit_p - re_entry_p) if re_pos == 1 else (re_entry_p - exit_p)
                        item4_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": pnl, "qty": 1,
                            "reason": f"洗盤追回 三階移動停利 (+{pnl:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 二階鎖利
                    t2_trig = eff_cfg.tier2_trigger_pts
                    t2_lock = eff_cfg.tier2_lock_pts
                    hit_t2 = (l <= re_entry_p + t2_lock) if re_pos == 1 else (h >= re_entry_p - t2_lock)
                    if re_highest_p >= t2_trig and hit_t2:
                        exit_p = (re_entry_p + t2_lock) if re_pos == 1 else (re_entry_p - t2_lock)
                        item4_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": t2_lock, "qty": 1,
                            "reason": f"洗盤追回 二階鎖利 (+{t2_lock:.0f}點)"
                        })
                        re_pos = 0
                        break

                    # 一階鎖利
                    t1_trig = eff_cfg.breakeven_trigger_pts
                    t1_lock = eff_cfg.breakeven_lock_pts
                    hit_t1 = (l <= re_entry_p + t1_lock) if re_pos == 1 else (h >= re_entry_p - t1_lock)
                    if re_highest_p >= t1_trig and hit_t1:
                        exit_p = (re_entry_p + t1_lock) if re_pos == 1 else (re_entry_p - t1_lock)
                        item4_trades.append({
                            "entry_time": re_entry_time, "exit_time": ts, "action": "BUY" if re_pos == 1 else "SELL",
                            "entry_price": re_entry_p, "exit_price": exit_p, "pnl_pts": t1_lock, "qty": 1,
                            "reason": f"洗盤追回 一階鎖利 (+{t1_lock:.0f}點)"
                        })
                        re_pos = 0
                        break

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
    i4_pts, i4_net = summarize(item4_trades)

    results_a.append({"date": d, "pts": a_pts, "net": a_net})
    results_item4.append({"date": d, "pts": i4_pts, "net": i4_net})
    details.append({"date": d, "a_net": a_net, "i4_net": i4_net, "triggered": item4_triggered, "trades": item4_trades})

df_a = pd.DataFrame(results_a)
df_i4 = pd.DataFrame(results_item4)

print("="*85)
print("📊 最新 20 個交易日: 策略 A vs Item 4 (主力甩轎洗盤追回) 績效對照表")
print("="*85)
print(f"指標名稱                      | 策略 A (目前策略，1口)    | Item 4 (洗盤追回，1口)")
print("-"*85)
a_net_tot = df_a['net'].sum()
i4_net_tot = df_i4['net'].sum()
print(f"扣除成本總淨損益 (NTD)          | NT$ {a_net_tot:>10,.0f} 元      | NT$ {i4_net_tot:>10,.0f} 元")

a_pts_tot = df_a['pts'].sum()
i4_pts_tot = df_i4['pts'].sum()
print(f"累積總獲利點數                   | {a_pts_tot:>12.0f} 點      | {i4_pts_tot:>12.0f} 點")

# MDD
cum_a = df_a['net'].cumsum()
mdd_a = (cum_a.cummax() - cum_a).max()
cum_i4 = df_i4['net'].cumsum()
mdd_i4 = (cum_i4.cummax() - cum_i4).max()
print(f"最大區間回撤 (MDD)              | NT$ {mdd_a:>10,.0f} 元      | NT$ {mdd_i4:>10,.0f} 元")

# PF
def get_pf(series):
    pos = series[series > 0].sum()
    neg = abs(series[series < 0].sum())
    return pos / neg if neg > 0 else 99.9

print(f"獲利因子 (Profit Factor)        | {get_pf(df_a['net']):>14.2f}        | {get_pf(df_i4['net']):>14.2f}")
print(f"獲利天數 / 虧損天數             | {(df_a['net']>0).sum()}勝 / {(df_a['net']<0).sum()}負              | {(df_i4['net']>0).sum()}勝 / {(df_i4['net']<0).sum()}負")
print("="*85)

print("\n📅 【Item 4 洗盤追回觸發明細】")
for r in details:
    if r['triggered']:
        print(f"• 日期: {r['date']} | 策略A: NT$ {r['a_net']:+,.0f} 元 -> Item 4: NT$ {r['i4_net']:+,.0f} 元")
        for t in r['trades']:
            print(f"  - [{t['action']}] {t.get('reason')} @ {t['entry_price']:.0f}->{t['exit_price']:.0f} (損益: {t['pnl_pts']:+.0f}點)")
