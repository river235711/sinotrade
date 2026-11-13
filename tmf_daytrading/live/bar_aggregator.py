# -*- coding: utf-8 -*-
"""
K 棒合成器模組 (Bar Aggregator)
職責：
1. 接收即時逐筆 Tick 串流 (on_tick)，零延遲在本地記憶體累加合成 1 分 K 棒 (OHLCV)。
2. 在行情稀疏或缺少 Tick 時，融合 Snapshot 快照報價補齊最新價位。
3. 支援載入早盤歷史 K 棒或外部備援 K 棒資料。
4. 輸出乾淨、按時間排序並過濾盤中時段 (08:45 ~ 13:45) 的 1 分 K 線 DataFrame。
"""

import time
import datetime
import logging
import threading
from typing import Dict, Any, Optional
import pandas as pd

logger = logging.getLogger("TMFTrader.BarAggregator")


class BarAggregator:
    """
    即時 1 分 K 棒合成與管理器 (具備執行緒安全鎖保護)
    """
    def __init__(self, kbars_disabled: bool = False):
        self._lock = threading.RLock()
        self._bars: Dict[pd.Timestamp, Dict[str, Any]] = {}
        self.history_loaded: bool = False
        self.kbars_disabled: bool = bool(kbars_disabled)
        self.backup_source: Optional[str] = None
        self.last_tick_time: float = 0.0
        self.last_snap_time: float = 0.0
        self.version: int = 0
        self._cached_df: Optional[pd.DataFrame] = None
        self._cached_key: Optional[tuple] = None

    def _bump_version(self) -> None:
        self.version += 1
        self._cached_df = None

    def update_tick(self, price: float, volume: int, dt: datetime.datetime) -> Optional[pd.Timestamp]:
        """
        接收逐筆 Tick 報價並即時聚合至當分鐘 K 棒
        :param price: 最新成交價
        :param volume: 單筆成交量
        :param dt: Tick 時間戳
        :return: 當分鐘對齊時間戳 (bar_ts)
        """
        if price <= 0:
            return None

        bar_ts = pd.Timestamp(dt.replace(second=0, microsecond=0))
        with self._lock:
            self.last_tick_time = time.time()
            b = self._bars.get(bar_ts)

            if b is None:
                self._bars[bar_ts] = {
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": int(volume),
                }
            else:
                if price > b["high"]:
                    b["high"] = price
                if price < b["low"]:
                    b["low"] = price
                b["close"] = price
                b["volume"] += int(volume)

            self._bump_version()
        return bar_ts

    def update_snapshot(self, price: float, volume: int = 1, dt: Optional[datetime.datetime] = None) -> Optional[pd.Timestamp]:
        """
        使用 Snapshot 快照報價更新當前分鐘 K 棒 (用於無 Tick 時之保活與價格同步)
        :param price: 快照成交價
        :param volume: 成交總量或估計量
        :param dt: 時間 (若無則取當前系統時間)
        :return: 當分鐘時間戳
        """
        if price <= 0:
            return None

        now_dt = dt or datetime.datetime.now()
        now_ts = pd.Timestamp(now_dt.replace(second=0, microsecond=0))

        with self._lock:
            self.last_snap_time = time.time()
            b = self._bars.get(now_ts)
            if b is None:
                self._bars[now_ts] = {
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": int(volume or 1),
                }
                self._bump_version()
            else:
                changed = False
                if price > b["high"]:
                    b["high"] = price
                    changed = True
                if price < b["low"]:
                    b["low"] = price
                    changed = True
                if b["close"] != price:
                    b["close"] = price
                    changed = True
                if changed:
                    self._bump_version()

        return now_ts

    def load_history_kbars(self, df: pd.DataFrame, today_str: Optional[str] = None) -> int:
        """
        載入券商歷史 1 分 K 棒至快取中
        :param df: 包含 open, high, low, close, volume 且以 ts 為索引的 DataFrame
        :param today_str: 指定日期字串 (YYYY-MM-DD)，若提供則自動篩選 08:45 ~ 13:45
        :return: 新載入的 K 棒數量
        """
        if df.empty:
            return 0

        target_df = df
        if today_str:
            target_df = target_df[target_df.index.strftime("%Y-%m-%d") == today_str].between_time("08:45", "13:45")

        loaded_count = 0
        with self._lock:
            for ts, row in target_df.iterrows():
                if ts not in self._bars:
                    self._bars[ts] = {
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row["volume"]),
                    }
                    loaded_count += 1

            if len(target_df) > 0:
                self.history_loaded = True
            if loaded_count > 0:
                self._bump_version()

        return loaded_count

    def load_backup_bars(self, backup_df: pd.DataFrame, now_min: Optional[pd.Timestamp] = None, source_tag: str = "TXF_BACKUP") -> int:
        """
        載入外部備援 1 分 K 棒 (高速補齊今日開盤歷史)
        :param backup_df: 外部備援 DataFrame (ts 索引)
        :param now_min: 當前分鐘時間戳 (避免覆蓋當前分鐘即時 Tick)
        :param source_tag: 備援來源標記 (供區隔是否混用大台量能)
        :return: 處理的 K 棒數量
        """
        if backup_df.empty:
            return 0

        limit_min = now_min or pd.Timestamp(datetime.datetime.now().replace(second=0, microsecond=0))
        count = 0
        with self._lock:
            for ts, row in backup_df.iterrows():
                if ts not in self._bars or ts < limit_min:
                    cur_vol = self._bars.get(ts, {}).get("volume", 0)
                    self._bars[ts] = {
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": max(int(row["volume"]), cur_vol),
                    }
                    count += 1

            self.history_loaded = True
            self.backup_source = source_tag
            if count > 0:
                self._bump_version()
        return count

    def needs_snapshot_sync(self, now_ts: pd.Timestamp, tick_timeout_sec: float = 3.0, snap_interval_sec: float = 2.0) -> bool:
        """
        判斷是否需要呼叫 Snapshot 進行報價同步補齊
        :param now_ts: 當前對齊分鐘之 Timestamp
        :param tick_timeout_sec: 超過此秒數無 Tick 則考慮補 Snapshot (預設 3 秒)
        :param snap_interval_sec: Snapshot 最小間隔秒數 (預設 2 秒，防券商限流)
        """
        now = time.time()
        with self._lock:
            no_tick = (now - self.last_tick_time >= tick_timeout_sec) or (now_ts not in self._bars)
            interval_ok = (now - self.last_snap_time >= snap_interval_sec)
            return no_tick and interval_ok

    def to_dataframe(self, today_str: Optional[str] = None, start_time: str = "08:45", end_time: str = "13:45") -> pd.DataFrame:
        """
        將目前聚合的記憶體 K 棒導出為標準格式 DataFrame (含版號快取與時間戳直接過濾優化)
        :param today_str: 限制之日期字串 (YYYY-MM-DD)，若為 None 則不限制日期
        :param start_time: 起始時段 (預設 08:45)
        :param end_time: 結束時段 (預設 13:45)
        :return: 以 ts 為索引、包含 open, high, low, close, volume 的 DataFrame
        """
        with self._lock:
            if not self._bars:
                return pd.DataFrame()

            cache_key = (self.version, len(self._bars), today_str, start_time, end_time)
            if self.version > 0 and self._cached_df is not None and self._cached_key == cache_key:
                return self._cached_df

            if today_str and start_time and end_time:
                t0 = pd.Timestamp(f"{today_str} {start_time}:00")
                t1 = pd.Timestamp(f"{today_str} {end_time}:59")
                filtered = {ts: v for ts, v in self._bars.items() if t0 <= ts <= t1}
                if not filtered:
                    out_df = pd.DataFrame()
                else:
                    out_df = pd.DataFrame.from_dict(filtered, orient="index")
                    out_df.index.name = "ts"
                    out_df = out_df.sort_index()
            else:
                out_df = pd.DataFrame.from_dict(self._bars, orient="index")
                out_df.index.name = "ts"
                out_df = out_df.sort_index()
                if today_str:
                    day_mask = (out_df.index.strftime("%Y-%m-%d") == today_str)
                    out_df = out_df[day_mask]
                if start_time and end_time and not out_df.empty:
                    out_df = out_df.between_time(start_time, end_time)

            if self.version > 0:
                self._cached_key = cache_key
                self._cached_df = out_df
            return out_df

    def has_bar(self, ts: pd.Timestamp) -> bool:
        """檢查是否已存在該分鐘之 K 棒"""
        with self._lock:
            return ts in self._bars

    def clear(self) -> None:
        """換日或重置時清空記憶體 K 棒快取"""
        with self._lock:
            self._bars.clear()
            self.history_loaded = False
            self.backup_source = None
            self.last_tick_time = 0.0
            self.last_snap_time = 0.0
            self._bump_version()

    def __len__(self) -> int:
        with self._lock:
            return len(self._bars)
