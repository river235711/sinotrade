#!/usr/bin/env python3
"""
盤後自動滾動更新最新 20 個交易日資料與重新回測腳本 (V2)
"""
import datetime
import os
import subprocess
import sys

sys.dont_write_bytecode = True

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd

from config import ORBStrategyConfig, ShioajiConfig, atomic_write_csv
from core.atr_scaler import describe
from gateways.data_repo import (
    config_for_date,
    fetch_cnyes_day_kbars,
    is_overnight_row_complete,
    load_or_fetch_overnight_levels,
    update_daily_history,
)
from gateways.shioaji_live import normalize_shioaji_kbars, resolve_tmf_contract


def fetch_intraday_kbars_shioaji(today_str: str) -> pd.DataFrame:
    api = None
    try:
        import shioaji as sj
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
        return df_day[["timestamp", "open", "high", "low", "close", "volume", "date", "contract"]].reset_index(drop=True)
    except Exception as e:
        print(f"⚠️ 從 Shioaji API 獲取今日 K 棒異常: {e}")
        return pd.DataFrame()
    finally:
        if api is not None:
            try:
                api.logout()
            except Exception:
                pass


def fetch_intraday_kbars_cnyes(today_str: str) -> pd.DataFrame:
    try:
        day_df = fetch_cnyes_day_kbars(today_str, timeout=8)
        if day_df.empty:
            return pd.DataFrame()
        out = day_df.reset_index().copy()
        out["timestamp"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
        out["date"] = today_str
        out["contract"] = "TXF_BKP"
        return out[["timestamp", "open", "high", "low", "close", "volume", "date", "contract"]].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def archive_1m_kbars(history_path: str, seed_df: pd.DataFrame, today_kbars: pd.DataFrame, today_str: str) -> pd.DataFrame:
    hist_base = pd.read_csv(history_path) if os.path.exists(history_path) else seed_df.copy()
    clean_hist = hist_base[hist_base["date"] != today_str] if not hist_base.empty else hist_base
    merged = today_kbars.copy() if clean_hist.empty else pd.concat([clean_hist, today_kbars], ignore_index=True)
    merged["ts"] = pd.to_datetime(merged["timestamp"])
    merged = merged.sort_values("ts").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    merged = merged.drop(columns=["ts"], errors="ignore")
    atomic_write_csv(merged, history_path, index=False)
    return merged


def backfill_overnight_level(data_dir: str, target_date_str: str) -> bool:
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
        cfg = ShioajiConfig()
        api = sj.Shioaji()
        api.login(api_key=cfg.api_key, secret_key=cfg.secret_key)
        contract = resolve_tmf_contract(api, "TMFR1")
        o_h, o_l, _ = load_or_fetch_overnight_levels(
            data_dir, target_date_str, api=api, contract=contract, allow_cnyes_fallback=False
        )
        return o_h is not None and o_l is not None
    except Exception:
        return False
    finally:
        if api is not None:
            try:
                api.logout()
            except Exception:
                pass


def roll_20days(target_date: str = None, push_git: bool = True) -> bool:
    data_dir = os.path.join(BASE_DIR, "data")
    cache_path = os.path.join(data_dir, "tmf_20days_1m_kbars.csv")
    today_str = target_date or datetime.date.today().strftime("%Y-%m-%d")

    today_kbars = fetch_intraday_kbars_shioaji(today_str)
    if today_kbars.empty or len(today_kbars) < 60:
        today_kbars = fetch_intraday_kbars_cnyes(today_str)
    if today_kbars.empty or len(today_kbars) < 60:
        print(f"⚠️ 今日 ({today_str}) K 棒筆數不足 ({len(today_kbars)} 筆)，可能非交易日。")
        return False

    history_path = os.path.join(data_dir, "history_1m_kbars.csv")
    if os.path.exists(cache_path):
        old_df = pd.read_csv(cache_path)
    elif os.path.exists(history_path):
        old_df = pd.read_csv(history_path)
    else:
        old_df = pd.DataFrame(columns=today_kbars.columns)

    clean_df = old_df[old_df["date"] != today_str] if not old_df.empty else old_df
    combined_df = today_kbars.copy() if clean_df.empty else pd.concat([clean_df, today_kbars], ignore_index=True)
    combined_df["ts"] = pd.to_datetime(combined_df["timestamp"])
    combined_df = combined_df.sort_values("ts").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    combined_df = combined_df.drop(columns=["ts"], errors="ignore")

    all_dates = sorted(combined_df["date"].unique())
    kept_dates = all_dates[-20:] if len(all_dates) > 20 else all_dates
    combined_df = combined_df[combined_df["date"].isin(kept_dates)].reset_index(drop=True)

    atomic_write_csv(combined_df, cache_path, index=False)
    archive_1m_kbars(history_path, old_df, today_kbars, today_str)
    backfill_overnight_level(data_dir, today_str)

    daily_hist = update_daily_history(data_dir, today_kbars)
    next_cfg, next_info = config_for_date(ORBStrategyConfig(), data_dir, "9999-12-31", daily=daily_hist)
    print(f"📐 永久日 K 已更新 ({len(daily_hist)} 日) | {describe(next_cfg, next_info)}")

    backtest_script = os.path.join(BASE_DIR, "backtester", "run_20days_backtest.py")
    subprocess.run([sys.executable, "-B", backtest_script], check=True)

    if push_git:
        push_script = os.path.join(BASE_DIR, "push.sh")
        if os.path.exists(push_script):
            commit_msg = f"data(v2): Roll 20-day dataset to {today_str} ({kept_dates[0]} ~ {kept_dates[-1]})"
            subprocess.run(["bash", push_script, "--data-only", commit_msg], check=False)

    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TMF 盤後滾動更新 20/60 日資料庫與回測 (V2)")
    parser.add_argument("date", nargs="?", default=None)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    sys.exit(0 if roll_20days(args.date, push_git=not args.no_push) else 1)
