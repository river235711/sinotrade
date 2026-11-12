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
        self.trader.log_dir = self.temp_dir.name

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

    def test_off_market_sleep_and_wake(self):
        """驗證盤後自動登出休眠、開盤前定時喚醒、非交易時段防打擾與心跳過濾"""
        import datetime
        from unittest.mock import MagicMock, patch

        mock_api = MagicMock()
        mock_contract = MagicMock()
        self.trader.api = mock_api
        self.trader.contract = mock_contract
        self.trader._is_session_active = True

        # 1. 測試 logout_and_sleep
        self.trader.logout_and_sleep(reason="盤後測試休眠")
        mock_api.logout.assert_called_once()
        self.assertIsNone(self.trader.api)
        self.assertIsNone(self.trader.contract)
        self.assertFalse(self.trader._is_session_active)

        # 2. 測試 wake_and_login
        with patch.object(self.trader, "initialize", return_value=True) as mock_init, \
             patch.object(self.trader, "load_tmf_contract", return_value=mock_contract) as mock_load:
            ok = self.trader.wake_and_login(reason="早盤喚醒測試")
            self.assertTrue(ok)
            mock_init.assert_called_once()
            mock_load.assert_called_once_with("TMFR1")
            self.assertTrue(self.trader._is_session_active)

        # 3. 測試非交易時段 reconnect 拒絕重連防打擾
        mock_dt = datetime.datetime(2026, 11, 12, 22, 40, 0)
        with patch("live.shioaji_trader.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            self.trader.pending_flatten = False
            self.trader._active_strategy = ORBStrategy(config=ORBStrategyConfig(), inst_config=InstrumentConfig())
            self.trader._active_strategy.position = 0
            # 應拒絕重連
            reconnected = self.trader.reconnect()
            self.assertFalse(reconnected)

        # 4. 測試非交易時段 keep_alive_heartbeat 拒絕 Ping
        self.trader.simulation = False
        self.trader.api = mock_api
        mock_dt_night = datetime.datetime(2026, 11, 12, 22, 40, 0)
        with patch("live.shioaji_trader.datetime.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt_night
            self.trader.keep_alive_heartbeat()
            mock_api.margin.assert_not_called()

    def test_holiday_and_snapshot_ts_failsafe(self):
        """驗證快照時間戳過濾 (防止休市日/夜盤殘留價格) 與休市日提前睡眠防呆"""
        import datetime
        import pandas as pd
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        today_str = "2026-11-13"
        ns = lambda dt_str: int(pd.Timestamp(dt_str).value)

        # 1. 快照時間戳過濾驗證
        # 前一日收盤快照 -> False
        self.assertFalse(ShioajiTMFTrader._is_today_day_session_snapshot(
            SimpleNamespace(close=43500, volume=1, ts=ns("2026-11-12 13:44:59")), today_str
        ))
        # 今日夜盤 04:59:59 快照 -> False
        self.assertFalse(ShioajiTMFTrader._is_today_day_session_snapshot(
            SimpleNamespace(close=43659, volume=1, ts=ns("2026-11-13 04:59:59")), today_str
        ))
        # 無效 0 時間戳 -> False
        self.assertFalse(ShioajiTMFTrader._is_today_day_session_snapshot(
            SimpleNamespace(close=43659, volume=1, ts=0), today_str
        ))
        # 今日日盤 08:45:05 快照 -> True
        self.assertTrue(ShioajiTMFTrader._is_today_day_session_snapshot(
            SimpleNamespace(close=43700, volume=1, ts=ns("2026-11-13 08:45:05")), today_str
        ))

        # 2. fetch_today_day_kbars 在休市日僅有昨夜盤快照時，不應產生任何假 K 棒
        mock_api = MagicMock()
        mock_api.kbars.return_value = {"ts": [], "Open": [], "High": [], "Low": [], "Close": [], "Volume": []}
        mock_api.snapshots.return_value = [
            SimpleNamespace(close=43659.0, volume=1, ts=ns("2026-11-13 04:59:59"))
        ]
        self.trader.api = mock_api
        self.trader.contract = MagicMock()
        self.trader.bar_aggregator.kbars_disabled = False
        mock_now = datetime.datetime(2026, 11, 13, 9, 10, 0)
        with patch("live.shioaji_trader.datetime.date") as mock_date, \
             patch("live.shioaji_trader.datetime.datetime") as mock_dt:
            mock_date.today.return_value = datetime.date(2026, 11, 13)
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            kbars_df = self.trader.fetch_today_day_kbars()
            self.assertTrue(kbars_df.empty)

        # 3. 休市旗標 _holiday_detected=True 時，即使在平日盤中時段 (10:00)，也不重連與不心跳 Ping
        self.trader.simulation = False
        self.trader._holiday_detected = True
        self.trader.pending_flatten = False
        self.trader._active_strategy = ORBStrategy(config=ORBStrategyConfig(), inst_config=InstrumentConfig())
        self.trader._active_strategy.position = 0
        mock_day = datetime.datetime(2026, 11, 13, 10, 0, 0)
        with patch("live.shioaji_trader.datetime.datetime") as mock_dt2:
            mock_dt2.now.return_value = mock_day
            self.assertFalse(self.trader.reconnect())
            mock_api.margin.reset_mock()
            self.trader.keep_alive_heartbeat()
            mock_api.margin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
