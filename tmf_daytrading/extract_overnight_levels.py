#!/usr/bin/env python3
"""
extract_overnight_levels.py - 從 Shioaji TMFR1 下載近 25 日 24 小時 1分K，
並精準計算出每個日盤 (08:45~13:45) 所對應的前一日夜盤 (15:00 ~ 次日 05:00) 的:
- overnight_high (夜盤最高價)
- overnight_low (夜盤最低價)
- overnight_close (夜盤收盤價 05:00)
- overnight_volume (夜盤總成交量)
"""
import os
import sys
import json
import datetime
import pandas as pd
import shioaji as sj
from config import ShioajiConfig

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

def main():
    cfg = ShioajiConfig()
    api = sj.Shioaji()
    api.login(api_key=cfg.api_key, secret_key=cfg.secret_key)
    
    tmf_contracts = {c.code: c for c in api.Contracts.Futures.TMF if 'TMF' in c.code}
    tmfr1 = tmf_contracts.get('TMFR1')
    
    today = datetime.date.today()
    start_date = (today - datetime.timedelta(days=35)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")
    
    print(f"📡 正在從 Shioaji 下載 TMFR1 連續近月合約之完整 24h 1分K 資料 ({start_date} ~ {end_date})...")
    kb = api.kbars(tmfr1, start=start_date, end=end_date)
    df = pd.DataFrame({**kb})
    df.columns = [c.lower() for c in df.columns]
    api.logout()
    
    if df.empty:
        print("❌ 無法取得 K 棒資料")
        return
        
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.sort_values('ts').reset_index(drop=True)
    print(f"✅ 取得總 K 棒數: {len(df)} 根 (時間範圍: {df['ts'].min()} ~ {df['ts'].max()})")
    
    # 儲存全天候 1分K 資料以備回測使用
    full_path = os.path.join(DATA_DIR, "tmfr1_full_24h_1m.csv")
    df.to_csv(full_path, index=False)
    print(f"💾 已儲存全時段 1分K 至: {full_path}")
    
    # 解析交易日對應關係:
    # 台指期一個完整交易日包含:
    # 1. 盤後夜盤: 前一日 15:00:00 ~ 當日 05:00:00
    # 2. 一般日盤: 當日 08:45:00 ~ 當日 13:45:00
    
    # 找出所有的日盤日期，加上今日 (若今日夜盤 05:00 已收盤但日盤尚未開始)
    df_day = df[(df['ts'].dt.time >= datetime.time(8, 45)) & (df['ts'].dt.time <= datetime.time(13, 45))]
    trading_dates = set(df_day['ts'].dt.strftime('%Y-%m-%d').unique())
    today_str = today.strftime("%Y-%m-%d")
    end_today_night = datetime.datetime.combine(today, datetime.time(5, 1, 0))
    if not df[(df['ts'] <= end_today_night) & (df['ts'] >= end_today_night - datetime.timedelta(hours=14))].empty:
        trading_dates.add(today_str)
    
    trading_dates = sorted(list(trading_dates))
    print(f"📅 辨識出有效交易日共 {len(trading_dates)} 天: {trading_dates[0]} ~ {trading_dates[-1]}")
    
    overnight_summary = []
    
    for i, date_str in enumerate(trading_dates):
        cur_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        
        # 夜盤起始: 尋找 cur_date 之前最近一個有資料的下午 15:00
        # 範圍: [cur_date - 4天 15:00:00, cur_date 05:01:00] 且時間介於 15:00~05:00
        end_night = datetime.datetime.combine(cur_date, datetime.time(5, 1, 0))
        start_night = datetime.datetime.combine(cur_date - datetime.timedelta(days=4), datetime.time(15, 0, 0))
        
        subset = df[(df['ts'] >= start_night) & (df['ts'] <= end_night)]
        # 找到 subset 中最後一個 hour >= 15 的日期
        evening_bars = subset[subset['ts'].dt.hour >= 15]
        if not evening_bars.empty:
            last_evening_date = evening_bars['ts'].dt.date.max()
            night_start_exact = datetime.datetime.combine(last_evening_date, datetime.time(15, 0, 0))
            night_bars = df[(df['ts'] >= night_start_exact) & (df['ts'] <= end_night)]
        else:
            night_bars = pd.DataFrame()
            
        if not night_bars.empty:
            o_high = float(night_bars['high'].max())
            o_low = float(night_bars['low'].min())
            o_open = float(night_bars.iloc[0]['open'])
            o_close = float(night_bars.iloc[-1]['close'])
            o_range = o_high - o_low
            o_bars = len(night_bars)
            overnight_summary.append({
                "trading_date": date_str,
                "night_start": str(night_bars['ts'].min()),
                "night_end": str(night_bars['ts'].max()),
                "night_bars": o_bars,
                "night_open": o_open,
                "night_high": o_high,
                "night_low": o_low,
                "night_close": o_close,
                "night_range": o_range
            })
            print(f"[{date_str}] 夜盤 K 棒 {o_bars} 根 | 夜高: {o_high:.0f} | 夜低: {o_low:.0f} | 振幅: {o_range:.0f} 點")
        else:
            print(f"[{date_str}] ⚠️ 無對應夜盤資料")
            
    new_summary_df = pd.DataFrame(overnight_summary)
    
    # 合併現有檔案以確保歷史不丟失
    for fn in ["overnight_levels_summary.csv", "overnight_levels_30days.csv"]:
        fpath = os.path.join(DATA_DIR, fn)
        if os.path.exists(fpath):
            try:
                old_df = pd.read_csv(fpath)
                combined = pd.concat([old_df[~old_df["trading_date"].isin(new_summary_df["trading_date"])], new_summary_df], ignore_index=True)
                combined = combined.sort_values("trading_date").reset_index(drop=True)
                combined.to_csv(fpath, index=False)
                print(f"💾 已更新並儲存至: {fpath} (共 {len(combined)} 筆)")
                continue
            except Exception as e:
                print(f"合併 {fn} 時異常: {e}")
        new_summary_df.to_csv(fpath, index=False)
        print(f"💾 已覆寫儲存至: {fpath} (共 {len(new_summary_df)} 筆)")

if __name__ == "__main__":
    main()
