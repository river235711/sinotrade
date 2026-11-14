#!/usr/bin/env python3
"""
TMF DayTrading V2 架構與策略等價性完整測試套件
涵蓋：
1. core/ 零 I/O 與零 Shioaji 依賴純潔性檢查
2. evaluate_bar 純函式不變性 + apply_intent_fill 成交推進 + 同K保護 + 三階鎖利 + 移動停利 + 夜盤防撞牆 + SAR
3. OnlineIndicators 與批次指標 100% 數值等價
4. TradingDayState 跨日全物件替換重置
5. SessionFSM 9 階段狀態機與委託拒單不汙染狀態測試
6. PositionGuard 啟動接管、盤中對帳、跨月殘倉 allow_fallback=False 嚴格防呆與 13:09 死守平倉
7. BarAggregator 執行緒安全與備援隔離
8. V1 vs V2 近 20 個交易日 100% 逐筆成交與損益完全對齊驗證
"""
import ast
import contextlib
import dataclasses
import datetime
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pandas as pd

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V1_DIR = os.path.join(os.path.dirname(V2_DIR), "tmf_daytrading")
if V2_DIR not in sys.path:
    sys.path.insert(0, V2_DIR)

from config import InstrumentConfig, ORBStrategyConfig
from core.indicators import OnlineIndicators, prepare_batch_indicators
from core.models import Bar, EnrichedBar, OrderIntent, SessionPhase, TradingDayState
from core.orb_strategy import (
    apply_exit_only,
    apply_intent_fill,
    evaluate_bar,
    with_orb_levels,
    with_overnight_levels,
)
from gateways.base import BrokerGateway, FillResult
from gateways.data_repo import StateRepo, load_daily_history
from gateways.shioaji_paper import ShioajiPaperGateway
from backtester.engine import BacktestEngine as V2BacktestEngine
from backtester.run_20days_backtest import _preload_overnight_levels
from runtime.bar_aggregator import BarAggregator
from runtime.notifier import TradeNotifier
from runtime.position_guard import PositionGuard
from runtime.session_fsm import SessionFSM


class FakeBrokerGateway(BrokerGateway):
    """純記憶體假券商配接器，供單元測試使用"""

    def __init__(
        self,
        fill_ok: bool = True,
        fill_price: float = 0.0,
        update_ledger_on_fill: bool = True,
    ):
        self.is_simulation = False
        self.connected = True
        self.ca_activated = True
        self.requested_contract_code = "TMFR1"
        self.contract = type("C", (), {"code": "TMFK6", "name": "微台202611"})()
        self.contract_code = "TMFK6"
        self.fill_ok = fill_ok
        self.fill_price = fill_price
        self.update_ledger_on_fill = update_ledger_on_fill
        self.connect_calls: List[str] = []
        self.close_calls: int = 0
        self.orders_placed: List[Dict[str, Any]] = []
        self.resolve_calls: List[Tuple[str, bool]] = []
        self.contracts_map: Dict[str, Any] = {"TMFK6": self.contract, "TMFR1": self.contract}
        self.query_sequence: List[Dict[str, Any]] = []
        self.order_sequence: List[FillResult] = []
        self.kbars_df: pd.DataFrame = pd.DataFrame()
        self.position_details: Dict[str, Any] = {
            "net": 0,
            "avg_price": 0.0,
            "direction": "Flat",
            "positions": [],
            "other_positions": [],
            "target_code": "TMFK6",
        }

    def connect(self, contract_code: str = "") -> bool:
        req = contract_code or self.requested_contract_code or "TMFR1"
        self.connect_calls.append(req)
        resolved = self.contracts_map.get(req, self.contract)
        if resolved is not None:
            self.contract = resolved
            self.contract_code = getattr(resolved, "code", self.contract_code)
        self.connected = True
        return True

    def reconnect(self, contract_code: str = "") -> bool:
        return self.connect(contract_code)

    def resolve_contract(self, code: str = "TMFR1", allow_fallback: bool = True) -> Any:
        self.resolve_calls.append((code, allow_fallback))
        if code in self.contracts_map:
            return self.contracts_map[code]
        return self.contract if allow_fallback else None

    def subscribe_ticks(self, on_tick_cb) -> bool:
        return True

    def fetch_kbars(self, start_date: str, end_date: str, timeout: int = 5000) -> pd.DataFrame:
        return self.kbars_df.copy() if not self.kbars_df.empty else pd.DataFrame()

    def fetch_snapshot_price(self, today_str: str):
        return None

    def place_order(
        self,
        action: str,
        qty: int,
        price_type: str = "MKP",
        octype: str = "Auto",
        price: float = 0.0,
        contract_override: Any = None,
        fill_timeout: float = 8.0,
        poll_interval: float = 0.15,
    ) -> FillResult:
        self.orders_placed.append({
            "action": action,
            "qty": qty,
            "price_type": price_type,
            "octype": octype,
            "price": price,
            "contract_override": getattr(contract_override, "code", None),
        })
        if self.order_sequence:
            res = self.order_sequence.pop(0)
            if self.update_ledger_on_fill and contract_override is None and res.filled_qty > 0:
                delta = int(res.filled_qty) if action == "BUY" else -int(res.filled_qty)
                new_net = int(self.position_details.get("net", 0)) + delta
                px_fill = res.avg_price if res.avg_price > 0 else price
                self.position_details["net"] = new_net
                self.position_details["avg_price"] = px_fill if new_net != 0 else 0.0
                self.position_details["direction"] = "Buy" if new_net > 0 else ("Sell" if new_net < 0 else "Flat")
            return res
        if not self.fill_ok:
            return FillResult(ok=False, filled_qty=0, avg_price=0.0, status="Cancelled")
        px = self.fill_price if self.fill_price > 0 else price
        if self.update_ledger_on_fill and contract_override is None:
            delta = int(qty) if action == "BUY" else -int(qty)
            new_net = int(self.position_details.get("net", 0)) + delta
            self.position_details["net"] = new_net
            self.position_details["avg_price"] = px if new_net != 0 else 0.0
            self.position_details["direction"] = "Buy" if new_net > 0 else ("Sell" if new_net < 0 else "Flat")
        return FillResult(ok=True, filled_qty=qty, avg_price=px, status="Filled")

    def query_position_details(self, contract_code: Optional[str] = None) -> Dict[str, Any]:
        if self.query_sequence:
            return dict(self.query_sequence.pop(0))
        return dict(self.position_details)

    def cancel_pending_orders(self) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False


