import os
import sys
import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from config import InstrumentConfig, ORBStrategyConfig
from backtester.engine import BacktestEngine
from backtester.metrics import BacktestMetrics

def run_test(use_wall_filter=False):
    cache_path = os.path.join(BASE_DIR, "data", "tmf_20days_1m_kbars.csv")
    df_cache = pd.read_csv(cache_path)
    target_dates = sorted(df_cache['date'].unique())[-20:]
    trading_days = [(d.replace('-', '_'), d.replace('-', '')) for d in target_dates]

    inst_cfg = InstrumentConfig(symbol="TMF", multiplier=10.0, commission_per_order=12.0, tax_rate=0.00001, slippage_points=1.0)
    strat_cfg = ORBStrategyConfig(use_overnight_wall_filter=use_wall_filter)

    all_trades = []
    daily_summaries = []

    for file_date, target_date in trading_days:
        csv_path = os.path.join(BASE_DIR, "data", f"Daily_{file_date}.csv")
        engine = BacktestEngine(data_path=csv_path, inst_config=inst_cfg)
        kbars = engine.load_taifex_csv(target_date=target_date, contract_month="AUTO")
        metrics = engine.run_orb_strategy(strat_cfg)
        tdf = metrics["trades_df"]
        day_date_fmt = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"

        day_pts = metrics["total_points"] if not tdf.empty else 0.0
        day_ntd = metrics["net_profit_ntd"] if not tdf.empty else 0.0

        daily_summaries.append({
            "date": day_date_fmt,
            "trades": metrics["total_trades"],
            "pts": day_pts,
            "net_ntd": day_ntd,
            "reason": tdf.iloc[0]["reason"] if not tdf.empty else "無進場"
        })

        if not tdf.empty:
            for _, r in tdf.iterrows():
                r_dict = r.to_dict()
                r_dict["date"] = day_date_fmt
                all_trades.append(r_dict)

    all_df = pd.DataFrame(all_trades)
    total_trades = len(all_df)
    wins = all_df[all_df["net_ntd"] > 0]
    losses = all_df[all_df["net_ntd"] <= 0]
    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
    total_pts = all_df["pnl_pts"].sum()
    total_ntd = all_df["net_ntd"].sum()
    gross_win = wins["net_ntd"].sum() if len(wins) > 0 else 0.0
    gross_loss = abs(losses["net_ntd"].sum()) if len(losses) > 0 else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else 999.0

    all_df["cum_ntd"] = all_df["net_ntd"].cumsum()
    all_df["peak_ntd"] = all_df["cum_ntd"].cummax().clip(lower=0)
    all_df["dd_ntd"] = all_df["peak_ntd"] - all_df["cum_ntd"]
    mdd_ntd = all_df["dd_ntd"].max() if len(all_df) > 0 else 0.0

    return {
        "dates": target_dates,
        "total_trades": total_trades,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "total_pts": total_pts,
        "total_ntd": total_ntd,
        "pf": pf,
        "mdd_ntd": mdd_ntd,
        "daily": daily_summaries,
        "trades": all_trades
    }

def main():
    res_before = run_test(use_wall_filter=False)
    res_after = run_test(use_wall_filter=True)

    print("=" * 100)
    print("🔬 TMF 20 個交易日真實回測對決：之前的策略 (未加防撞牆) vs 現在啟用的策略 (加上防撞牆)")
    print(f"📅 回測區間: {res_before['dates'][0]} ~ {res_before['dates'][-1]} (共 20 個交易日)")
    print("=" * 100)

    print(f"{'指標名稱':<25} | {'之前的策略 (未加防撞牆)':<22} | {'現在的策略 (加上防撞牆)':<22} | {'優化差異'}")
    print("-" * 100)
    print(f"{'扣除成本總淨損益 (NTD)':<23} | NT$ {res_before['total_ntd']:>12,.0f} 元 | NT$ {res_after['total_ntd']:>12,.0f} 元 | NT$ {res_after['total_ntd'] - res_before['total_ntd']:>+8,.0f} 元 (+{(res_after['total_ntd']-res_before['total_ntd'])/res_before['total_ntd']*100:.1f}%)")
    print(f"{'累積總獲利點數':<25} | {res_before['total_pts']:>14.0f} 點 | {res_after['total_pts']:>14.0f} 點 | {res_after['total_pts'] - res_before['total_pts']:>+10.0f} 點")
    print(f"{'單筆交易勝率 (Win Rate)':<23} | {res_before['win_rate']:>13.1f}% ({res_before['win_count']}勝{res_before['loss_count']}敗) | {res_after['win_rate']:>13.1f}% ({res_after['win_count']}勝{res_after['loss_count']}敗) | {res_after['win_rate'] - res_before['win_rate']:>+9.1f}%")
    print(f"{'獲利因子 (Profit Factor)':<22} | {res_before['pf']:>16.2f}   | {res_after['pf']:>16.2f}   | {res_after['pf'] - res_before['pf']:>+11.2f}")
    print(f"{'最大區間回撤 (MDD NTD)':<22} | NT$ {res_before['mdd_ntd']:>12,.0f} 元 | NT$ {res_after['mdd_ntd']:>12,.0f} 元 | NT$ {res_after['mdd_ntd'] - res_before['mdd_ntd']:>+8,.0f} 元")
    print(f"{'總交易筆數':<26} | {res_before['total_trades']:>14} 筆 | {res_after['total_trades']:>14} 筆 | 持平")
    print("=" * 100)

    print("\n📅 【20 個交易日每日損益逐日對照表 (NTD)】")
    print(f"{'交易日期':<12} | {'之前的策略':<14} | {'現在的策略':<14} | {'損益差異':<12} | {'關鍵說明'}")
    print("-" * 80)
    for b, a in zip(res_before["daily"], res_after["daily"]):
        diff = a["net_ntd"] - b["net_ntd"]
        note = "⚪ 損益完全相同"
        if diff > 0:
            note = f"🟢 現在策略勝出 (+NT$ {diff:,.0f} 元)"
        elif diff < 0:
            note = f"🔴 之前策略較佳 (-NT$ {abs(diff):,.0f} 元)"
        print(f"{b['date']:<12} | NT$ {b['net_ntd']:>8,.0f} 元 | NT$ {a['net_ntd']:>8,.0f} 元 | NT$ {diff:>+8,.0f} 元 | {note}")

if __name__ == "__main__":
    main()
