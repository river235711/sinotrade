#!/usr/bin/env python3
# live/update_20days.py - 每日收盤後自動滾動更新最新 20 個交易日數據集
import os
import sys
sys.dont_write_bytecode = True

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import datetime
import subprocess
import pandas as pd
from strategies.atr_scaler import update_daily_history, config_for_date, describe
from config import ORBStrategyConfig, atomic_write_csv
from live.market_data import (
    resolve_tmf_contract,
    normalize_shioaji_kbars,
    fetch_cnyes_day_kbars,
    is_overnight_row_complete,
    load_or_fetch_overnight_levels,
)


def fetch_intraday_kbars_shioaji(today_str: str) -> pd.DataFrame:
    """
    從永豐金 Shioaji 原生 API 獲取今日完整日盤 1分K 資料 (08:45 ~ 13:45) 並對齊起始時間戳。
    作為第一優先數據源，具備 100% 原生期貨精度，不依賴外部爬蟲或第三方接口。
    """
    api = None
    try:
        import shioaji as sj
        from config import ShioajiConfig
        cfg = ShioajiConfig()
        api = sj.Shioaji()
        api.login(api_key=cfg.api_key, secret_key=cfg.secret_key)

        contract = resolve_tmf_contract(api, "TMFR1")
        if contract is None:
            return pd.DataFrame()

        kb = api.kbars(contract, start=today_str, end=today_str, timeout=10000)
        df = normalize_shioaji_kbars(kb, subtract_minute=True)
        if df.empty:
            return pd.DataFrame()

        df = df.reset_index()
        df_day = df[(df["ts"].dt.time >= datetime.time(8, 45)) & (df["ts"].dt.time <= datetime.time(13, 44))].copy()
        if df_day.empty:
            return pd.DataFrame()
        df_day["timestamp"] = df_day["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        df_day["date"] = today_str
        df_day["contract"] = "TMFR1"
        cols = ["timestamp", "open", "high", "low", "close", "volume", "date", "contract"]
        return df_day[cols].reset_index(drop=True)
    except Exception as e:
        print(f"⚠️ 從 Shioaji 原生 API 獲取今日 K 棒異常: {e}")
        return pd.DataFrame()
    finally:
        if api is not None:
            try:
                api.logout()
            except Exception:
                pass


def fetch_intraday_kbars_cnyes(today_str: str) -> pd.DataFrame:
    """從 CNYES 高速台指期行情接口獲取指定日期的完整 1分K 資料 (08:45 ~ 13:44)，標記為大台備援 (TXF_BKP)"""
    try:
        day_df = fetch_cnyes_day_kbars(today_str, timeout=8)
        if day_df.empty:
            return pd.DataFrame()
        print("⚠️ [CNYES 備援資料] 本日日盤 1分K 改由 CNYES 大台(TXF) 公開行情回補（成交量屬 TXF 量能，已標記 contract=TXF_BKP）。")
        out = day_df.reset_index().copy()
        out["timestamp"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        out["date"] = today_str
        out["contract"] = "TXF_BKP"
        cols = ["timestamp", "open", "high", "low", "close", "volume", "date", "contract"]
        return out[cols].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def archive_1m_kbars(history_path: str, seed_df: pd.DataFrame, today_kbars: pd.DataFrame, today_str: str) -> pd.DataFrame:
    """將每日 1分K 原子化累積歸檔至永久歷史庫 (history_1m_kbars.csv)，永不剔除超過 20 日的歷史 K 棒"""
    if os.path.exists(history_path):
        hist_base = pd.read_csv(history_path)
    else:
        hist_base = seed_df.copy()

    clean_hist = hist_base[hist_base["date"] != today_str] if not hist_base.empty else hist_base
    merged = today_kbars.copy() if clean_hist.empty else pd.concat([clean_hist, today_kbars], ignore_index=True)
    merged["ts"] = pd.to_datetime(merged["timestamp"])
    merged = merged.sort_values("ts").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    merged = merged.drop(columns=["ts"], errors="ignore")
    atomic_write_csv(merged, history_path, index=False)
    return merged


def backfill_overnight_level(data_dir: str, target_date_str: str) -> bool:
    """若今日夜盤彙整紀錄遺失或不完整（例如半夜誤存半截夜盤），以 Shioaji 原生行情補齊 overnight_levels_summary.csv"""
    lvl_path = os.path.join(data_dir, "overnight_levels_summary.csv")
    if os.path.exists(lvl_path):
        try:
            df_lvl = pd.read_csv(lvl_path)
            match_row = df_lvl[df_lvl["trading_date"] == target_date_str]
            if not match_row.empty and is_overnight_row_complete(match_row.iloc[0].to_dict(), target_date_str):
                return True
        except Exception:
            pass

    api = None
    try:
        import shioaji as sj
        from config import ShioajiConfig
        cfg = ShioajiConfig()
        api = sj.Shioaji()
        api.login(api_key=cfg.api_key, secret_key=cfg.secret_key)
        contract = resolve_tmf_contract(api, "TMFR1")
        o_h, o_l, src = load_or_fetch_overnight_levels(
            data_dir,
            target_date_str,
            api=api,
            contract=contract,
            allow_cnyes_fallback=False,
        )
        if o_h is not None and o_l is not None:
            print(f"🌙 [盤後夜盤補齊] 已透過 {src} 補齊 {target_date_str} 完整夜盤極值: 夜高={o_h:.0f} / 夜低={o_l:.0f}")
            return True
    except Exception as e:
        print(f"⚠️ [盤後夜盤補齊] 補抓 {target_date_str} 夜盤極值略過: {e}")
    finally:
        if api is not None:
            try:
                api.logout()
            except Exception:
                pass
    return False

def roll_20days(target_date: str = None, push_git: bool = True):
    base_dir = BASE_DIR
    data_dir = os.path.join(base_dir, "data")
    cache_path = os.path.join(data_dir, "tmf_20days_1m_kbars.csv")

    today_str = target_date or datetime.date.today().strftime("%Y-%m-%d")
    print("=" * 80)
    print(f"🔄 啟動 TMF 滾動 20 日窗口資料庫自動更新 (目標日: {today_str})")
    print("=" * 80)

    # 1. 抓取今日完整 1分K (優先使用 Shioaji 原生主力合約，備援使用 CNYES)
    print(f"📡 正在抓取 {today_str} 日盤 (08:45 ~ 13:45) 完整 1分K 資料...")
    today_kbars = fetch_intraday_kbars_shioaji(today_str)
    if today_kbars.empty or len(today_kbars) < 60:
        print("ℹ️ 嘗試自備援接口 (CNYES) 回補...")
        today_kbars = fetch_intraday_kbars_cnyes(today_str)
    if today_kbars.empty or len(today_kbars) < 60:
        print(f"⚠️ 今日 ({today_str}) 抓取的 K 棒筆數不足 ({len(today_kbars)} 筆)，可能非交易日或尚未開盤。")
        return False

    print(f"✅ 成功抓取今日日盤 K 棒，共 {len(today_kbars)} 根 1分K。")
    print(f"   開盤: {today_kbars.iloc[0]['open']:.0f} | 最高: {today_kbars['high'].max():.0f} | 最低: {today_kbars['low'].min():.0f} | 收盤: {today_kbars.iloc[-1]['close']:.0f}")

    # 2. 讀取現有 20 日資料 (支援缺檔時由永久歷史庫或零起始冷啟動建立)
    history_path = os.path.join(data_dir, "history_1m_kbars.csv")
    if os.path.exists(cache_path):
        old_df = pd.read_csv(cache_path)
    elif os.path.exists(history_path):
        print(f"ℹ️ 找不到滾動快取檔，改從永久歷史庫冷啟動重建: {history_path}")
        old_df = pd.read_csv(history_path)
    else:
        print(f"ℹ️ 找不到既有快取資料檔，將從零建立起始 20 日資料庫: {cache_path}")
        old_df = pd.DataFrame(columns=today_kbars.columns)

    old_dates = sorted(old_df["date"].dropna().unique()) if not old_df.empty else []
    if old_dates:
        print(f"📂 現有資料庫共包含 {len(old_dates)} 個交易日 ({old_dates[0]} ~ {old_dates[-1]})")

    # 3. 合併與滾動剔除
    # 先過濾掉同日期的舊紀錄 (覆蓋)
    clean_df = old_df[old_df["date"] != today_str] if not old_df.empty else old_df
    combined_df = today_kbars.copy() if clean_df.empty else pd.concat([clean_df, today_kbars], ignore_index=True)
    combined_df["ts"] = pd.to_datetime(combined_df["timestamp"])
    combined_df = combined_df.sort_values("ts").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    combined_df = combined_df.drop(columns=["ts"], errors="ignore")

    all_dates = sorted(combined_df["date"].unique())
    if len(all_dates) > 20:
        dropped_dates = all_dates[:-20]
        kept_dates = all_dates[-20:]
        print(f"✂️ 執行滾動更新：自動剔除最舊 {len(dropped_dates)} 個交易日 {dropped_dates}，保留最新 20 個交易日！")
        combined_df = combined_df[combined_df["date"].isin(kept_dates)].reset_index(drop=True)
    else:
        kept_dates = all_dates

    # 原子化儲存回滾動 20 日 CSV，並同步將所有 1分K 累積保存至永久歷史檔 history_1m_kbars.csv
    atomic_write_csv(combined_df, cache_path, index=False)
    print(f"💾 已成功覆蓋儲存至: {cache_path}")
    print(f"📅 最新 20 日區間: {kept_dates[0]} ~ {kept_dates[-1]} (共 {len(kept_dates)} 天，總計 {len(combined_df)} 根 K 棒)")

    hist_df = archive_1m_kbars(history_path, old_df, today_kbars, today_str)
    print(f"🗄️ 永久 1分K 歷史庫已更新: {history_path} (累計 {hist_df['date'].nunique()} 天 / {len(hist_df)} 根)")

    # 若今日夜盤彙整表遺失或不完整，順便於盤後補齊
    backfill_overnight_level(data_dir, today_str)

    # 3b. 同步更新永久日 K 歷史檔 (daily_bars.csv)，並預告下一交易日的 ATR 動態參數
    daily_hist = update_daily_history(data_dir, today_kbars)
    base_cfg = ORBStrategyConfig()
    next_cfg, next_info = config_for_date(base_cfg, data_dir, "9999-12-31", daily=daily_hist)
    print(f"📐 永久日 K 歷史已更新 ({len(daily_hist)} 日) | 下一交易日 {describe(next_cfg, next_info)}")

    # 4. 自動重新執行 20 日回測並產出績效圖表
    print("\n📊 正在自動重新執行最新 20 日回測並產出報表圖表...")
    backtest_script = os.path.join(base_dir, "backtester", "run_20days_backtest.py")
    try:
        subprocess.run([sys.executable, "-B", backtest_script], check=True)
    except Exception as e:
        print(f"⚠️ 重新執行回測時發生錯誤: {e}")

    # 5. 自動 Commit & Push 到 GitHub (僅推送數據與報表目錄，不誤推本機未完成程式碼)
    if push_git:
        push_script = os.path.join(base_dir, "push.sh")
        if os.path.exists(push_script):
            commit_msg = f"data: Roll 20-day dataset to {today_str} ({kept_dates[0]} ~ {kept_dates[-1]})"
            print(f"\n🚀 正在自動推送最新資料與圖表至 GitHub: {commit_msg} ...")
            subprocess.run(["bash", push_script, "--data-only", commit_msg], check=False)

    print("\n🎉 滾動 20 日更新與回測流程全部順利完成！")
    return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TMF 盤後滾動更新 20/60 日資料庫與回測")
    parser.add_argument("date", nargs="?", default=None, help="指定更新日期 YYYY-MM-DD (預設今日)")
    parser.add_argument("--no-push", action="store_true", help="僅本地更新與重跑回測，不執行 push.sh 推送")
    args = parser.parse_args()
    ok = roll_20days(args.date, push_git=not args.no_push)
    sys.exit(0 if ok else 1)
