"""
純技術指標計算核心 (O(1) 線上指標累加器 + 批次向量化計算 + ORB 區間計算)
數學定義與 pandas ewm(span=9/21, adjust=False) 及累計 VWAP 100% 等價。
"""
from typing import Optional, Tuple

import pandas as pd

from config import ORBStrategyConfig

_ALPHA_EMA9 = 2.0 / (9.0 + 1.0)
_ALPHA_EMA21 = 2.0 / (21.0 + 1.0)


class OnlineIndicators:
    """
    O(1) 線上技術指標計算器。
    - VWAP = sum(close * volume) / sum(volume) (累計量為 0 時退回 close)
    - EMA9 / EMA21 = pandas ewm(span=9/21, adjust=False).mean()
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.pv_sum: float = 0.0
        self.vol_sum: float = 0.0
        self.ema9: Optional[float] = None
        self.ema21: Optional[float] = None
        self.count: int = 0

    def peek(self, close: float, volume: float = 0.0) -> Tuple[float, float, float]:
        """計算包含當前價格的即時指標，但不改變內部累計狀態 (適合盤中未收完的 K 棒)"""
        tot_vol = self.vol_sum + volume
        vwap = ((self.pv_sum + close * volume) / tot_vol) if tot_vol > 0 else close
        if self.count == 0 or self.ema9 is None or self.ema21 is None:
            return vwap, close, close
        e9 = (1.0 - _ALPHA_EMA9) * self.ema9 + _ALPHA_EMA9 * close
        e21 = (1.0 - _ALPHA_EMA21) * self.ema21 + _ALPHA_EMA21 * close
        return vwap, e9, e21

    def update(self, close: float, volume: float = 0.0) -> Tuple[float, float, float]:
        """推進一根已完成的 K 棒並回傳最新 (vwap, ema9, ema21)"""
        self.pv_sum += close * volume
        self.vol_sum += volume
        vwap = (self.pv_sum / self.vol_sum) if self.vol_sum > 0 else close
        if self.count == 0 or self.ema9 is None or self.ema21 is None:
            self.ema9 = close
            self.ema21 = close
        else:
            self.ema9 = (1.0 - _ALPHA_EMA9) * self.ema9 + _ALPHA_EMA9 * close
            self.ema21 = (1.0 - _ALPHA_EMA21) * self.ema21 + _ALPHA_EMA21 * close
        self.count += 1
        return vwap, self.ema9, self.ema21

    @classmethod
    def from_dataframe(cls, kbars: pd.DataFrame) -> "OnlineIndicators":
        """從歷史日內 K 棒 DataFrame 初始化累加器狀態"""
        inst = cls()
        if kbars is None or kbars.empty:
            return inst
        closes = kbars["close"].to_numpy(dtype=float)
        vols = kbars["volume"].to_numpy(dtype=float) if "volume" in kbars.columns else [0.0] * len(closes)
        for c, v in zip(closes, vols):
            inst.update(float(c), float(v))
        return inst


def prepare_batch_indicators(kbars: pd.DataFrame) -> pd.DataFrame:
    """計算整日 DataFrame 的 VWAP 與 EMA9 / EMA21 指標 (累計量為 0 時以收盤價取代 0.0)"""
    df = kbars.copy()
    cum_vol_raw = df["volume"].cumsum()
    raw_vwap = (df["close"] * df["volume"]).cumsum() / cum_vol_raw.replace(0, 1)
    df["vwap"] = raw_vwap.where(cum_vol_raw > 0, df["close"])
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    return df


def calculate_orb_levels_from_df(
    kbars: pd.DataFrame,
    cfg: ORBStrategyConfig,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    計算早盤前導區間 (08:45~09:00) 與現貨開盤區間 (09:00~09:15) 高低點。
    回傳 (pre_orb_high, pre_orb_low, orb_high, orb_low)。
    """
    if kbars is None or kbars.empty:
        return None, None, None, None

    pre_h = pre_l = orb_h = orb_l = None
    pre_bars = kbars.between_time(cfg.pre_orb_start_time[:5], cfg.pre_orb_end_time[:5])
    if len(pre_bars) > 0:
        pre_h = float(pre_bars["high"].max())
        pre_l = float(pre_bars["low"].min())

    orb_bars = kbars.between_time(cfg.orb_start_time[:5], cfg.orb_end_time[:5])
    if len(orb_bars) > 0:
        orb_h = float(orb_bars["high"].max())
        orb_l = float(orb_bars["low"].min())

    return pre_h, pre_l, orb_h, orb_l