class TestCorePurityAndStrategy(unittest.TestCase):
    def test_core_has_no_io_or_shioaji_imports(self):
        forbidden = {"shioaji", "requests", "urllib", "smtplib", "ssl", "subprocess"}
        core_dir = os.path.join(V2_DIR, "core")
        for fn in os.listdir(core_dir):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(core_dir, fn), "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fn)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertTrue(
                forbidden.isdisjoint(imported),
                f"{fn} imported forbidden external/IO modules: {forbidden & imported}",
            )

    def test_evaluate_bar_is_pure_and_does_not_mutate_state(self):
        cfg = ORBStrategyConfig(use_vwap_ema_filter=True, use_overnight_wall_filter=False)
        state = TradingDayState(
            trading_date="2026-10-30",
            orb_high=23100.0,
            orb_low=23000.0,
            orb_calculated=True,
        )
        bar = Bar(pd.Timestamp("2026-10-30 09:20:00"), 23090.0, 23130.0, 23085.0, 23120.0, 50.0)
        eb = EnrichedBar(bar=bar, vwap=23080.0, ema9=23100.0, ema21=23090.0)

        intent = evaluate_bar(state, eb, cfg)
        self.assertEqual(intent.action, "BUY")
        self.assertEqual(intent.side_sign, 1)
        # state 必須完全保持原樣 (不變性)
        self.assertEqual(state.position, 0)
        self.assertEqual(state.entry_price, 0.0)
        self.assertEqual(state.trade_count, 0)

        # 僅在成交回報後才推進狀態
        next_state, rec = apply_intent_fill(state, intent, fill_price=23122.0, fill_time=bar.timestamp)
        self.assertIsNone(rec)
        self.assertEqual(next_state.position, 1)
        self.assertEqual(next_state.entry_price, 23122.0)
        self.assertEqual(next_state.trade_count, 1)

    def test_online_indicators_match_batch_prepare(self):
        times = pd.date_range("2026-10-30 08:45:00", periods=40, freq="1min")
        closes = [23000.0 + (i * 3.5) + ((-1) ** i) * 7.0 for i in range(40)]
        vols = [float(10 + (i % 5) * 4) for i in range(40)]
        df = pd.DataFrame({
            "open": closes,
            "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes],
            "close": closes,
            "volume": vols,
        }, index=times)
        batch = prepare_batch_indicators(df)

        online = OnlineIndicators()
        for idx, (c, v) in enumerate(zip(closes, vols)):
            vwap_p, e9_p, e21_p = online.peek(c, v)
            self.assertAlmostEqual(vwap_p, float(batch["vwap"].iloc[idx]), places=9)
            self.assertAlmostEqual(e9_p, float(batch["ema9"].iloc[idx]), places=9)
            self.assertAlmostEqual(e21_p, float(batch["ema21"].iloc[idx]), places=9)
            vwap, e9, e21 = online.update(c, v)
            self.assertAlmostEqual(vwap, float(batch["vwap"].iloc[idx]), places=9)
            self.assertAlmostEqual(e9, float(batch["ema9"].iloc[idx]), places=9)
            self.assertAlmostEqual(e21, float(batch["ema21"].iloc[idx]), places=9)

    def test_same_candle_protection_and_tier_locks_and_sar(self):
        cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False)
        # 進場多單於 23000，若同根陽線先衝到 23100 (+100 觸發 +80->+30 一階鎖利)，收盤在 23090，
        # 同根低點 22990 雖低於 23030，因 prev_hp=0 尚未超過 80 且為紅 K (c>o)，不應誤觸鎖利！
        state = TradingDayState(
            trading_date="2026-10-30",
            orb_high=23000.0,
            orb_low=22900.0,
            orb_calculated=True,
            position=1,
            entry_price=23000.0,
            entry_time=pd.Timestamp("2026-10-30 09:20:00"),
            trade_count=1,
        )
        bar1 = EnrichedBar(
            Bar(pd.Timestamp("2026-10-30 09:21:00"), 23010.0, 23100.0, 22990.0, 23090.0, 20.0),
            vwap=23050.0, ema9=23040.0, ema21=23020.0,
        )
        i1 = evaluate_bar(state, bar1, cfg)
        self.assertEqual(i1.action, "HOLD")
        self.assertEqual(i1.new_highest_profit, 100.0)
        state, _ = apply_intent_fill(state, i1)

        # 下一根跌破 +30 鎖利線 (23030)，應觸發一階保本鎖利出場
        bar2 = EnrichedBar(
            Bar(pd.Timestamp("2026-10-30 09:22:00"), 23080.0, 23085.0, 23020.0, 23025.0, 20.0),
            vwap=23050.0, ema9=23040.0, ema21=23020.0,
        )
        i2 = evaluate_bar(state, bar2, cfg)
        self.assertEqual(i2.action, "EXIT")
        self.assertEqual(i2.pnl_pts, 30.0)

        # 測試 SAR 反手與 apply_exit_only 退避
        sl_state = TradingDayState(
            trading_date="2026-10-30",
            orb_high=23000.0,
            orb_low=22900.0,
            orb_calculated=True,
            position=1,
            entry_price=23000.0,
            entry_time=pd.Timestamp("2026-10-30 09:20:00"),
            trade_count=1,
        )
        sl_bar = EnrichedBar(
            Bar(pd.Timestamp("2026-10-30 09:30:00"), 22980.0, 22990.0, 22905.0, 22915.0, 50.0),
            vwap=22960.0, ema9=22940.0, ema21=22950.0,
        )
        sar_intent = evaluate_bar(sl_state, sl_bar, cfg)
        self.assertEqual(sar_intent.action, "SAR_SELL")
        self.assertEqual(sar_intent.side_sign, -1)

        s_sar, rec_sar = apply_intent_fill(sl_state, sar_intent, fill_price=22910.0, fill_time=sl_bar.bar.timestamp, new_entry_price=22908.0)
        self.assertEqual(s_sar.position, -1)
        self.assertEqual(s_sar.entry_price, 22908.0)
        self.assertEqual(s_sar.trade_count, 2)
        self.assertEqual(rec_sar.pnl_pts, -90.0)

        s_fallback, _ = apply_exit_only(sl_state, sar_intent, fill_price=22910.0, fill_time=sl_bar.bar.timestamp)
        self.assertEqual(s_fallback.position, 0)


