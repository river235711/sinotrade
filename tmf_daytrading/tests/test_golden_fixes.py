# -*- coding: utf-8 -*-
"""
完整驗證「精簡黃金版 1~6 點改善」單元與整合測試：
1. BarAggregator RLock 執行緒安全與 CNYES_TXF 備援來源標記
2. Shioaji api.kbars -1 分鐘起始時間戳對齊與 CNYES 無位移規則
3. 夜盤 CSV 完整度驗證 (night_bars>=800, night_end>=04:55) 與 05:05 前防未完成寫入保護
4. StateManager entry_time 還原、重啟對帳 highest_profit 嚴格過濾 entry_time 之後、resume 外部指令存檔
5. 同分鐘 SAR 假突破反手後的合成高低價隔離保護 + 同分後段 Tick 即時停損檢查 + 收線清理
6. 重複 @on_tick_fop_v1 / subscribe 防呆、08:35 健檢複用既有連線不登出互踢
7. orders_YYYYMMDD.csv 第九欄 signal_price (含舊版 8 欄向下相容)
8. update_20days 永久 1分K 累積歸檔 (history_1m_kbars.csv) 與盤後夜盤補齊
9. 09:15 ORB 鎖定即時播報 (可進場 / 不進場原因 / 3A 已進場實際停損停利價)
10. BacktestEngine 預先載入快取 DataFrame 與原版磁碟讀取結果 100% 一致
"""

import os
import sys
import csv
import json
import tempfile
import datetime
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import InstrumentConfig, ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import load_daily_history
from backtester.engine import BacktestEngine
from backtester.run_20days_backtest import _preload_overnight_levels
from live.bar_aggregator import BarAggregator
from live.state_manager import StateManager
from live.execution import FillResult, append_order_audit
from live.market_data import (
    resolve_tmf_contract,
    normalize_shioaji_kbars,
    is_overnight_row_complete,
    should_persist_overnight,
    load_or_fetch_overnight_levels,
    build_0915_orb_report,
    fetch_cnyes_day_kbars,
)
from live.shioaji_trader import ShioajiTMFTrader
from live.update_20days import archive_1m_kbars, backfill_overnight_level
from live.health_check import run_health_check


def _make_sample_kbars(date_str: str, closes, highs=None, lows=None, opens=None, start_hhmm="08:45"):
    start_ts = pd.Timestamp(f"{date_str} {start_hhmm}:00")
    ts_list = [start_ts + pd.Timedelta(minutes=i) for i in range(len(closes))]
    opens = opens or list(closes)
    highs = highs or [c + 5 for c in closes]
    lows = lows or [c - 5 for c in closes]
    return pd.DataFrame(
        {
            "open": [float(x) for x in opens],
            "high": [float(x) for x in highs],
            "low": [float(x) for x in lows],
            "close": [float(x) for x in closes],
            "volume": [100] * len(closes),
        },
        index=pd.DatetimeIndex(ts_list, name="ts"),
    )


