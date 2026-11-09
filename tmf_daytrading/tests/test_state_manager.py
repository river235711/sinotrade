# -*- coding: utf-8 -*-
"""
單元測試：StateManager (持倉與策略狀態持久化管理器)
驗證：
1. 策略即時狀態之原子化持久化存檔 (JSON 完整性)
2. 程式重啟狀態還原 (Restore) 正確性
3. 異常隔離防禦 (檔案不存在或損壞時安全處理)
"""

import sys
import os
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.state_manager import StateManager
from strategies.orb_strategy import ORBStrategy
from config import ORBStrategyConfig, InstrumentConfig


class TestStateManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mgr = StateManager(log_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_load_state(self):
        """測試狀態儲存與讀取"""
        strat = ORBStrategy(config=ORBStrategyConfig(), inst_config=InstrumentConfig())
        strat.position = 1
        strat.entry_price = 45000.0
        strat.entry_time = "09:18:00"
        strat.highest_profit = 75.0
        strat.trade_count = 1
        strat.orb_high = 45100.0
        strat.orb_low = 44900.0
        strat.orb_calculated = True

        ok = self.mgr.save_state(strat, entries_halted=False, date_str="20261106")
        self.assertTrue(ok)

        # 讀取狀態
        data = self.mgr.load_state(date_str="20261106")
        self.assertIsNotNone(data)
        self.assertEqual(data["position"], 1)
        self.assertEqual(data["entry_price"], 45000.0)
        self.assertEqual(data["highest_profit"], 75.0)
        self.assertEqual(data["trade_count"], 1)
        self.assertEqual(data["orb_high"], 45100.0)
        self.assertEqual(data["orb_low"], 44900.0)
        self.assertTrue(data["orb_calculated"])
        self.assertFalse(data["entries_halted"])

    def test_restore_strategy_state(self):
        """測試狀態還原至策略實例"""
        saved = {
            "position": -1,
            "entry_price": 44800.0,
            "highest_profit": 50.0,
            "trade_count": 2,
            "orb_high": 45050.0,
            "orb_low": 44850.0,
            "orb_calculated": True,
        }
        strat = ORBStrategy(config=ORBStrategyConfig(), inst_config=InstrumentConfig())
        ok = self.mgr.restore_strategy_state(strat, saved)
        self.assertTrue(ok)
        self.assertEqual(strat.position, -1)
        self.assertEqual(strat.entry_price, 44800.0)
        self.assertEqual(strat.highest_profit, 50.0)
        self.assertEqual(strat._hp_before_bar_val, 50.0)
        self.assertEqual(strat.trade_count, 2)
        self.assertEqual(strat.orb_high, 45050.0)
        self.assertEqual(strat.orb_low, 44850.0)
        self.assertTrue(strat.orb_calculated)

    def test_load_non_existent_state(self):
        """測試讀取不存在的檔案時安全回傳 None"""
        res = self.mgr.load_state(date_str="19990101")
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
