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


if __name__ == "__main__":
    unittest.main()
