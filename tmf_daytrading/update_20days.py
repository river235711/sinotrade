#!/usr/bin/env bash
# update_20days.py - 每日收盤後自動滾動更新最新 20 個交易日數據集
import os
import sys
import json
import urllib.request
import datetime
import subprocess
import pandas as pd
from strategies.atr_scaler import update_daily_history, compute_atr, scale_strategy_config
from config import ORBStrategyConfig

def fetch_intraday_kbars_cnyes(today_str: str) -> pd.DataFrame:
    """從高速台指期行情接口獲取指定日期的完整 1分K 資料 (08:45 ~ 13:45)"""
    url = "https://ws.api.cnyes.com/ws/api/v1/charting/history?symbol=TWF:TXF:FUTURES&resolution=1&quote=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        payload = json.loads(r.read().decode("utf-8"))
    data = payload.get("data", {})
    t_list = data.get("t", [])
    if not t_list:
        return pd.DataFrame()
    df = pd.DataFrame({
        "timestamp": [datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") for ts in t_list],
        "open": data.get("o", []),
        "high": data.get("h", []),
        "low": data.get("l", []),
        "close": data.get("c", []),
        "volume": data.get("v", []),
        "date": today_str,
        "contract": "AUTO"
    })
    df["ts"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("ts").sort_index()
    day_df = df[df.index.strftime("%Y-%m-%d") == today_str].between_time("08:45", "13:45")
    return day_df.reset_index(drop=True)

def roll_20days(target_date: str = None, push_git: bool = True):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "data")
    cache_path = os.path.join(data_dir, "tmf_20days_1m_kbars.csv")

    today_str = target_date or datetime.date.today().strftime("%Y-%m-%d")
    print("=" * 80)
    print(f"🔄 啟動 TMF 滾動 20 日窗口資料庫自動更新 (目標日: {today_str})")
    print("=" * 80)

    # 1. 抓取今日完整 1分K
    print(f"📡 正在抓取 {today_str} 日盤 (08:45 ~ 13:45) 完整 1分K 資料...")
    today_kbars = fetch_intraday_kbars_cnyes(today_str)
    if today_kbars.empty or len(today_kbars) < 60:
        print(f"⚠️ 今日 ({today_str}) 抓取的 K 棒筆數不足 ({len(today_kbars)} 筆)，可能非交易日或尚未開盤。")
        return False

    print(f"✅ 成功抓取今日日盤 K 棒，共 {len(today_kbars)} 根 1分K。")
    print(f"   開盤: {today_kbars.iloc[0]['open']:.0f} | 最高: {today_kbars['high'].max():.0f} | 最低: {today_kbars['low'].min():.0f} | 收盤: {today_kbars.iloc[-1]['close']:.0f}")

    # 2. 讀取現有 20 日資料
    if not os.path.exists(cache_path):
        print(f"❌ 找不到基準資料檔: {cache_path}")
        return False

    old_df = pd.read_csv(cache_path)
    old_dates = sorted(old_df["date"].unique())
    print(f"📂 現有資料庫共包含 {len(old_dates)} 個交易日 ({old_dates[0]} ~ {old_dates[-1]})")

    # 3. 合併與滾動剔除
    # 先過濾掉同日期的舊紀錄 (覆蓋)
    clean_df = old_df[old_df["date"] != today_str]
    combined_df = pd.concat([clean_df, today_kbars], ignore_index=True)
    combined_df["ts"] = pd.to_datetime(combined_df["timestamp"])
    combined_df = combined_df.sort_values("ts").reset_index(drop=True)
    combined_df = combined_df.drop(columns=["ts"], errors="ignore")

    all_dates = sorted(combined_df["date"].unique())
    if len(all_dates) > 20:
        dropped_dates = all_dates[:-20]
        kept_dates = all_dates[-20:]
        print(f"✂️ 執行滾動更新：自動剔除最舊 {len(dropped_dates)} 個交易日 {dropped_dates}，保留最新 20 個交易日！")
        combined_df = combined_df[combined_df["date"].isin(kept_dates)].reset_index(drop=True)
    else:
        kept_dates = all_dates

    # 儲存回 CSV
    combined_df.to_csv(cache_path, index=False)
    print(f"💾 已成功覆蓋儲存至: {cache_path}")
    print(f"📅 最新 20 日區間: {kept_dates[0]} ~ {kept_dates[-1]} (共 {len(kept_dates)} 天，總計 {len(combined_df)} 根 K 棒)")

    # 3b. 同步更新永久日 K 歷史檔 (daily_bars.csv)，並預告下一交易日的 ATR 動態參數
    daily_hist = update_daily_history(data_dir, today_kbars)
    base_cfg = ORBStrategyConfig()
    next_atr, n_days = compute_atr(daily_hist, "9999-12-31", period=base_cfg.atr_period, min_days=base_cfg.atr_min_days)
    next_cfg, next_info = scale_strategy_config(base_cfg, next_atr)
    print(f"📐 永久日 K 歷史已更新 ({len(daily_hist)} 日) | 下一交易日 20日ATR={next_atr:.0f}點 (比例 {next_info['ratio']:.2f}x) -> 停損 -{next_cfg.stop_loss_pts:.0f}點 / 一階 +{next_cfg.breakeven_trigger_pts:.0f}→+{next_cfg.breakeven_lock_pts:.0f}點 / 二階 +{next_cfg.tier2_trigger_pts:.0f}→+{next_cfg.tier2_lock_pts:.0f}點")

    # 4. 自動重新執行 20 日回測並產出績效圖表
    print("\n📊 正在自動重新執行最新 20 日回測並產出報表圖表...")
    backtest_script = os.path.join(base_dir, "run_20days_backtest.py")
    try:
        subprocess.run([sys.executable, backtest_script], check=True)
    except Exception as e:
        print(f"⚠️ 重新執行回測時發生錯誤: {e}")

    # 5. 自動 Commit & Push 到 GitHub
    if push_git:
        push_script = os.path.join(base_dir, "push.sh")
        if os.path.exists(push_script):
            commit_msg = f"data: Roll 20-day dataset to {today_str} ({kept_dates[0]} ~ {kept_dates[-1]})"
            print(f"\n🚀 正在自動推送最新資料與圖表至 GitHub: {commit_msg} ...")
            subprocess.run(["bash", push_script, commit_msg], check=False)

    print("\n🎉 滾動 20 日更新與回測流程全部順利完成！")
    return True

if __name__ == "__main__":
    t_date = sys.argv[1] if len(sys.argv) > 1 else None
    roll_20days(t_date)
