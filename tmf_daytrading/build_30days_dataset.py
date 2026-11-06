#!/usr/bin/env python3
"""
build_30days_dataset.py - 建立完整 30 個交易日的日盤 1分K、歷史日K 與夜盤高低點資料庫 (標準化 K 棒起始時間戳)
"""
import os
import sys
import datetime
import pandas as pd
import shioaji as sj

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from config import ShioajiConfig

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

def main():
    full_path = os.path.join(DATA_DIR, "tmfr1_full_3months_1m.csv")
    if os.path.exists(full_path):
        print(f"📂 發現本地已快取的全時段 1分K 資料: {full_path}")
        df = pd.read_csv(full_path)
    else:
        cfg = ShioajiConfig()
        api = sj.Shioaji()
        print("🔑 正在登入永豐金 Shioaji API...")
        api.login(api_key=cfg.api_key, secret_key=cfg.secret_key)
        tmfr1 = [c for c in api.Contracts.Futures.TMF if c.code == 'TMFR1'][0]
        
        print("📡 下載 TMFR1 從 2026-08-01 至 2026-11-06 全時段 1分K 資料...")
        kb = api.kbars(tmfr1, start='2026-08-01', end='2026-11-06')
        df = pd.DataFrame({**kb})
        df.columns = [c.lower() for c in df.columns]
        api.logout()
        print("🔌 已登出 Shioaji API")
        df.to_csv(full_path, index=False)
        print(f"💾 已儲存全時段 1分K 至: {full_path}")
        
    if df.empty:
        print("❌ 無法取得 K 棒資料")
        return
        
    # 重要轉換：Shioaji K 棒時間戳為收盤時間 (Close Timestamp，如 08:46:00 代表 08:45~08:46 該根 K 棒)
    # 本系統策略核心與 CNYES 歷史數據庫皆採用起始時間戳 (Start Timestamp，即 08:45:00 代表 08:45~08:46 該根 K 棒)
    # 因此減去 1 分鐘，使 ORB (between_time('09:00', '09:15')) 與回測進場 (09:16:00) 100% 精準對齊！
    df['ts'] = pd.to_datetime(df['ts']) - pd.Timedelta(minutes=1)
    df = df.sort_values('ts').reset_index(drop=True)
    print(f"✅ 轉換為標準起始時間戳，總 K 棒數: {len(df)} 根 (範圍: {df['ts'].min()} ~ {df['ts'].max()})")
    
    # 1. 產生完整的歷史日K (08:45:00 ~ 13:44:00)，供 20日動態 ATR 計算
    df_day = df[(df['ts'].dt.time >= datetime.time(8, 45)) & (df['ts'].dt.time <= datetime.time(13, 44))].copy()
    df_day['date'] = df_day['ts'].dt.strftime('%Y-%m-%d')
    
    all_dates = sorted(df_day['date'].unique())
    print(f"📅 總日盤交易日數: {len(all_dates)} 天 ({all_dates[0]} ~ {all_dates[-1]})")
    
    daily_records = []
    for d in all_dates:
        sub = df_day[df_day['date'] == d]
        daily_records.append({
            "date": d,
            "open": float(sub.iloc[0]['open']),
            "high": float(sub['high'].max()),
            "low": float(sub['low'].min()),
            "close": float(sub.iloc[-1]['close'])
        })
    daily_df = pd.DataFrame(daily_records)
    daily_path = os.path.join(DATA_DIR, "daily_bars.csv")
    daily_df.to_csv(daily_path, index=False)
    print(f"💾 已更新完整歷史日K ({len(daily_df)} 天) 至: {daily_path}")
    
    # 2. 取最後 30 個交易日
    assert len(all_dates) >= 30, f"交易日不足 30 天 (目前只有 {len(all_dates)} 天)"
    target_30_dates = all_dates[-30:]
    print(f"\n🎯 本次回測前 30 個交易日範圍: {target_30_dates[0]} ~ {target_30_dates[-1]} (共 {len(target_30_dates)} 天)")
    
    # 3. 匯出這 30 個交易日的日盤 1分K
    df_30_day = df_day[df_day['date'].isin(target_30_dates)].copy()
    df_30_day['timestamp'] = df_30_day['ts'].dt.strftime('%Y-%m-%d %H:%M:%S')
    df_30_day['contract'] = 'TMFR1'
    cols_out = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'date', 'contract']
    df_30_day_out = df_30_day[cols_out].sort_values('timestamp')
    kbars_30_path = os.path.join(DATA_DIR, "tmf_30days_1m_kbars.csv")
    df_30_day_out.to_csv(kbars_30_path, index=False)
    print(f"💾 已儲存 30 日日盤 1分K ({len(df_30_day_out)} 根) 至: {kbars_30_path}")
    
    # 4. 抽取這 30 個交易日所對應的前一日夜盤極值 (15:00:00 ~ 04:59:00)
    overnight_summary = []
    for date_str in target_30_dates:
        cur_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        # 夜盤結尾為當日上午 04:59:59 (即收盤於 05:00:00 的那根 K 棒)
        end_night = datetime.datetime.combine(cur_date, datetime.time(4, 59, 59))
        start_night = datetime.datetime.combine(cur_date - datetime.timedelta(days=4), datetime.time(15, 0, 0))
        
        subset = df[(df['ts'] >= start_night) & (df['ts'] <= end_night)]
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
            print(f"[{date_str}] 夜盤 {o_bars:>3} 根 | 夜高: {o_high:.0f} | 夜低: {o_low:.0f} | 振幅: {o_range:>4.0f} 點")
        else:
            print(f"[{date_str}] ⚠️ 無對應夜盤資料")
            
    summary_df = pd.DataFrame(overnight_summary)
    summary_path = os.path.join(DATA_DIR, "overnight_levels_30days.csv")
    summary_df.to_csv(summary_path, index=False)
    print(f"\n💾 30 日夜盤高低點彙整表 ({len(summary_df)} 天) 已儲存至: {summary_path}")

if __name__ == "__main__":
    main()
