# -*- coding: utf-8 -*-
"""
整合測試：ShioajiTMFTrader 重構相容性驗證
驗證：
1. Trader 實例正常初始化
2. bar_aggregator 與 state_manager 正確掛載
3. 向後相容屬性 (_realtime_bars, _history_loaded, _last_tick_time 等) 雙向連動無礙
4. _save_state 與 _load_state 正常運作
"""

import sys
import os
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.shioaji_trader import ShioajiTMFTrader
from live.bar_aggregator import BarAggregator
from live.state_manager import StateManager
from strategies.orb_strategy import ORBStrategy
from config import ORBStrategyConfig, InstrumentConfig


class TestTraderRefactorIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.trader = ShioajiTMFTrader(simulation=True)
        self.trader.state_manager = StateManager(log_dir=self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_components_instantiation(self):
        """驗證新模組正確掛載"""
        self.assertIsInstance(self.trader.bar_aggregator, BarAggregator)
        self.assertIsInstance(self.trader.state_manager, StateManager)

    def test_backward_compatibility_properties(self):
        """驗證向後相容屬性雙向連動"""
        # _realtime_bars
        self.assertIs(self.trader._realtime_bars, self.trader.bar_aggregator._bars)

        # _history_loaded
        self.assertFalse(self.trader._history_loaded)
        self.trader._history_loaded = True
        self.assertTrue(self.trader.bar_aggregator.history_loaded)

        # _last_tick_time
        self.assertEqual(self.trader._last_tick_time, 0.0)
        self.trader._last_tick_time = 12345.6
        self.assertEqual(self.trader.bar_aggregator.last_tick_time, 12345.6)

    def test_state_methods(self):
        """驗證 _save_state 與 _load_state 代理正常"""
        strat = ORBStrategy(config=ORBStrategyConfig(), inst_config=InstrumentConfig())
        strat.position = 1
        strat.entry_price = 46000.0
        
        self.trader._save_state(strat)
        loaded = self.trader._load_state()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("entry_price"), 46000.0)

    def test_speed_and_stability_optimizations(self):
        """驗證 150ms 成交確認、DataFrame 版號快取、秒停喚醒與盤後旗標持久化"""
        import datetime
        # 1. 150ms IOC 成交確認輪詢
        self.assertEqual(self.trader.fill_poll_interval, 0.15)

        # 2. BarAggregator 版號快取與時間戳過濾
        agg = self.trader.bar_aggregator
        self.assertEqual(agg.version, 0)
        dt = datetime.datetime(2026, 11, 11, 9, 5, 12)
        agg.update_tick(45100.0, 3, dt)
        self.assertEqual(agg.version, 1)
        df1 = agg.to_dataframe(today_str="2026-11-11")
        df2 = agg.to_dataframe(today_str="2026-11-11")
        self.assertIs(df1, df2)  # 未變動時直接命中快取，不重建 DataFrame

        agg.update_tick(45120.0, 2, dt)
        self.assertEqual(agg.version, 2)
        df3 = agg.to_dataframe(today_str="2026-11-11")
        self.assertIsNot(df1, df3)
        self.assertEqual(float(df3.iloc[-1]["high"]), 45120.0)

        # 3. 盤後 EOD 通知與結算旗標持久化
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        strat = ORBStrategy(config=ORBStrategyConfig(), inst_config=InstrumentConfig())
        self.trader._eod_close_notified = True
        self.trader._eod_done_date = today_str
        self.trader._save_state(strat)
        loaded = self.trader._load_state()
        self.assertTrue(loaded.get("eod_close_notified"))
        self.assertTrue(loaded.get("eod_done"))

        # 4. request_stop 毫秒級喚醒
        self.trader._wake_event.clear()
        self.assertFalse(self.trader._stop_requested)
        self.trader.request_stop()
        self.assertTrue(self.trader._stop_requested)
        self.assertTrue(self.trader._wake_event.is_set())

    def test_cross_platform_detect_running_mode(self):
        """驗證 main._detect_running_mode 使用 ps -eo 跨平台偵測 (macOS / Linux / WSL)"""
        from types import SimpleNamespace
        from unittest.mock import patch
        import main as entry_main

        fake_ps_out = (
            f"  {os.getpid()} python3 -B main.py --cmd close\n"
            "  45123 /Users/demo/venv/bin/python -B main.py --mode live --qty 1\n"
        )
        with patch("main.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=fake_ps_out)):
            self.assertEqual(entry_main._detect_running_mode(), "live")

        fake_ps_eq = "  8888 /opt/homebrew/bin/python3 /app/main.py --mode=paper\n"
        with patch("main.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=fake_ps_eq)):
            self.assertEqual(entry_main._detect_running_mode(), "paper")

        fake_ps_cmd_only = "  9999 python3 main.py --mode live --cmd close\n"
        with patch("main.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=fake_ps_cmd_only)), \
             patch("main.os.path.isdir", return_value=False):
            self.assertIsNone(entry_main._detect_running_mode())


if __name__ == "__main__":
    unittest.main()
