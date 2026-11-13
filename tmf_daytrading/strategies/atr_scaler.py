"""
ATR 動態尺度調整模組 (ATR Volatility Scaling)

目的：
    固定點數的停損 / 鎖利 (例如 -90 點) 只適用於某一種波動環境。
    本模組依「前 N 個交易日的日盤平均真實振幅 (ATR)」自動放大或縮小所有點數參數：
        今日比例 = 今日 ATR / 基準 ATR (atr_reference)
        今日參數 = 固定參數 × 今日比例 (並限制在 atr_scale_min ~ atr_scale_max 倍之間)
    當 ATR 等於基準值時，參數與原本的固定點數完全相同。

重要：ATR 只使用「今天以前」的日 K 計算，盤中重啟也會得到相同參數，不會偷看未來資料。
"""
import os
import dataclasses
from typing import Optional, Tuple, Dict

import pandas as pd

from config import atomic_write_csv

# 依 ATR 比例縮放的點數參數
SCALED_FIELDS = (
    "stop_loss_pts",
    "breakeven_trigger_pts",
    "breakeven_lock_pts",
    "tier1_5_trigger_pts",
    "tier1_5_lock_pts",
    "tier2_trigger_pts",
    "tier2_lock_pts",
    "trailing_activation_pts",
    "trailing_distance_pts",
    "early_super_breakout_pts",
    "max_breakout_chase_pts",
    "max_orb_range_pts",
)

DAILY_HISTORY_FILE = "daily_bars.csv"     # 永久累積的日盤日 K (不滾動刪除，供 ATR 計算)
ROLLING_KBARS_FILE = "tmf_20days_1m_kbars.csv"


def build_daily_bars(df_1m: pd.DataFrame) -> pd.DataFrame:
    """把 1分K (需含 timestamp/open/high/low/close 或 DatetimeIndex) 合成日盤 (08:45~13:45) 日 K"""
    df = df_1m.copy()
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    df = df.sort_index().between_time("08:45", "13:45")
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    g = df.groupby(df.index.strftime("%Y-%m-%d"))
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
    })
    daily.index.name = "date"
    return daily


def load_daily_history(data_dir: str) -> pd.DataFrame:
    """讀取永久日 K 歷史，並與滾動 20 日 1分K 資料合併 (同日期以 1分K 合成結果為準)"""
    frames = []
    hist_path = os.path.join(data_dir, DAILY_HISTORY_FILE)
    if os.path.exists(hist_path):
        hist = pd.read_csv(hist_path, index_col="date")
        frames.append(hist[["open", "high", "low", "close"]])
    roll_path = os.path.join(data_dir, ROLLING_KBARS_FILE)
    if os.path.exists(roll_path):
        frames.append(build_daily_bars(pd.read_csv(roll_path)))
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    daily = pd.concat(frames)
    daily = daily[~daily.index.duplicated(keep="last")].sort_index()
    return daily


def save_daily_history(data_dir: str, daily: pd.DataFrame):
    """原子覆寫永久日 K 歷史檔"""
    path = os.path.join(data_dir, DAILY_HISTORY_FILE)
    out = daily[["open", "high", "low", "close"]].sort_index().round(1)
    out.index.name = "date"
    atomic_write_csv(out, path)


def update_daily_history(data_dir: str, df_1m_new: pd.DataFrame) -> pd.DataFrame:
    """把新的 1分K 合成日 K 併入永久歷史 (每日收盤後呼叫)"""
    daily = load_daily_history(data_dir)
    new_daily = build_daily_bars(df_1m_new)
    daily = pd.concat([daily, new_daily])
    daily = daily[~daily.index.duplicated(keep="last")].sort_index()
    save_daily_history(data_dir, daily)
    return daily


def compute_atr(daily: pd.DataFrame, before_date: str, period: int = 20, min_days: int = 5) -> Tuple[Optional[float], int]:
    """
    計算 before_date (不含當日) 之前最近 period 個交易日的平均真實振幅 (簡單平均)。
    真實振幅 TR = max(高-低, |高-前日收|, |低-前日收|)，前日收為前一日日盤收盤。
    回傳 (ATR, 實際使用天數)；歷史不足 min_days 天時回傳 (None, 天數)。
    """
    prior = daily[daily.index < before_date].sort_index()
    if prior.empty:
        return None, 0
    prev_close = prior["close"].shift(1)
    hl = prior["high"] - prior["low"]
    hc = (prior["high"] - prev_close).abs()
    lc = (prior["low"] - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)   # 第一天沒有前收，只用高低差
    tr = tr.tail(period)
    if len(tr) < min_days:
        return None, len(tr)
    return float(tr.mean()), len(tr)


