# -*- coding: utf-8 -*-
"""
單元測試：BarAggregator (K 棒合成器)
驗證：
1. 即時 Tick 聚合為 1 分 K 棒 (OHLCV 計算精確性)
2. 跨分鐘切換時自動建立新 K 棒
3. 快照 (Snapshot) 補價邏輯
4. 歷史 K 棒載入與過濾
5. 輸出 DataFrame 格式與時間索引正確性
"""

import sys
import os
import datetime
import unittest
import pandas as pd

# 加入根目錄
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.bar_aggregator import BarAggregator


class TestBarAggregator(unittest.TestCase):
    def setUp(self):
        self.agg = BarAggregator()

    def test_single_bar_tick_aggregation(self):
        """測試同一分鐘內多筆 Tick 聚合為單一 K 棒"""
        dt_base = datetime.datetime(2026, 11, 6, 9, 0, 0)

        # 第 1 筆: 09:00:05 @ 20000, vol=2 (開盤價)
        self.agg.update_tick(20000.0, 2, dt_base + datetime.timedelta(seconds=5))
        # 第 2 筆: 09:00:20 @ 20050, vol=5 (新最高價)
        self.agg.update_tick(20050.0, 5, dt_base + datetime.timedelta(seconds=20))
        # 第 3 筆: 09:00:35 @ 19980, vol=3 (新最低價)
        self.agg.update_tick(19980.0, 3, dt_base + datetime.timedelta(seconds=35))
        # 第 4 筆: 09:00:58 @ 20010, vol=1 (收盤價)
        self.agg.update_tick(20010.0, 1, dt_base + datetime.timedelta(seconds=58))

        self.assertEqual(len(self.agg), 1)
        ts = pd.Timestamp(dt_base)
        b = self.agg._bars[ts]

        self.assertEqual(b["open"], 20000.0)
        self.assertEqual(b["high"], 20050.0)
        self.assertEqual(b["low"], 19980.0)
        self.assertEqual(b["close"], 20010.0)
        self.assertEqual(b["volume"], 11)  # 2 + 5 + 3 + 1

    def test_multi_bar_transition(self):
        """測試跨分鐘自動建立新 K 棒"""
        dt1 = datetime.datetime(2026, 11, 6, 9, 0, 10)
        dt2 = datetime.datetime(2026, 11, 6, 9, 1, 15)

        self.agg.update_tick(20000.0, 1, dt1)
        self.agg.update_tick(20020.0, 2, dt2)

        self.assertEqual(len(self.agg), 2)
        df = self.agg.to_dataframe(today_str="2026-11-06")
        self.assertEqual(len(df), 2)
        self.assertListEqual(list(df["close"]), [20000.0, 20020.0])

    def test_snapshot_update(self):
        """測試快照更新無 Tick 之分鐘"""
        dt = datetime.datetime(2026, 11, 6, 9, 5, 10)
        ts = pd.Timestamp(datetime.datetime(2026, 11, 6, 9, 5, 0))

        # 初始快照
        self.agg.update_snapshot(20100.0, volume=5, dt=dt)
        self.assertTrue(self.agg.has_bar(ts))
        b = self.agg._bars[ts]
        self.assertEqual(b["open"], 20100.0)
        self.assertEqual(b["close"], 20100.0)

        # 再次快照更高價格
        self.agg.update_snapshot(20150.0, volume=10, dt=dt)
        b = self.agg._bars[ts]
        self.assertEqual(b["high"], 20150.0)
        self.assertEqual(b["close"], 20150.0)

    def test_load_history_kbars(self):
        """測試載入早盤歷史 K 棒"""
        timestamps = [
            pd.Timestamp("2026-11-06 08:45:00"),
            pd.Timestamp("2026-11-06 08:46:00"),
            pd.Timestamp("2026-11-06 08:47:00"),
        ]
        mock_df = pd.DataFrame({
            "open": [20000.0, 20010.0, 20020.0],
            "high": [20010.0, 20025.0, 20030.0],
            "low": [19990.0, 20005.0, 20015.0],
            "close": [20005.0, 20020.0, 20025.0],
            "volume": [10, 20, 30],
        }, index=pd.Index(timestamps, name="ts"))

        loaded = self.agg.load_history_kbars(mock_df, today_str="2026-11-06")
        self.assertEqual(loaded, 3)
        self.assertTrue(self.agg.history_loaded)
        self.assertEqual(len(self.agg), 3)

        # 驗證 to_dataframe 輸出正確
        df = self.agg.to_dataframe(today_str="2026-11-06")
        self.assertEqual(len(df), 3)
        self.assertEqual(df.index.name, "ts")

    def test_clear_reset(self):
        """測試換日清空重置"""
        self.agg.update_tick(20000.0, 1, datetime.datetime(2026, 11, 6, 9, 0, 0))
        self.agg.history_loaded = True
        self.assertEqual(len(self.agg), 1)

        self.agg.clear()
        self.assertEqual(len(self.agg), 0)
        self.assertFalse(self.agg.history_loaded)


if __name__ == "__main__":
    unittest.main()