class TestRuntimeFSMAndGuards(unittest.TestCase):
    def setUp(self):
        # 預設攔截所有單元測試期間之真實 Telegram/Email 推播，避免寄出測試警訊
        p = patch.object(TradeNotifier, "send", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_session_fsm_9_phases_and_rollover_resets_all_flags(self):
        cfg = ORBStrategyConfig()
        expected = [
            ("2026-11-16 08:00:00", SessionPhase.PREMARKET_STANDBY),
            ("2026-11-16 08:35:00", SessionPhase.HEALTH_CHECK),
            ("2026-11-16 08:45:00", SessionPhase.PRE_ORB_BUILD),
            ("2026-11-16 09:00:30", SessionPhase.ORB_BUILD),
            ("2026-11-16 09:01:00", SessionPhase.EARLY_BREAKOUT),
            ("2026-11-16 09:16:00", SessionPhase.MAIN_BREAKOUT),
            ("2026-11-16 11:30:01", SessionPhase.POST_ENTRY_HOLD),
            ("2026-11-16 13:08:00", SessionPhase.FORCE_CLOSE),
            ("2026-11-16 13:10:01", SessionPhase.POST_CLOSE_SETTLE),
        ]
        for dt_s, exp_phase in expected:
            dt = datetime.datetime.strptime(dt_s, "%Y-%m-%d %H:%M:%S")
            self.assertEqual(SessionFSM.determine_phase(dt, cfg), exp_phase)

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "paper")
            gw = FakeBrokerGateway()
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1)
            # 汙染舊日各種旗標與持倉
            fsm.state = TradingDayState(
                trading_date="2026-11-13",
                position=1,
                entry_price=23000.0,
                trade_count=2,
                entries_halted=True,
                morning_wake_logged=True,
                health_check_sent=True,
                orb_finalized=True,
                orb_report_sent=True,
                offline_hc_alert_sent=True,
                post_close_done=True,
                settlement_alert_sent=True,
                eod_roll_done=True,
                holiday_detected=True,
            )
            rolled = fsm.rollover_day("2026-11-16")
            self.assertEqual(rolled.trading_date, "2026-11-16")
            self.assertEqual(rolled.position, 0)
            self.assertEqual(rolled.trade_count, 0)
            self.assertFalse(rolled.entries_halted)
            self.assertFalse(rolled.morning_wake_logged)
            self.assertFalse(rolled.health_check_sent)
            self.assertFalse(rolled.orb_finalized)
            self.assertFalse(rolled.orb_report_sent)
            self.assertFalse(rolled.offline_hc_alert_sent)
            self.assertFalse(rolled.post_close_done)
            self.assertFalse(rolled.settlement_alert_sent)
            self.assertFalse(rolled.eod_roll_done)
            self.assertFalse(rolled.holiday_detected)

    def test_order_failure_does_not_mutate_trading_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            gw = FakeBrokerGateway(fill_ok=False)
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1)
            intent = OrderIntent(action="BUY", side_sign=1, price=23100.0, reason="TestReject")
            st, rec = fsm.execute_order_intent(intent, pd.Timestamp("2026-11-16 09:20:00"))
            self.assertEqual(st.position, 0)
            self.assertEqual(st.trade_count, 0)
            self.assertIsNone(rec)

    def test_intrabar_stop_loss_triggers_before_bar_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "paper")
            gw = FakeBrokerGateway(fill_ok=True, fill_price=22905.0)
            gw.position_details["net"] = 1
            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False, enable_sar=False)
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1, base_cfg=cfg)
            fsm.state = TradingDayState(
                trading_date="2026-11-16",
                position=1,
                entry_price=23000.0,
                entry_time=pd.Timestamp("2026-11-16 09:19:00"),
                trade_count=1,
                effective_config=cfg,
            )
            # 模擬 09:20:15 分鐘尚未收線前的下跌 Tick 跌破 90 點停損 (22910)
            fsm.bar_aggregator.update_tick(22990.0, 1, datetime.datetime(2026, 11, 16, 9, 20, 5))
            fsm.bar_aggregator.update_tick(22905.0, 1, datetime.datetime(2026, 11, 16, 9, 20, 15))
            fsm.step(datetime.datetime(2026, 11, 16, 9, 20, 15))
            self.assertEqual(fsm.state.position, 0)
            self.assertEqual(len(gw.orders_placed), 1)
            self.assertEqual(gw.orders_placed[0]["action"], "SELL")

    def test_position_guard_takeover_and_cross_month_strict_no_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = StateRepo(log_dir=tmp)
            guard = PositionGuard(log_dir=tmp)
            gw = FakeBrokerGateway()

            # 1. 模擬跨月殘倉 (TMFJ6)，但券商查無該舊合約且 allow_fallback=False 回傳 None -> 嚴禁退回 TMFR1 下單！
            gw.position_details = {
                "net": 1,
                "avg_price": 23050.0,
                "direction": "Buy",
                "positions": [{"code": "TMFK6", "direction": "Buy", "qty": 1, "price": 23050.0}],
                "other_positions": [{"code": "TMFJ6", "net": -1, "qty": 1, "direction": "Sell", "avg_price": 22980.0}],
                "target_code": "TMFK6",
            }
            init_state = TradingDayState(trading_date="2026-11-16", contract_code="TMFK6")
            taken = guard.startup_reconcile(init_state, gw, qty=1, state_repo=repo)
            self.assertEqual(taken.position, 1)
            self.assertEqual(taken.entry_price, 23050.0)
            self.assertIn(("TMFJ6", False), gw.resolve_calls)
            # 未解析出 TMFJ6 時絕不送出任何誤平 TMFK6/TMFR1 之委託
            self.assertEqual(len(gw.orders_placed), 0)

            # 2. 若明確解析出 TMFJ6 合約物件，才應對該精準合約下 Cover 單
            old_contract = type("C", (), {"code": "TMFJ6"})()
            gw.contracts_map["TMFJ6"] = old_contract
            ok = guard.flatten_other_month_positions(gw.position_details["other_positions"], gw)
            self.assertTrue(ok)
            self.assertEqual(len(gw.orders_placed), 1)
            self.assertEqual(gw.orders_placed[0]["action"], "BUY")
            self.assertEqual(gw.orders_placed[0]["contract_override"], "TMFJ6")

    def test_bar_aggregator_thread_safety_and_paper_ledger(self):
        agg = BarAggregator()
        errors = []

        def writer():
            try:
                base = datetime.datetime(2026, 11, 16, 9, 0, 0)
                for i in range(100):
                    agg.update_tick(23000.0 + i, 1, base + datetime.timedelta(seconds=i))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    agg.to_dataframe(today_str="2026-11-16")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        paper = ShioajiPaperGateway(contract_code="TMFK6")
        paper.set_last_price(23100.0)
        res_open = paper.place_order("BUY", 1)
        self.assertTrue(res_open.ok)
        self.assertEqual(paper.query_position_details()["net"], 1)
        paper.set_last_price(23200.0)
        res_close = paper.place_order("SELL", 1)
        self.assertTrue(res_close.ok)
        self.assertEqual(paper.query_position_details()["net"], 0)

    def test_off_market_sleep_0830_wake_and_0905_holiday_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            gw = FakeBrokerGateway()
            mock_notifier = MagicMock()
            fsm = SessionFSM(
                gateway=gw,
                base_dir=tmp,
                log_dir=log_dir,
                quantity=1,
                notifier=mock_notifier,
                holiday_grace_seconds=30.0,
            )

            # 1. 盤前 08:15 自動登出休眠
            fsm.step(datetime.datetime(2026, 11, 16, 8, 15, 0))
            self.assertFalse(gw.connected)
            self.assertEqual(gw.close_calls, 1)

            # 2. 模擬換月：TMFR1 切換為新主力合約 TMFL6，08:30 自動喚醒並重抓合約
            next_month = type("C", (), {"code": "TMFL6", "name": "微台202612"})()
            gw.contracts_map["TMFR1"] = next_month
            fsm.step(datetime.datetime(2026, 11, 16, 8, 30, 0))
            self.assertTrue(gw.connected)
            self.assertEqual(gw.connect_calls, ["TMFR1"])
            self.assertEqual(fsm.state.contract_code, "TMFL6")

            # 3. 09:05:00 與 09:05:31 仍 0 Tick -> 判定休市日自動登出深度睡眠
            fsm.step(datetime.datetime(2026, 11, 16, 9, 5, 0))
            self.assertTrue(gw.connected)
            self.assertFalse(fsm.state.holiday_detected)

            fsm.step(datetime.datetime(2026, 11, 16, 9, 5, 31))
            self.assertTrue(fsm.state.holiday_detected)
            self.assertTrue(fsm.state.entries_halted)
            self.assertFalse(gw.connected)

            # 4. 休市日後 13:50 不再重複連線或跑盤後流程
            fsm.step(datetime.datetime(2026, 11, 16, 13, 50, 0))
            self.assertFalse(gw.connected)

    def test_134515_auto_eod_roll_20days(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            gw = FakeBrokerGateway()
            mock_notifier = MagicMock()
            rolled_dates: List[str] = []

            def fake_roll(d_str: str) -> bool:
                rolled_dates.append(d_str)
                return True

            fsm = SessionFSM(
                gateway=gw,
                base_dir=tmp,
                log_dir=log_dir,
                quantity=1,
                notifier=mock_notifier,
                eod_roll_fn=fake_roll,
            )

            # 13:10:05 確認零部位並發送收盤完成通知，但不提前觸發 13:45:15 滾動更新
            fsm.step(datetime.datetime(2026, 11, 16, 13, 10, 5))
            self.assertTrue(fsm.state.post_close_done)
            self.assertFalse(fsm.state.eod_roll_done)
            self.assertEqual(rolled_dates, [])

            # 13:45:15 自動執行 roll_20days 一次
            fsm.step(datetime.datetime(2026, 11, 16, 13, 45, 15))
            fsm.step(datetime.datetime(2026, 11, 16, 13, 46, 0))
            self.assertTrue(fsm.state.eod_roll_done)
            self.assertEqual(rolled_dates, ["2026-11-16"])
            self.assertTrue(gw.connected)

            # 13:50:00 安全登出離線休眠
            fsm.step(datetime.datetime(2026, 11, 16, 13, 50, 0))
            self.assertFalse(gw.connected)

    def test_0915_preliminary_and_0916_bar_close_orb_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "paper")
            gw = FakeBrokerGateway()
            sent_msgs: List[str] = []
            mock_notifier = MagicMock()
            mock_notifier.send.side_effect = lambda msg, **kw: sent_msgs.append(msg)
            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False)
            fsm = SessionFSM(
                gateway=gw,
                base_dir=tmp,
                log_dir=log_dir,
                quantity=1,
                base_cfg=cfg,
                notifier=mock_notifier,
            )
            fsm.rollover_day("2026-11-16")
            fsm.bar_aggregator.history_loaded = True

            # 建立 08:45~09:14 分K (09:00~09:14 區間為 23000 ~ 23100)
            for m in range(45, 60):
                fsm.bar_aggregator.update_tick(23050.0, 5, datetime.datetime(2026, 11, 16, 8, m, 10))
            for m in range(0, 15):
                px = 23100.0 if m == 5 else (23000.0 if m == 10 else 23050.0)
                fsm.bar_aggregator.update_tick(px, 5, datetime.datetime(2026, 11, 16, 9, m, 10))

            # 09:14 時不應提前設定 pre_orb / orb (對齊 BacktestEngine 與 V1)
            fsm.step(datetime.datetime(2026, 11, 16, 9, 14, 10))
            self.assertFalse(fsm.state.orb_calculated)
            self.assertIsNone(fsm.state.pre_orb_high)

            # 09:15:00 收到首 Tick -> 初次鎖定 ORB=23000~23100 並發送確立報告，但尚未定稿
            fsm.bar_aggregator.update_tick(23060.0, 5, datetime.datetime(2026, 11, 16, 9, 15, 0))
            fsm.step(datetime.datetime(2026, 11, 16, 9, 15, 0))
            self.assertTrue(fsm.state.orb_calculated)
            self.assertFalse(fsm.state.orb_finalized)
            self.assertEqual(fsm.state.orb_high, 23100.0)
            self.assertEqual(fsm.state.orb_low, 23000.0)
            self.assertEqual(len(sent_msgs), 1)
            self.assertIn("📌 [09:15 ORB 區間確立]", sent_msgs[0])

            # 09:15:30 突破區間新高至 23140 (尚在 09:15 分鐘內不重發)
            fsm.bar_aggregator.update_tick(23140.0, 5, datetime.datetime(2026, 11, 16, 9, 15, 30))
            fsm.step(datetime.datetime(2026, 11, 16, 9, 15, 30))
            self.assertEqual(len(sent_msgs), 1)

            # 09:16:00 09:15 K棒完整收盤 -> 二次擴大校正 ORB 至 23140 並補發更新推播
            fsm.bar_aggregator.update_tick(23135.0, 5, datetime.datetime(2026, 11, 16, 9, 16, 0))
            fsm.step(datetime.datetime(2026, 11, 16, 9, 16, 0))
            self.assertTrue(fsm.state.orb_finalized)
            self.assertEqual(fsm.state.orb_high, 23140.0)
            self.assertEqual(len(sent_msgs), 2)
            self.assertIn("📌 [09:15 ORB 完整收盤更新]", sent_msgs[1])
            self.assertIn("23000 ~ 23140", sent_msgs[1])

    def test_post_trade_verify_retries_and_residual_flatten(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = StateRepo(log_dir=tmp)
            mock_notifier = MagicMock()
            guard = PositionGuard(log_dir=tmp, post_trade_retries=3, post_trade_retry_delay=0.0)

            # 情境一：實盤買進 1 口成交後券商帳務延遲 2 次仍顯示 0，第 3 次同步為 +1 口 -> 通過不誤補平
            gw_lag = FakeBrokerGateway(fill_ok=True, fill_price=23100.0)
            gw_lag.query_sequence = [
                {"net": 0, "avg_price": 0.0, "other_positions": []},
                {"net": 0, "avg_price": 0.0, "other_positions": []},
                {"net": 1, "avg_price": 23100.0, "other_positions": []},
            ]
            s_open = TradingDayState(trading_date="2026-11-16", contract_code="TMFK6", position=1, entry_price=23100.0, trade_count=1)
            res_ok = FillResult(ok=True, filled_qty=1, avg_price=23100.0, status="Filled")
            verified = guard.post_trade_verify(s_open, gw_lag, 1, res_ok, "BUY", state_repo=repo, notifier=mock_notifier)
            self.assertEqual(verified.position, 1)
            self.assertEqual(len(gw_lag.orders_placed), 0)
            self.assertEqual(len(gw_lag.query_sequence), 0)

            # 情境二：策略已執行 EXIT 期望 0 口，但 3 次重試後券商仍殘留 +1 口 -> 立即強制補平
            gw_res = FakeBrokerGateway(fill_ok=True, fill_price=23090.0)
            gw_res.position_details = {"net": 1, "avg_price": 23100.0, "other_positions": [], "target_code": "TMFK6"}
            s_flat = TradingDayState(trading_date="2026-11-16", contract_code="TMFK6", position=0, trade_count=1)
            verified_flat = guard.post_trade_verify(s_flat, gw_res, 1, res_ok, "EXIT", state_repo=repo, notifier=mock_notifier)
            self.assertEqual(verified_flat.position, 0)
            self.assertTrue(verified_flat.entries_halted)
            self.assertEqual(len(gw_res.orders_placed), 1)
            self.assertEqual(gw_res.orders_placed[0]["action"], "SELL")
            self.assertEqual(gw_res.orders_placed[0]["octype"], "Cover")

    def test_process_closed_bars_skips_redundant_batch_indicators(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "paper")
            gw = FakeBrokerGateway()
            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False)
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1, base_cfg=cfg)
            fsm.rollover_day("2026-11-16")
            fsm.bar_aggregator.history_loaded = True

            # 已收盤 09:00 K 棒 + 進行中 09:01 K 棒
            fsm.bar_aggregator.update_tick(23050.0, 5, datetime.datetime(2026, 11, 16, 9, 0, 10))
            fsm.bar_aggregator.update_tick(23060.0, 5, datetime.datetime(2026, 11, 16, 9, 1, 5))

            import runtime.session_fsm as fsm_mod
            with patch.object(fsm_mod, "prepare_batch_indicators", wraps=fsm_mod.prepare_batch_indicators) as spy_prep:
                # 第一次 09:01:05 (有新收盤 K 棒 09:00) -> 計算 1 次
                fsm.step(datetime.datetime(2026, 11, 16, 9, 1, 5))
                self.assertEqual(spy_prep.call_count, 1)

                # 同分鐘內 09:01:20, 09:01:40 進新 Tick -> 無新收盤 K 棒，0 次重算！
                fsm.bar_aggregator.update_tick(23070.0, 2, datetime.datetime(2026, 11, 16, 9, 1, 20))
                fsm.step(datetime.datetime(2026, 11, 16, 9, 1, 20))
                fsm.bar_aggregator.update_tick(23080.0, 3, datetime.datetime(2026, 11, 16, 9, 1, 40))
                fsm.step(datetime.datetime(2026, 11, 16, 9, 1, 40))
                self.assertEqual(spy_prep.call_count, 1)

                # 換分鐘至 09:02:00 (09:01 收盤) -> 才再計算第 2 次
                fsm.bar_aggregator.update_tick(23085.0, 1, datetime.datetime(2026, 11, 16, 9, 2, 0))
                fsm.step(datetime.datetime(2026, 11, 16, 9, 2, 0))
                self.assertEqual(spy_prep.call_count, 2)

    def test_intraday_restart_does_not_replay_historical_orders_and_backfills_on_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            gw = FakeBrokerGateway(fill_ok=True)

            # 準備 08:45 ~ 10:04 的歷史 K 棒：包含 09:20 突破進場訊號、以及 08:50 低點 (不應誤觸停損)
            times = pd.date_range("2026-11-16 08:45:00", periods=80, freq="1min")
            rows = []
            for ts in times:
                hhmm = ts.strftime("%H:%M")
                if "08:45" <= hhmm <= "08:59":
                    px = 22950.0 if hhmm == "08:50" else 23050.0
                    rows.append((px + 10, px + 10, px - 10, px, 20))
                elif "09:00" <= hhmm <= "09:15":
                    rows.append((23050.0, 23100.0, 23000.0, 23050.0, 20))
                elif hhmm == "09:20":
                    rows.append((23090.0, 23130.0, 23085.0, 23125.0, 60))
                elif hhmm == "09:45":
                    # 09:40 進場後的波段高點 (+90)
                    rows.append((23160.0, 23190.0, 23150.0, 23170.0, 30))
                else:
                    rows.append((23130.0, 23140.0, 23120.0, 23135.0, 20))
            gw.kbars_df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=times)

            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False, use_vwap_ema_filter=False)

            # 情境 A：10:05 空手重啟 -> 載入歷史計算 ORB 與指標，但絕不拿 09:20 歷史訊號下單
            fsm_flat = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1, base_cfg=cfg)
            fsm_flat.rollover_day("2026-11-16")
            fsm_flat.step(datetime.datetime(2026, 11, 16, 10, 5, 10))
            self.assertTrue(fsm_flat.state.orb_calculated)
            self.assertTrue(fsm_flat.state.orb_finalized)
            self.assertEqual(fsm_flat.state.orb_high, 23100.0)
            self.assertEqual(len(gw.orders_placed), 0)

            # 斷線再透過 wake_and_login 重連 -> 會自動重啟 history_loaded 進行缺漏 K 棒回補，且超過 1 分鐘的斷線舊訊號不重播
            fsm_flat.logout_and_sleep("測試斷線")
            self.assertFalse(gw.connected)
            self.assertTrue(fsm_flat.wake_and_login("測試重連"))
            self.assertFalse(fsm_flat.bar_aggregator.history_loaded)
            outage_ts = pd.Timestamp("2026-11-16 10:05:00")
            recent_closed_ts = pd.Timestamp("2026-11-16 10:06:00")
            gw.kbars_df.loc[outage_ts] = [23140.0, 23148.0, 23135.0, 23142.0, 25]
            gw.kbars_df.loc[recent_closed_ts] = [23050.0, 23060.0, 23045.0, 23055.0, 20]
            fsm_flat.step(datetime.datetime(2026, 11, 16, 10, 7, 5))
            self.assertTrue(fsm_flat.bar_aggregator.has_bar(outage_ts))
            self.assertTrue(fsm_flat.bar_aggregator.has_bar(recent_closed_ts))
            self.assertEqual(len(gw.orders_placed), 0)

            # 情境 B：10:05 持多單 (09:40 @ 23100) 重啟接管 -> 不被 08:50 (22940) 誤觸停損，且最高浮盈回補 +90
            gw.position_details["net"] = 1
            fsm_hold = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1, base_cfg=cfg)
            fsm_hold.rollover_day("2026-11-16")
            fsm_hold.state = dataclasses.replace(
                fsm_hold.state,
                position=1,
                open_qty=1,
                entry_price=23100.0,
                entry_time=pd.Timestamp("2026-11-16 09:40:00"),
                trade_count=1,
            )
            fsm_hold.step(datetime.datetime(2026, 11, 16, 10, 5, 10))
            self.assertEqual(fsm_hold.state.position, 1)
            self.assertEqual(len(gw.orders_placed), 0)
            self.assertEqual(fsm_hold.state.highest_profit, 90.0)

    def test_force_close_1308_no_blind_spot_and_failsafe_mkt_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False)
            gw = FakeBrokerGateway(fill_ok=True, fill_price=23120.0)
            gw.position_details["net"] = 1

            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1, base_cfg=cfg)
            fsm.rollover_day("2026-11-16")
            fsm.bar_aggregator.history_loaded = True
            fsm.state = dataclasses.replace(
                fsm.state,
                position=1,
                open_qty=1,
                entry_price=23000.0,
                entry_time=pd.Timestamp("2026-11-16 09:20:00"),
                trade_count=1,
            )
            # 在 13:07:30 建立一根尚未進入 13:08 的前分鐘 K 棒 -> 到 13:08:00 應立刻觸發收盤前平倉，不需苦等 13:09
            fsm.bar_aggregator.update_tick(23120.0, 5, datetime.datetime(2026, 11, 16, 13, 7, 30))
            phase = fsm.step(datetime.datetime(2026, 11, 16, 13, 8, 0))
            self.assertEqual(phase, SessionPhase.FORCE_CLOSE)
            self.assertEqual(fsm.state.position, 0)
            self.assertEqual(len(gw.orders_placed), 1)
            self.assertEqual(gw.orders_placed[0]["action"], "SELL")
            self.assertEqual(gw.orders_placed[0]["octype"], "Cover")

            # 測試 13:09 enforce_failsafe_close: 前兩次 MKP 未成交 + 第 1 次查帳回傳 None 重連恢復後，第 3 次升級 MKT 成功補平
            repo = StateRepo(log_dir=tmp)
            guard = PositionGuard(log_dir=tmp, post_trade_retries=1, post_trade_retry_delay=0.0)
            gw_esc = FakeBrokerGateway(fill_ok=True, fill_price=23110.0)
            gw_esc.query_sequence = [
                {"net": None, "avg_price": 0.0, "other_positions": []},
                {"net": 1, "avg_price": 23000.0, "other_positions": []},
                {"net": 1, "avg_price": 23000.0, "other_positions": []},
            ]
            gw_esc.order_sequence = [
                FillResult(ok=False, filled_qty=0, avg_price=0.0, status="Cancelled"),
                FillResult(ok=True, filled_qty=1, avg_price=23110.0, status="Filled"),
            ]
            s_open = TradingDayState(trading_date="2026-11-16", contract_code="TMFK6", position=1, open_qty=1, entry_price=23000.0)
            s_done = guard.enforce_failsafe_close(s_open, gw_esc, state_repo=repo, max_attempts=3)
            self.assertEqual(s_done.position, 0)
            self.assertEqual(s_done.open_qty, 0)
            self.assertTrue(guard.last_failsafe_verified_flat)
            self.assertEqual([o["price_type"] for o in gw_esc.orders_placed], ["MKP", "MKT"])

    def test_multi_lot_partial_fill_and_periodic_reconcile_direction_qty_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            gw = FakeBrokerGateway(fill_ok=True, fill_price=23105.0)
            # 進場下 2 口僅部分成交 1 口 -> 不可被 post_trade_verify 誤平，且後續平倉只能送 1 口 Cover！
            gw.order_sequence = [
                FillResult(ok=False, filled_qty=1, avg_price=23105.0, status="Cancelled"),
                FillResult(ok=True, filled_qty=1, avg_price=23150.0, status="Filled"),
            ]
            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False)
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=2, base_cfg=cfg)
            fsm.rollover_day("2026-11-16")

            st_in, _ = fsm.execute_order_intent(
                OrderIntent(action="BUY", side_sign=1, price=23105.0, reason="PartialFillTest"),
                pd.Timestamp("2026-11-16 09:25:00"),
            )
            self.assertEqual(st_in.position, 1)
            self.assertEqual(st_in.open_qty, 1)
            self.assertEqual(len(gw.orders_placed), 1)
            self.assertEqual(gw.orders_placed[0]["qty"], 2)

            st_out, rec = fsm.execute_order_intent(
                OrderIntent(action="EXIT", side_sign=0, price=23150.0, reason="ExitExactQty", pnl_pts=45.0),
                pd.Timestamp("2026-11-16 09:30:00"),
            )
            self.assertIsNotNone(rec)
            self.assertEqual(st_out.position, 0)
            self.assertEqual(st_out.open_qty, 0)
            self.assertEqual(gw.orders_placed[-1]["qty"], 1)

            # 盤中定期對帳：偵測口數縮減 (2->1) 與方向衝突 (+1 變 -2)
            guard = fsm.position_guard
            s_qty = TradingDayState(trading_date="2026-11-16", contract_code="TMFK6", position=1, open_qty=2, entry_price=23100.0)
            gw.position_details = {"net": 1, "avg_price": 23100.0, "other_positions": []}
            s_qty = guard.periodic_reconcile(s_qty, gw, state_repo=fsm.state_repo)
            self.assertEqual(s_qty.open_qty, 1)

            gw.position_details = {"net": -2, "avg_price": 23200.0, "other_positions": []}
            s_flip = guard.periodic_reconcile(s_qty, gw, state_repo=fsm.state_repo)
            self.assertEqual(s_flip.position, -1)
            self.assertEqual(s_flip.open_qty, 2)
            self.assertTrue(s_flip.entries_halted)

    def test_pending_flatten_retries_until_flat_after_exit_or_sar_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            cfg = ORBStrategyConfig(use_atr_scaling=False, use_overnight_wall_filter=False, enable_sar=True)
            gw = FakeBrokerGateway(fill_ok=True, fill_price=23000.0)
            gw.position_details = {"net": 1, "avg_price": 23000.0, "other_positions": [], "target_code": "TMFK6"}
            # 1) SAR 第一腳平倉遭 MKP Cancelled -> 立起 _pending_flatten
            # 2) 次秒 _run_pending_flatten() 呼叫 enforce_failsafe_close(): MKP 再失敗後末試 MKT 成功歸零
            gw.order_sequence = [
                FillResult(ok=False, filled_qty=0, avg_price=0.0, status="Cancelled"),
                FillResult(ok=False, filled_qty=0, avg_price=0.0, status="Cancelled"),
                FillResult(ok=False, filled_qty=0, avg_price=0.0, status="Cancelled"),
                FillResult(ok=True, filled_qty=1, avg_price=22900.0, status="Filled"),
            ]
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1, base_cfg=cfg)
            fsm.position_guard = PositionGuard(log_dir=log_dir, post_trade_retries=1, post_trade_retry_delay=0.0)
            fsm.rollover_day("2026-11-16")
            fsm.bar_aggregator.history_loaded = True
            fsm.state = dataclasses.replace(
                fsm.state,
                position=1,
                open_qty=1,
                entry_price=23000.0,
                entry_time=pd.Timestamp("2026-11-16 09:20:00"),
                trade_count=1,
                orb_high=23000.0,
                orb_low=22950.0,
                orb_calculated=True,
                orb_finalized=True,
            )
            sar_intent = OrderIntent(
                action="SAR_SELL", side_sign=-1, price=22910.0, reason="TestSarFail", pnl_pts=-90.0, is_sar=True
            )
            st1, rec1 = fsm.execute_order_intent(sar_intent, pd.Timestamp("2026-11-16 09:30:00"))
            self.assertIsNone(rec1)
            self.assertEqual(st1.position, 1)
            self.assertTrue(fsm._pending_flatten)

            # 進入次秒 step() -> 即使非收盤時段或週六也能立即觸發不死鳥追平直到 0 口
            fsm.step(datetime.datetime(2026, 11, 16, 9, 30, 1))
            self.assertFalse(fsm._pending_flatten)
            self.assertEqual(fsm.state.position, 0)
            self.assertEqual(fsm.state.open_qty, 0)
            self.assertEqual(gw.orders_placed[-1]["price_type"], "MKT")
            self.assertEqual(gw.orders_placed[-1]["octype"], "Cover")

    def test_live_and_paper_gateway_connect_catches_login_exception_and_cleans_up(self):
        from config import ShioajiConfig
        from gateways.shioaji_live import ShioajiLiveGateway

        fake_api = MagicMock()
        fake_api.login.side_effect = RuntimeError("broker maintenance timeout")
        fake_sj_mod = MagicMock()
        fake_sj_mod.Shioaji.return_value = fake_api
        cfg = ShioajiConfig(api_key="k", secret_key="s", ca_path="", ca_passwd="", person_id="")

        with patch.dict(sys.modules, {"shioaji": fake_sj_mod}):
            live_gw = ShioajiLiveGateway(sj_cfg=cfg)
            self.assertFalse(live_gw.connect())
            self.assertFalse(live_gw.connected)
            self.assertIsNone(live_gw.api)

            paper_gw = ShioajiPaperGateway(sj_cfg=cfg)
            self.assertFalse(paper_gw.connect())
            self.assertFalse(paper_gw.connected)
            self.assertIsNone(paper_gw.api)

            # 若 login 成功但查無合約，需安全登出避免 Session 洩漏
            fake_api.login.side_effect = None
            with patch.object(live_gw, "resolve_contract", return_value=None):
                self.assertFalse(live_gw.connect())
                self.assertFalse(live_gw.connected)
                self.assertIsNone(live_gw.api)
                self.assertGreaterEqual(fake_api.logout.call_count, 1)

    def test_sync_intraday_history_early_open_skip_and_30s_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            log_dir = os.path.join(tmp, "logs", "live")
            gw = FakeBrokerGateway()
            fsm = SessionFSM(gateway=gw, base_dir=tmp, log_dir=log_dir, quantity=1)
            fsm.rollover_day("2026-11-16")

            with patch.object(gw, "fetch_kbars", wraps=gw.fetch_kbars) as spy_k:
                # 08:45:10 -> 首分鐘尚未收盤，不應狂打 fetch_kbars 且標記完成
                fsm.step(datetime.datetime(2026, 11, 16, 8, 45, 10))
                self.assertEqual(spy_k.call_count, 0)
                self.assertTrue(fsm.bar_aggregator.history_loaded)

                # 08:47:00 重置 history_loaded 後模擬回傳空表 -> 每 30 秒才重試 1 次，絕不每秒狂刷
                fsm.bar_aggregator.history_loaded = False
                fsm._last_history_sync_attempt_ts = 0.0
                fsm.step(datetime.datetime(2026, 11, 16, 8, 47, 0))
                fsm.step(datetime.datetime(2026, 11, 16, 8, 47, 1))
                fsm.step(datetime.datetime(2026, 11, 16, 8, 47, 15))
                self.assertEqual(spy_k.call_count, 1)

                fsm.step(datetime.datetime(2026, 11, 16, 8, 47, 31))
                self.assertEqual(spy_k.call_count, 2)


