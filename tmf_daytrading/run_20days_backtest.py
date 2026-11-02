#!/usr/bin/env python3
"""
TMF (微型臺指期) 當沖交易程式 - 前 20 個交易日回測腳本
回測區間: 2026-09-30 ~ 2026-10-30 (共 20 個交易日)
商品: 微型臺指期近月主力合約 (TMF)
策略: ORB 開盤區間突破策略 + 移動停利 (Trailing Stop)
"""
import os
import shutil
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from config import InstrumentConfig, ORBStrategyConfig
from backtester.engine import BacktestEngine

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(base_dir, "data", "tmf_20days_1m_kbars.csv")
    if os.path.exists(cache_path):
        cached_df = pd.read_csv(cache_path)
        all_dates = sorted(cached_df['date'].unique())
        target_dates = all_dates[-20:]
    else:
        target_dates = [
            "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
            "2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14",
            "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21",
            "2026-10-22", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"
        ]

    trading_days = [(d.replace('-', '_'), d.replace('-', '')) for d in target_dates]
    start_date_str = target_dates[0]
    end_date_str = target_dates[-1]

    inst_cfg = InstrumentConfig(
        symbol="TMF",
        multiplier=10.0,
        commission_per_order=12.0,
        tax_rate=0.00001,
        slippage_points=1.0
    )

    strat_cfg = ORBStrategyConfig()

    all_trades = []
    daily_summaries = []

    print("=" * 95)
    print(f"🚀 TMF 微型臺指期貨 - 前 20 個交易日當沖回測報告 ({start_date_str} ~ {end_date_str})")
    print("=" * 95)

    for file_date, target_date in trading_days:
        csv_path = os.path.join(base_dir, "data", f"Daily_{file_date}.csv")
        engine = BacktestEngine(data_path=csv_path, inst_config=inst_cfg)
        kbars = engine.load_taifex_csv(target_date=target_date, contract_month="AUTO")
        contract_m = engine.active_contract_month
        metrics = engine.run_orb_strategy(strat_cfg)

        tdf = metrics["trades_df"]
        day_date_fmt = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
        day_open = kbars.iloc[0]['open']
        day_high = kbars['high'].max()
        day_low = kbars['low'].min()
        day_close = kbars.iloc[-1]['close']
        orb_h = metrics["orb_high"]
        orb_l = metrics["orb_low"]

        day_pts = metrics["total_points"] if not tdf.empty else 0.0
        day_ntd = metrics["net_profit_ntd"] if not tdf.empty else 0.0

        daily_summaries.append({
            "date": day_date_fmt,
            "contract": contract_m,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "close": day_close,
            "range": day_high - day_low,
            "orb_high": orb_h,
            "orb_low": orb_l,
            "orb_range": orb_h - orb_l,
            "trades": metrics["total_trades"],
            "win_count": metrics.get("win_count", 0),
            "loss_count": metrics.get("loss_count", 0),
            "points_pnl": day_pts,
            "net_ntd": day_ntd
        })

        if not tdf.empty:
            for _, row in tdf.iterrows():
                r_dict = row.to_dict()
                r_dict["date"] = day_date_fmt
                r_dict["contract"] = contract_m
                all_trades.append(r_dict)

    # 印出每日摘要表
    print("\n📅 【20 個交易日每日行情與損益總覽】")
    print("-" * 95)
    print(f"{'日期':<12} {'合約':<8} {'開盤':<7} {'最高':<7} {'最低':<7} {'收盤':<7} {'全日振幅':<8} {'ORB上界':<8} {'ORB下界':<8} {'筆數':<4} {'點數損益':<8} {'淨損益(NTD)':<10}")
    print("-" * 95)
    for d in daily_summaries:
        pts_str = f"{d['points_pnl']:+.0f}"
        ntd_str = f"{d['net_ntd']:+,.0f}"
        print(f"{d['date']:<12} {d['contract']:<8} {d['open']:<7.0f} {d['high']:<7.0f} {d['low']:<7.0f} {d['close']:<7.0f} {d['range']:<8.0f} {d['orb_high']:<8.0f} {d['orb_low']:<8.0f} {d['trades']:<4} {pts_str:<8} {ntd_str:<10}")
    print("-" * 95)

    # 印出所有逐筆交易明細
    print("\n📋 【20 個交易日完整逐筆交易時間與回補明細】")
    print("-" * 95)
    for idx, t in enumerate(all_trades, 1):
        act_str = "🟢 做多 (BUY)" if t["action"] == "BUY" else "🔴 做空 (SELL)"
        e_t = pd.to_datetime(t["entry_time"]).strftime("%H:%M:%S")
        x_t = pd.to_datetime(t["exit_time"]).strftime("%H:%M:%S")
        dur_min = int((pd.to_datetime(t["exit_time"]) - pd.to_datetime(t["entry_time"])).total_seconds() // 60)
        print(f"#{idx:02d} | {t['date']} ({t['contract']}) | {act_str} | 進場: {e_t} @ {t['entry_price']:.0f} -> 回補: {x_t} @ {t['exit_price']:.0f} ({dur_min}分) | {t['pnl_pts']:+.0f}點 ({t['net_ntd']:+,.0f}元) | {t['reason']}")

    # 計算 20 日總體統計
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
    mdd_ntd = all_df["dd_ntd"].max()

    daily_df = pd.DataFrame(daily_summaries)
    win_days = (daily_df["net_ntd"] > 0).sum()
    loss_days = (daily_df["net_ntd"] < 0).sum()
    flat_days = (daily_df["net_ntd"] == 0).sum()

    print("\n" + "=" * 95)
    print("📈 【前 20 個交易日整體績效統計 (20-Day Performance Summary)】")
    print("=" * 95)
    print(f"• 回測期間:                 {start_date_str} ~ {end_date_str} (共 {len(daily_summaries)} 個交易日)")
    print(f"• 獲利天數 / 虧損天數:      {win_days} 天獲利 / {loss_days} 天虧損 / {flat_days} 天無交易 (有交易日勝率: {win_days/(win_days+loss_days)*100:.1f}%)" if (win_days+loss_days)>0 else "• 獲利天數 / 虧損天數: 0")
    print(f"• 總交易筆數:               {total_trades} 筆 (做多 {(all_df['action']=='BUY').sum() if not all_df.empty else 0} 筆 / 做空 {(all_df['action']=='SELL').sum() if not all_df.empty else 0} 筆)")
    print(f"• 單筆交易勝率 (Win Rate):  {win_rate:.1f}% ({len(wins)} 勝 / {len(losses)} 負)")
    print(f"• 累積總獲利點數:           {total_pts:+.0f} 點")
    print(f"• 扣除成本總淨損益 (NTD):   NT$ {total_ntd:+,.0f} 元")
    print(f"• 獲利因子 (Profit Factor): {pf:.2f}")
    print(f"• 平均每筆淨損益:           NT$ {(total_ntd/total_trades) if total_trades>0 else 0:+,.0f} 元")
    print(f"• 最大區間回撤 (MDD):       NT$ {mdd_ntd:,.0f} 元")
    print("=" * 95)

    # 繪製 20 日損益圖表
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 8.5), gridspec_kw={'height_ratios': [1.3, 1]})
    plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')

    dates_short = [d["date"][5:] for d in daily_summaries]
    daily_ntds = [d["net_ntd"] for d in daily_summaries]
    cum_daily_ntd = np.cumsum(daily_ntds)

    # 上圖：20 日累積淨損益曲線
    ax1.plot(dates_short, cum_daily_ntd, marker='o', color='#8e44ad', linewidth=2.2, markersize=6, label='Cumulative Net PnL (NTD)')
    ax1.fill_between(range(len(dates_short)), cum_daily_ntd, 0, color='#8e44ad', alpha=0.15)
    for i, val in enumerate(cum_daily_ntd):
        ax1.annotate(f"{val:+,.0f}", (i, val), textcoords="offset points", xytext=(0, 8), ha='center', fontsize=7.5, fontweight='bold')
    ax1.axhline(0, color='black', linestyle='--', linewidth=0.8)
    ax1.set_title(f"TMF Micro Taiwan Index - 20-Day ORB Strategy Cumulative Equity Curve ({start_date_str} ~ {end_date_str})", fontsize=13, fontweight='bold', pad=12)
    ax1.set_ylabel("Cumulative Net PnL (NTD)", fontsize=11)
    ax1.legend(loc='upper left')
    ax1.grid(True, linestyle=':', alpha=0.6)

    # 下圖：每日單日損益長條圖
    bar_colors = ['#27ae60' if v >= 0 else '#e74c3c' for v in daily_ntds]
    bars = ax2.bar(dates_short, daily_ntds, color=bar_colors, alpha=0.85, width=0.6)
    for bar, val in zip(bars, daily_ntds):
        y_pos = val + (60 if val >= 0 else -140)
        ax2.annotate(f"{val:+,.0f}", (bar.get_x() + bar.get_width()/2, y_pos), ha='center', fontsize=7.5, fontweight='bold')
    ax2.axhline(0, color='black', linewidth=0.8)
    ax2.set_title("Daily Net Profit / Loss (NTD)", fontsize=12, fontweight='bold')
    ax2.set_ylabel("Daily Net PnL (NTD)", fontsize=11)
    ax2.set_xlabel("Trading Date (2026)", fontsize=11)
    ax2.set_ylim(min(daily_ntds) - 350, max(daily_ntds) + 350)
    ax2.grid(True, linestyle=':', alpha=0.6)
    plt.xticks(rotation=30)

    plt.tight_layout()
    chart_path = os.path.join(base_dir, "tmf_backtest_20days.png")
    plt.savefig(chart_path, dpi=200)
    plt.close()

    artifact_dir = r"C:\Users\ting0\.gemini\antigravity\brain\0eeb3784-cd73-4675-a9e0-4753b8b0afaf"
    wsl_artifact_dir = "/mnt/c/Users/ting0/.gemini/antigravity/brain/0eeb3784-cd73-4675-a9e0-4753b8b0afaf"
    for ad in [wsl_artifact_dir, artifact_dir]:
        if os.path.exists(ad):
            try:
                shutil.copy(chart_path, os.path.join(ad, "tmf_backtest_20days.png"))
            except Exception:
                pass

if __name__ == "__main__":
    main()
