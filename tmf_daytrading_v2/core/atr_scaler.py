"""
ATR 動態尺度調整純領域核心 (ATR Volatility Scaling)
只處理 DataFrame 與參數計算，完全不含任何磁碟或網路 I/O。
"""
import dataclasses
from typing import Dict, Optional, Tuple

import pandas as pd

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


def merge_daily_history(*frames: Optional[pd.DataFrame]) -> pd.DataFrame:
    """依序合併多個日 K DataFrame (同日期以後者為準並依日期遞增排序)"""
    valid = [f[["open", "high", "low", "close"]] for f in frames if f is not None and not f.empty]
    if not valid:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    daily = pd.concat(valid)
    daily = daily[~daily.index.duplicated(keep="last")].sort_index()
    daily.index.name = "date"
    return daily


def compute_atr(
    daily: pd.DataFrame, before_date: str, period: int = 20, min_days: int = 5
) -> Tuple[Optional[float], int]:
    """
    計算 before_date (不含當日) 之前最近 period 個交易日的平均真實振幅 (簡單平均)。
    真實振幅 TR = max(高-低, |高-前日收|, |低-前日收|)。
    歷史不足 min_days 天時回傳 (None, 實際天數)。
    """
    if daily is None or daily.empty:
        return None, 0
    prior = daily[daily.index < before_date].sort_index()
    if prior.empty:
        return None, 0
    prev_close = prior["close"].shift(1)
    hl = prior["high"] - prior["low"]
    hc = (prior["high"] - prev_close).abs()
    lc = (prior["low"] - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    tr = tr.tail(period)
    if len(tr) < min_days:
        return None, len(tr)
    return float(tr.mean()), len(tr)


def scale_strategy_config(
    base_cfg, atr: Optional[float], ref_atr: Optional[float] = None
) -> Tuple[object, Dict]:
    """
    依 ATR 縮放策略點數參數，回傳 (新設定, 資訊 dict)。
    停損點數保底原始基準，避免極低波動時停損過窄被雜訊洗出。
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
            scaled_val = max(float(base_val), scaled_val)
        changes[f] = scaled_val
    new_cfg = dataclasses.replace(base_cfg, **changes)
    info.update({"ref_atr": ref, "ratio": ratio, "raw_ratio": raw_ratio, "applied": True})
    return new_cfg, info


def scale_config_from_daily(
    base_cfg, daily: pd.DataFrame, target_date: str
) -> Tuple[object, Dict]:
    """純函式版：由記憶體內的日 K DataFrame 計算 20 日 ATR 與 60 日動態基準 ATR 並縮放設定"""
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
        head = (
            f"ATR({info.get('atr_days', '?')}日)={info['atr']:.0f}點 "
            f"({ref_tag} {cfg.atr_reference:.0f}，比例 {info['ratio']:.2f}x)"
        )
    elif info.get("atr") is None and getattr(cfg, "use_atr_scaling", False):
        head = f"ATR 歷史不足 ({info.get('atr_days', 0)} 日 < {cfg.atr_min_days} 日)，使用固定參數"
    else:
        head = "ATR 縮放未啟用，使用固定參數"
    return (
        f"{head} → 停損 -{cfg.stop_loss_pts:.0f} | 一階 +{cfg.breakeven_trigger_pts:.0f}→+{cfg.breakeven_lock_pts:.0f} | "
        f"中階 +{cfg.tier1_5_trigger_pts:.0f}→+{cfg.tier1_5_lock_pts:.0f} | "
        f"二階 +{cfg.tier2_trigger_pts:.0f}→+{cfg.tier2_lock_pts:.0f} | "
        f"移動停利 +{cfg.trailing_activation_pts:.0f}(回撤{cfg.trailing_distance_pts:.0f}) | "
        f"ORB上限 {cfg.max_orb_range_pts:.0f}"
    )