class TestV1V2BacktestParity(unittest.TestCase):
    def test_20day_exact_parity_with_v1(self):
        # 動態於獨立 namespace 載入 V1 BacktestEngine 以對拍 20 日每筆交易與損益
        v1_engine_path = os.path.join(V1_DIR, "backtester", "engine.py")
        if not os.path.exists(v1_engine_path):
            self.fail(f"V1 reference engine missing at {v1_engine_path}")

        data_dir = os.path.join(V2_DIR, "data")
        cache_path = os.path.join(data_dir, "tmf_20days_1m_kbars.csv")
        cached_df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        dates = sorted(cached_df["date"].unique())[-20:]
        daily_df = load_daily_history(data_dir)
        on_df = _preload_overnight_levels(data_dir)
        by_date = {d: g for d, g in cached_df.groupby("date", sort=False)}
        strat_cfg = ORBStrategyConfig()
        inst_cfg = InstrumentConfig()

        # 載入 V1 套件至隔離的 sys.path (於測試結束恢復)
        orig_sys_path = list(sys.path)
        saved_modules = {k: v for k, v in sys.modules.items() if k in ("config", "backtester", "backtester.engine", "backtester.metrics", "strategies", "strategies.orb_strategy", "strategies.atr_scaler")}
        try:
            for k in list(saved_modules):
                sys.modules.pop(k, None)
            sys.path.insert(0, V1_DIR)
            import backtester.engine as v1_engine_mod
            V1BacktestEngine = v1_engine_mod.BacktestEngine
        finally:
            sys.path[:] = orig_sys_path
            for k in ("config", "backtester", "backtester.engine", "backtester.metrics", "strategies", "strategies.orb_strategy", "strategies.atr_scaler"):
                sys.modules.pop(k, None)
            sys.modules.update(saved_modules)

        v1_trades_all = []
        v2_trades_all = []
        for d in dates:
            csv_path = os.path.join(data_dir, f"Daily_{d.replace('-', '_')}.csv")
            target_date = d.replace("-", "")
            day_df = by_date[d]

            e1 = V1BacktestEngine(csv_path, inst_config=inst_cfg, cached_kbars_df=day_df, daily_history_df=daily_df, overnight_levels_df=on_df)
            e1.load_taifex_csv(target_date=target_date, contract_month="AUTO")
            m1 = e1.run_orb_strategy(strat_cfg)

            e2 = V2BacktestEngine(csv_path, inst_config=inst_cfg, cached_kbars_df=day_df, daily_history_df=daily_df, overnight_levels_df=on_df)
            e2.load_taifex_csv(target_date=target_date, contract_month="AUTO")
            m2 = e2.run_orb_strategy(strat_cfg)

            self.assertEqual(m1["orb_high"], m2["orb_high"], f"{d} ORB High mismatch")
            self.assertEqual(m1["orb_low"], m2["orb_low"], f"{d} ORB Low mismatch")
            self.assertAlmostEqual(m1["total_points"], m2["total_points"], places=6, msg=f"{d} points mismatch")
            self.assertAlmostEqual(m1["net_profit_ntd"], m2["net_profit_ntd"], places=6, msg=f"{d} NTD mismatch")
            if not m1["trades_df"].empty:
                v1_trades_all.extend(m1["trades_df"][["entry_time", "exit_time", "action", "entry_price", "exit_price", "pnl_pts", "reason"]].to_dict("records"))
            if not m2["trades_df"].empty:
                v2_trades_all.extend(m2["trades_df"][["entry_time", "exit_time", "action", "entry_price", "exit_price", "pnl_pts", "reason"]].to_dict("records"))

        self.assertEqual(len(v1_trades_all), len(v2_trades_all))
        for t1, t2 in zip(v1_trades_all, v2_trades_all):
            self.assertEqual(t1, t2)


