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
import datetime
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

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
from runtime.position_guard import PositionGuard
from runtime.session_fsm import SessionFSM


class FakeBrokerGateway(BrokerGateway):
    """純記憶體假券商配接器，供單元測試使用"""

    def __init__(self, fill_ok: bool = True, fill_price: float = 0.0):
        self.is_simulation = False
        self.connected = True
        self.ca_activated = True
        self.contract = type("C", (), {"code": "TMFK6", "name": "微台202611"})()
        self.contract_code = "TMFK6"
        self.fill_ok = fill_ok
        self.fill_price = fill_price
        self.orders_placed: List[Dict[str, Any]] = []
        self.resolve_calls: List[Tuple[str, bool]] = []
        self.contracts_map: Dict[str, Any] = {"TMFK6": self.contract, "TMFR1": self.contract}
        self.position_details: Dict[str, Any] = {
            "net": 0,
            "avg_price": 0.0,
            "direction": "Flat",
            "positions": [],
            "other_positions": [],
            "target_code": "TMFK6",
        }

    def connect(self, contract_code: str = "TMFR1") -> bool:
        self.connected = True
        return True

    def reconnect(self, contract_code: str = "TMFR1") -> bool:
        return True

    def resolve_contract(self, code: str = "TMFR1", allow_fallback: bool = True) -> Any:
        self.resolve_calls.append((code, allow_fallback))
        if code in self.contracts_map:
            return self.contracts_map[code]
        return self.contract if allow_fallback else None

    def subscribe_ticks(self, on_tick_cb) -> bool:
        return True

    def fetch_kbars(self, start_date: str, end_date: str, timeout: int = 5000) -> pd.DataFrame:
        return pd.DataFrame()

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
        if not self.fill_ok:
            return FillResult(ok=False, filled_qty=0, avg_price=0.0, status="Cancelled")
        px = self.fill_price if self.fill_price > 0 else price
        return FillResult(ok=True, filled_qty=qty, avg_price=px, status="Filled")

    def query_position_details(self, contract_code: Optional[str] = None) -> Dict[str, Any]:
        return dict(self.position_details)

    def cancel_pending_orders(self) -> None:
        pass

    def close(self) -> None:
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