class TestGoldenFixes(unittest.TestCase):
    def test_bar_aggregator_thread_safety_and_backup_source(self):
        agg = BarAggregator(kbars_disabled=True)
        today_str = "2026-11-13"
        b_df = _make_sample_kbars(today_str, [23000, 23010], start_hhmm="08:45")
        agg.load_backup_bars(b_df, now_min=pd.Timestamp(f"{today_str} 09:00:00"), source_tag="CNYES_TXF")
        self.assertEqual(agg.backup_source, "CNYES_TXF")

        errors = []

        def writer():
            try:
                base_dt = datetime.datetime(2026, 11, 13, 9, 1, 0)
                for i in range(200):
                    agg.update_tick(23000 + (i % 50), 1, base_dt + datetime.timedelta(seconds=i % 59))
                    if i % 20 == 0:
                        agg.update_snapshot(23020.0, volume=5, dt=base_dt + datetime.timedelta(minutes=1))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    df = agg.to_dataframe(today_str=today_str)
                    _ = agg.needs_snapshot_sync(pd.Timestamp(f"{today_str} 09:02:00"))
                    if not df.empty:
                        self.assertIn("close", df.columns)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(3)] + [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_shioaji_kbars_minus_one_minute_alignment(self):
        raw = {
            "ts": [
                "2026-11-13 08:46:00",
                "2026-11-13 09:00:00",
                "2026-11-13 09:15:00",
            ],
            "Open": [23000, 23010, 23020],
            "High": [23010, 23020, 23030],
            "Low": [22995, 23005, 23015],
            "Close": [23005, 23015, 23025],
            "Volume": [50, 60, 70],
        }
        df = normalize_shioaji_kbars(raw, subtract_minute=True)
        expected = [
            pd.Timestamp("2026-11-13 08:45:00"),
            pd.Timestamp("2026-11-13 08:59:00"),
            pd.Timestamp("2026-11-13 09:14:00"),
        ]
        self.assertEqual(list(df.index), expected)

    def test_cnyes_keeps_start_timestamp_no_shift(self):
        mock_cnyes_df = pd.DataFrame({
            "ts": [
                pd.Timestamp("2026-11-13 08:45:00"),
                pd.Timestamp("2026-11-13 13:44:00"),
                pd.Timestamp("2026-11-13 13:45:00"),
            ],
            "open": [23000, 23100, 23110],
            "high": [23010, 23110, 23120],
            "low": [22990, 23090, 23100],
            "close": [23005, 23105, 23115],
            "volume": [100, 200, 300],
        })
        with patch("live.market_data.fetch_cnyes_history", return_value=mock_cnyes_df):
            day_df = fetch_cnyes_day_kbars("2026-11-13")
        self.assertEqual(
            list(day_df.index),
            [pd.Timestamp("2026-11-13 08:45:00"), pd.Timestamp("2026-11-13 13:44:00")],
        )

    def test_overnight_csv_completeness_and_pre_0505_guard(self):
        self.assertFalse(
            is_overnight_row_complete({"night_bars": 541, "night_end": "2026-11-13 00:01:00"}, "2026-11-13")
        )
        self.assertTrue(
            is_overnight_row_complete({"night_bars": 840, "night_end": "2026-11-13 04:59:00"}, "2026-11-13")
        )

        # 未過 05:05 且未滿 800 根時不可寫入 CSV
        partial_bars = pd.DataFrame({
            "ts": [pd.Timestamp("2026-11-12 15:00:00"), pd.Timestamp("2026-11-13 00:01:00")],
            "open": [23000, 23100],
            "high": [23050, 23120],
            "low": [22950, 23000],
            "close": [23020, 23110],
        })
        self.assertFalse(
            should_persist_overnight(partial_bars, "2026-11-13", now_dt=datetime.datetime(2026, 11, 13, 0, 2, 0))
        )
        self.assertTrue(
            should_persist_overnight(partial_bars, "2026-11-13", now_dt=datetime.datetime(2026, 11, 13, 5, 6, 0))
        )

        # 若 CSV 有殘缺夜盤，load_or_fetch_overnight_levels 會略過殘缺紀錄並重抓完整夜盤
        with tempfile.TemporaryDirectory() as tmp:
            lvl_path = os.path.join(tmp, "overnight_levels_summary.csv")
            pd.DataFrame([{
                "trading_date": "2026-11-13",
                "night_start": "2026-11-12 15:00:00",
                "night_end": "2026-11-13 00:01:00",
                "night_bars": 541,
                "night_open": 23000,
                "night_high": 23200,
                "night_low": 22900,
                "night_close": 23100,
                "night_range": 300,
            }]).to_csv(lvl_path, index=False)

            # 建立完整 840 根模擬 API kbars (Shioaji 回傳為收盤時間戳，故 15:01~05:00)
            full_ts = [pd.Timestamp("2026-11-12 15:01:00") + pd.Timedelta(minutes=i) for i in range(840)]
            mock_api = MagicMock()
            mock_api.kbars.return_value = {
                "ts": full_ts,
                "Open": [23100] * 840,
                "High": [23555 if i == 700 else 23200 for i in range(840)],
                "Low": [22888 if i == 800 else 23000 for i in range(840)],
                "Close": [23150] * 840,
                "Volume": [10] * 840,
            }
            mock_contract = SimpleNamespace(code="TMFK6")
            o_h, o_l, src = load_or_fetch_overnight_levels(
                tmp, "2026-11-13", api=mock_api, contract=mock_contract, allow_cnyes_fallback=False
            )
            self.assertEqual(src, "shioaji")
            self.assertEqual(o_h, 23555.0)
            self.assertEqual(o_l, 22888.0)

    def test_state_manager_entry_time_restore_and_startup_highest_profit_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = StateManager(log_dir=tmp)
            s_orig = ORBStrategy(ORBStrategyConfig(), InstrumentConfig())
            s_orig.position = 1
            s_orig.entry_price = 23100.0
            s_orig.entry_time = pd.Timestamp("2026-11-13 09:20:00")
            s_orig.highest_profit = 55.0
            s_orig.trade_count = 1
            sm.save_state(s_orig, date_str="20261113")

            saved = sm.load_state("20261113")
            s_new = ORBStrategy(ORBStrategyConfig(), InstrumentConfig())
            sm.restore_strategy_state(s_new, saved)
            self.assertEqual(s_new.entry_time, pd.Timestamp("2026-11-13 09:20:00"))

            # 驗證重啟接管計算 highest_profit 只看 entry_time 之後的 K 棒，不受早盤高點污染
            with patch("live.shioaji_trader.get_mode_log_dir", return_value=tmp), \
                 patch("live.shioaji_trader.TradeNotifier"):
                trader = ShioajiTMFTrader(simulation=False)
                trader.notifier = MagicMock()
                trader.api = MagicMock()
                trader.state_manager = sm
                trader.get_real_position_details = MagicMock(
                    return_value={"net": 1, "avg_price": 23100.0, "direction": "Buy", "positions": []}
                )
                # 09:05 有早盤大高點 23600；09:20 (進場前/當分) 與 09:21~09:22 僅漲到 23150
                kbars = _make_sample_kbars(
                    "2026-11-13",
                    closes=[23400, 23100, 23140, 23130],
                    highs=[23600, 23120, 23150, 23145],
                    lows=[23300, 23090, 23100, 23120],
                    start_hhmm="09:19",
                )
                kbars.index = pd.DatetimeIndex([
                    pd.Timestamp("2026-11-13 09:05:00"),
                    pd.Timestamp("2026-11-13 09:20:00"),
                    pd.Timestamp("2026-11-13 09:21:00"),
                    pd.Timestamp("2026-11-13 09:22:00"),
                ])
                # saved_state highest_profit=10 讓 K 棒後段 23150-23100=50 主導
                saved["highest_profit"] = 10.0
                sm.save_state(s_orig, date_str="20261113", custom_fields={"highest_profit": 10.0})

                s_rec = ORBStrategy(ORBStrategyConfig(), InstrumentConfig())
                with patch("live.shioaji_trader.datetime") as mock_dt:
                    mock_dt.date.today.return_value = datetime.date(2026, 11, 13)
                    mock_dt.datetime.now.return_value = datetime.datetime(2026, 11, 13, 9, 25, 0)
                    trader._reconcile_and_recover_on_startup(s_rec, kbars)

                self.assertEqual(s_rec.position, 1)
                self.assertEqual(s_rec.highest_profit, 50.0)

    def test_resume_command_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("live.shioaji_trader.get_mode_log_dir", return_value=tmp), \
                 patch("live.shioaji_trader.TradeNotifier"):
                trader = ShioajiTMFTrader(simulation=True)
                trader.notifier = MagicMock()
                trader.entries_halted = True
                strat = ORBStrategy(ORBStrategyConfig(), InstrumentConfig())
                cmd_path = os.path.join(tmp, "command.json")
                with open(cmd_path, "w", encoding="utf-8") as f:
                    json.dump({"cmd": "resume"}, f)
                trader._check_external_command(strat, cur_price=23000.0)
                self.assertFalse(trader.entries_halted)
                loaded = trader._load_state()
                self.assertIsNotNone(loaded)
                self.assertFalse(loaded["entries_halted"])

    def test_intrabar_sar_same_minute_synthetic_bar_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("live.shioaji_trader.get_mode_log_dir", return_value=tmp), \
                 patch("live.shioaji_trader.TradeNotifier"):
                trader = ShioajiTMFTrader(simulation=True)
                trader.notifier = MagicMock()
                cfg = ORBStrategyConfig(
                    enable_sar=True,
                    sar_end_time="11:00:00",
                    stop_loss_pts=60.0,
                    use_vwap_ema_filter=False,
                    use_atr_scaling=False,
                    use_overnight_wall_filter=False,
                )
                strategy = ORBStrategy(cfg, InstrumentConfig())
                strategy.orb_high = 23050.0
                strategy.orb_low = 22950.0
                strategy.orb_calculated = True
                strategy.position = 1
                strategy.entry_price = 23060.0
                strategy.entry_bar_idx = 0
                strategy.trade_count = 1

                # 同分 10:05 先小幅衝高至 23100 (未達+80鎖利，但遠高於反手後空單停損線 23000) 後急殺跌破 orb_low=22950 收 22940 -> 觸發 SAR_SELL
                ts_1005 = pd.Timestamp("2026-11-13 10:05:00")
                prev_bar = pd.Series({"open": 23060.0, "high": 23070.0, "low": 23040.0, "close": 23050.0, "vwap": 23000.0, "ema9": 23040.0, "ema21": 23010.0})
                latest_sar = pd.Series({"open": 23060.0, "high": 23100.0, "low": 22935.0, "close": 22940.0, "vwap": 23000.0, "ema9": 22980.0, "ema21": 23010.0})
                processed = set()

                # Stub _send_and_verify so SAR fills at 22940 cleanly
                trader._send_and_verify = MagicMock(return_value=FillResult(True, 2, 22940.0, "Filled"))
                trader._post_trade_check = MagicMock()

                trader._evaluate_intrabar_tick(strategy, ts_1005, latest_sar, prev_bar, processed)
                self.assertEqual(strategy.position, -1)
                self.assertIsNotNone(trader._intrabar_sar_state)
                self.assertNotIn(ts_1005, processed)

                # 同分鐘下一筆 Tick 小幅震盪在 22930：原始 latest["high"] 仍有反手前 23100 (>22940+60)，但合成狀態不得誤停損！
                latest_next_tick = pd.Series({"open": 23060.0, "high": 23100.0, "low": 22930.0, "close": 22930.0, "vwap": 23000.0, "ema9": 22970.0, "ema21": 23005.0})
                trader._evaluate_intrabar_tick(strategy, ts_1005, latest_next_tick, prev_bar, processed)
                self.assertEqual(strategy.position, -1)

                # 若同分鐘後續 Tick 真的反抽至 23010 (對空單 @22940 已虧 70 > stop_loss 60)，必須即時觸發停損平倉！
                latest_stop_tick = pd.Series({"open": 23060.0, "high": 23100.0, "low": 22930.0, "close": 23010.0, "vwap": 23000.0, "ema9": 22990.0, "ema21": 23005.0})
                trader._evaluate_intrabar_tick(strategy, ts_1005, latest_stop_tick, prev_bar, processed)
                self.assertEqual(strategy.position, 0)
                self.assertIsNone(trader._intrabar_sar_state)
                self.assertIn(ts_1005, processed)

    def test_duplicate_tick_subscribe_guard_and_health_check_session_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("live.shioaji_trader.get_mode_log_dir", return_value=tmp), \
                 patch("live.shioaji_trader.TradeNotifier"):
                trader = ShioajiTMFTrader(simulation=True)
                mock_api = MagicMock()
                c_target = SimpleNamespace(code="TMFK6", name="微台指202611")
                c_r1 = SimpleNamespace(code="TMFR1", name="微台指近月", target_code="TMFK6")
                mock_api.contracts.futures.return_value = [c_r1, c_target]
                trader.api = mock_api

                trader.load_tmf_contract("TMFR1")
                trader.load_tmf_contract("TMFR1")
                self.assertEqual(mock_api.subscribe.call_count, 1)
                self.assertEqual(mock_api.on_tick_fop_v1.call_count, 1)

                # run_health_check 傳入既有 api/contract 時不可呼叫 logout
                mock_api.futopt_account = SimpleNamespace(broker_id="F002000", account_id="SIM001", username="Tester")
                mock_api.list_positions.return_value = []
                res = run_health_check(mode="paper", qty=1, send_notify=False, api=mock_api, contract=c_target)
                self.assertTrue(res["overall_ok"])
                mock_api.logout.assert_not_called()

    def test_audit_csv_signal_price_and_legacy_8col_compat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixed_dt = datetime.datetime(2026, 11, 13, 9, 16, 5)
            append_order_audit(
                tmp, "BUY", 1, "Auto", FillResult(True, 1, 23102.0, "Filled"),
                signal_price=23100.0, now_dt=fixed_dt
            )
            csv_path = os.path.join(tmp, "orders_20261113.csv")
            with open(csv_path, "r", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0][-1], "signal_price")
            self.assertEqual(rows[1][-1], "23100")

            # 舊版 8 欄檔案續寫不可多出第 9 欄導致錯欄
            legacy_dt = datetime.datetime(2026, 11, 12, 9, 16, 5)
            legacy_path = os.path.join(tmp, "orders_20261112.csv")
            with open(legacy_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["time", "action", "qty", "octype", "status", "filled_qty", "avg_price", "ok"])
            append_order_audit(
                tmp, "BUY", 1, "Auto", FillResult(True, 1, 23002.0, "Filled"),
                signal_price=23000.0, now_dt=legacy_dt
            )
            with open(legacy_path, "r", encoding="utf-8") as f:
                l_rows = list(csv.reader(f))
            self.assertEqual(len(l_rows[0]), 8)
            self.assertEqual(len(l_rows[1]), 8)

    def test_build_0915_orb_report_can_enter_no_entry_and_in_position(self):
        cfg = ORBStrategyConfig(
            stop_loss_pts=100.0,
            breakeven_trigger_pts=75.0,
            breakeven_lock_pts=25.0,
            tier1_5_trigger_pts=160.0,
            tier1_5_lock_pts=100.0,
            tier2_trigger_pts=230.0,
            tier2_lock_pts=150.0,
            trailing_activation_pts=300.0,
            trailing_distance_pts=80.0,
            max_orb_range_pts=320.0,
            max_initial_trades=1,
        )
        # 1. 可進場
        s1 = ORBStrategy(cfg, InstrumentConfig())
        s1.orb_high = 23200.0
        s1.orb_low = 23000.0
        s1.orb_calculated = True
        code1, msg1 = build_0915_orb_report(s1)
        self.assertEqual(code1, "CAN_ENTER")
        self.assertIn("可進場", msg1)
        self.assertIn("23100", msg1)   # 做多停損 23200 - 100
        self.assertIn("23275", msg1)   # 做多一階觸 23275
        self.assertIn("23360", msg1)   # 做多中階觸 23360
        self.assertIn("23430", msg1)   # 做多二階觸 23430
        self.assertIn("23500", msg1)   # 做多三階觸 23500

        # 2. 不進場 (振幅超過上限)
        s2 = ORBStrategy(cfg, InstrumentConfig())
        s2.orb_high = 23500.0
        s2.orb_low = 23000.0
        s2.orb_calculated = True
        code2, msg2 = build_0915_orb_report(s2)
        self.assertEqual(code2, "NO_ENTRY")
        self.assertIn("今日不進場", msg2)
        self.assertIn("500", msg2)

        # 3. 3A 已進場 (顯示實際停損/停利價)
        s3 = ORBStrategy(cfg, InstrumentConfig())
        s3.orb_high = 23200.0
        s3.orb_low = 23000.0
        s3.orb_calculated = True
        s3.position = 1
        s3.entry_price = 23215.0
        s3.trade_count = 1
        code3, msg3 = build_0915_orb_report(s3)
        self.assertEqual(code3, "IN_POSITION")
        self.assertIn("23115", msg3)   # 23215 - 100
        self.assertIn("23290", msg3)   # 23215 + 75

    def test_update_20days_cumulative_archive_and_overnight_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_rows = [
                {"timestamp": f"2026-10-{d:02d} 08:45:00", "open": 23000, "high": 23010, "low": 22990, "close": 23005, "volume": 100, "date": f"2026-10-{d:02d}", "contract": "TMFR1"}
                for d in range(1, 21)
            ]
            seed_df = pd.DataFrame(seed_rows)
            today_df = pd.DataFrame([
                {"timestamp": "2026-11-13 08:45:00", "open": 23500, "high": 23510, "low": 23490, "close": 23505, "volume": 120, "date": "2026-11-13", "contract": "TMFR1"}
            ])
            hist_path = os.path.join(tmp, "history_1m_kbars.csv")
            merged = archive_1m_kbars(hist_path, seed_df, today_df, "2026-11-13")
            self.assertEqual(merged["date"].nunique(), 21)
            self.assertIn("2026-10-01", set(merged["date"]))

            # 已完整則 backfill 不觸發 Shioaji 登入
            lvl_path = os.path.join(tmp, "overnight_levels_summary.csv")
            pd.DataFrame([{
                "trading_date": "2026-11-13",
                "night_start": "2026-11-12 15:00:00",
                "night_end": "2026-11-13 04:59:00",
                "night_bars": 840,
                "night_open": 23000,
                "night_high": 23400,
                "night_low": 22900,
                "night_close": 23200,
                "night_range": 500,
            }]).to_csv(lvl_path, index=False)
            self.assertTrue(backfill_overnight_level(tmp, "2026-11-13"))

    def test_backtest_engine_preloaded_cache_parity(self):
        data_dir = os.path.join(BASE_DIR, "data")
        cache_path = os.path.join(data_dir, "tmf_20days_1m_kbars.csv")
        if not os.path.exists(cache_path):
            self.skipTest("缺少 tmf_20days_1m_kbars.csv")

        cached_kbars = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        daily_hist = load_daily_history(data_dir)
        overnight_df = _preload_overnight_levels(data_dir)
        sample_date = str(sorted(cached_kbars["date"].unique())[-1]).replace("-", "")
        inst_cfg = InstrumentConfig(symbol="TMF", multiplier=10.0, commission_per_order=12.0, tax_rate=0.00001, slippage_points=1.0)

        e_disk = BacktestEngine(os.path.join(data_dir, f"Daily_{sample_date}.csv"), inst_config=inst_cfg)
        e_disk.load_taifex_csv(target_date=sample_date)
        m_disk = e_disk.run_orb_strategy(ORBStrategyConfig())

        e_mem = BacktestEngine(
            os.path.join(data_dir, f"Daily_{sample_date}.csv"),
            inst_config=inst_cfg,
            cached_kbars_df=cached_kbars,
            daily_history_df=daily_hist,
            overnight_levels_df=overnight_df,
        )
        e_mem.load_taifex_csv(target_date=sample_date)
        m_mem = e_mem.run_orb_strategy(ORBStrategyConfig())

        self.assertEqual(m_disk["total_points"], m_mem["total_points"])
        self.assertEqual(m_disk["net_profit_ntd"], m_mem["net_profit_ntd"])
        self.assertEqual(m_disk["orb_high"], m_mem["orb_high"])
        self.assertEqual(m_disk["orb_low"], m_mem["orb_low"])


if __name__ == "__main__":
    unittest.main()