def scale_strategy_config(base_cfg, atr: Optional[float], ref_atr: Optional[float] = None) -> Tuple[object, Dict]:
    """
    依 ATR 縮放策略點數參數，回傳 (新設定, 資訊 dict)。
    若提供 ref_atr (例如前 60 日動態基準 ATR)，則以 ref_atr 為分母；否則退回 base_cfg.atr_reference。
    ATR 為 None (歷史不足) 或未啟用時，回傳原固定參數。
    """
    info = {"atr": atr, "ref_atr": ref_atr, "ratio": 1.0, "applied": False}
    if not getattr(base_cfg, "use_atr_scaling", False) or atr is None or atr <= 0:
        return base_cfg, info

    ref = float(ref_atr) if (ref_atr is not None and ref_atr > 0) else float(base_cfg.atr_reference)
    raw_ratio = atr / ref
    ratio = min(max(raw_ratio, base_cfg.atr_scale_min), base_cfg.atr_scale_max)
    changes = {"atr_reference": round(ref, 1)}
    for f in SCALED_FIELDS:
        base_val = getattr(base_cfg, f)
        scaled_val = float(max(1.0, round(base_val * ratio)))
        if f == "stop_loss_pts":
            # 停損保底原始基準 (90點)：高波動 (ratio > 1) 放大停損保護，低波動 (ratio < 1) 不縮小停損，避免正常均線回測被洗出
            scaled_val = max(float(base_val), scaled_val)
        changes[f] = scaled_val
    new_cfg = dataclasses.replace(base_cfg, **changes)
    info.update({"ref_atr": ref, "ratio": ratio, "raw_ratio": raw_ratio, "applied": True})
    return new_cfg, info


def config_for_date(base_cfg, data_dir: str, target_date: str, daily: pd.DataFrame = None) -> Tuple[object, Dict]:
    """便利函式：讀取歷史、計算 target_date 的 20 日 ATR 與 60 日動態基準 ATR，並回傳縮放後設定"""
    if daily is None:
        daily = load_daily_history(data_dir)
    atr, n = compute_atr(daily, target_date, base_cfg.atr_period, base_cfg.atr_min_days)
    ref_period = getattr(base_cfg, "atr_ref_period", 60)
    ref_atr, ref_n = compute_atr(daily, target_date, ref_period, base_cfg.atr_min_days)
    cfg, info = scale_strategy_config(base_cfg, atr, ref_atr=ref_atr)
    info["atr_days"] = n
    info["ref_days"] = ref_n
    return cfg, info


def describe(cfg, info: Dict) -> str:
    """產生一行參數說明文字 (供日誌使用)"""
    if info.get("applied"):
        ref_d = info.get("ref_days")
        ref_tag = f"{ref_d}日基準" if ref_d else "基準"
        head = (f"ATR({info.get('atr_days', '?')}日)={info['atr']:.0f}點 "
                f"({ref_tag} {cfg.atr_reference:.0f}，比例 {info['ratio']:.2f}x)")
    elif info.get("atr") is None and getattr(cfg, "use_atr_scaling", False):
        head = f"ATR 歷史不足 ({info.get('atr_days', 0)} 日 < {cfg.atr_min_days} 日)，使用固定參數"
    else:
        head = "ATR 縮放未啟用，使用固定參數"
    return (f"{head} → 停損 -{cfg.stop_loss_pts:.0f} | 一階 +{cfg.breakeven_trigger_pts:.0f}→+{cfg.breakeven_lock_pts:.0f} | "
            f"中階 +{cfg.tier1_5_trigger_pts:.0f}→+{cfg.tier1_5_lock_pts:.0f} | "
            f"二階 +{cfg.tier2_trigger_pts:.0f}→+{cfg.tier2_lock_pts:.0f} | "
            f"移動停利 +{cfg.trailing_activation_pts:.0f}(回撤{cfg.trailing_distance_pts:.0f}) | "
            f"ORB上限 {cfg.max_orb_range_pts:.0f}")