class TestUnifiedCLI(unittest.TestCase):
    def test_no_legacy_sh_scripts_and_tmf_executable(self):
        tmf_path = os.path.join(V2_DIR, "tmf")
        self.assertTrue(os.path.isfile(tmf_path), "缺少統一入口腳本 ./tmf")
        if os.name == "posix":
            self.assertTrue(os.access(tmf_path, os.X_OK), "./tmf 必須具備可執行權限 (+x)")

        leftover_sh = []
        skip_dirs = {"venv", ".venv", ".git", "__pycache__"}
        for root, dirs, files in os.walk(V2_DIR):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in files:
                if fn.endswith(".sh"):
                    leftover_sh.append(os.path.relpath(os.path.join(root, fn), V2_DIR))
        self.assertEqual(leftover_sh, [], f"V2 目錄不應殘留 .sh 腳本: {leftover_sh}")

    def test_tmf_help_and_unknown_subcommand(self):
        bash_bin = shutil.which("bash")
        if not bash_bin:
            self.skipTest("bash not available")
        tmf_path = os.path.join(V2_DIR, "tmf")

        expected_sections = [
            "統一控制入口 (./tmf)",
            "【1. 常駐機器人啟停與監控】",
            "【2. 盤中即時風控指令】",
            "【3. 健檢、查帳、回測與盤後更新】",
            "【4. Git 同步與部署】",
        ]
        for args in ([], ["help"], ["-h"], ["--help"]):
            res = subprocess.run(
                [bash_bin, tmf_path, *args],
                cwd=V2_DIR, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, f"./tmf {' '.join(args)} failed: {res.stderr}")
            for sec in expected_sections:
                self.assertIn(sec, res.stdout)

        bad = subprocess.run(
            [bash_bin, tmf_path, "nonexistent_cmd"],
            cwd=V2_DIR, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("未知指令", bad.stdout + bad.stderr)
        self.assertIn("統一控制入口 (./tmf)", bad.stdout)

        res_status = subprocess.run(
            [bash_bin, tmf_path, "status", "paper"],
            cwd=V2_DIR, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(res_status.returncode, 0, res_status.stderr)
        self.assertIn("[PAPER] 模式", res_status.stdout)

    def test_python_status_and_process_isolation(self):
        import main as v2_main

        v1_other = "/tmp/other_project/tmf_daytrading/main.py"
        v2_self = os.path.join(V2_DIR, "main.py")
        self.assertIsNone(v2_main._parse_mode_from_args(["python", v1_other, "--mode", "live"], base_dir=V2_DIR))
        self.assertEqual(v2_main._parse_mode_from_args(["python", v2_self, "--mode", "paper"], base_dir=V2_DIR), "paper")
        self.assertIsNone(v2_main._parse_mode_from_args(["python", v2_self, "--status", "--mode", "paper"], base_dir=V2_DIR))
        self.assertIsNone(v2_main._parse_mode_from_args(["python", v2_self, "--tail", "stdout", "--mode", "live"], base_dir=V2_DIR))

        today = datetime.date.today().strftime("%Y%m%d")
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "logs", "paper")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f"state_{today}.json"), "w", encoding="utf-8") as f:
                json.dump({"position": 1, "trade_count": 2, "entries_halted": False, "orb_low": 23100, "orb_high": 23180, "orb_calculated": True}, f)
            with open(os.path.join(log_dir, f"trader_{today}.log"), "w", encoding="utf-8") as f:
                f.write("2026-11-14 09:05:00 [INFO] ORB calculated\n")
            with open(os.path.join(log_dir, "stdout.log"), "w", encoding="utf-8") as f:
                f.write("2026-11-14 09:06:00 [ERROR] sample alert\n")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                v2_main.print_bot_status("paper", base_dir=tmpdir, pid=None)
            out = buf.getvalue()
            self.assertIn("持倉=1 | 成交=2", out)
            self.assertIn("ORB calculated", out)
            self.assertIn("sample alert", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
