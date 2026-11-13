# -*- coding: utf-8 -*-
"""
回歸與等價性測試：驗證程式精簡重構與執行加速路徑
涵蓋：
1. ORBStrategy 多空對稱評分、三階鎖利 (含預設 tier1_5 160/100)、順勢棒防呆、SAR 反手與 execute_values 等價性
2. BarAggregator structure_version / version、get_latest_bar_state、欄式建構 to_dataframe 與直接修改 _realtime_bars 相容性
3. 盤中增量純標量指標 (_compute_prefix_state + _compute_latest_indicators) 與 ORBStrategy.prepare_indicators 完全一致 (含零量能 K 棒)
4. 單日回測與模擬實盤熱路徑 (completed + intrabar scalar fast-path) 重播完全對齊
5. _check_external_command 單次 RPC 轉發、BacktestMetrics.max_drawdown_pts、append_order_audit 快取與縮檔偵測、TradeNotifier HTML 跳脫與 LINE 熔斷不阻斷 Gmail、load_env_file
"""

import os
import sys
import csv
import json
import time
import email
import tempfile
import datetime
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import InstrumentConfig, ORBStrategyConfig, load_env_file
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date, load_daily_history
from live.bar_aggregator import BarAggregator
from live.shioaji_trader import ShioajiTMFTrader
from live.execution import FillResult, append_order_audit
from live.notifier import TradeNotifier
from backtester.engine import BacktestEngine
from backtester.metrics import BacktestMetrics


class TestSpeedAndRefactor(unittest.TestCase):
    def setUp(self):
        self.cfg = ORBStrategyConfig()
        self.inst = InstrumentConfig()

    def test_orb_long_short_tier_symmetry_and_sar(self):
        """驗證多空對稱合併後的各階停利、順勢棒防誤觸、停損、SAR 反手，以及 execute_bar / execute_values 等價性"""
        ts1 = pd.Timestamp("2026-11-10 09:20:00")
        ts2 = pd.Timestamp("2026-11-10 09:21:00")

        # 1. 預設 tier1_5 (160 / 100) 與一階 (80 / 30) / 二階 (200 / 120) / 三階 (280, trail=60) 多空對稱測試
        for sign in (1, -1):
            for trig, exp_lock in [(90, 30.0), (170, 100.0), (220, 120.0)]:
                strat_bar = ORBStrategy(self.cfg, self.inst)
                strat_val = ORBStrategy(self.cfg, self.inst)
                for s in (strat_bar, strat_val):
                    s.position = sign
                    s.entry_price = 20000.0
                    s.entry_time = pd.Timestamp("2026-11-10 09:16:00")
                    s.trade_count = 1
                    s.highest_profit = float(trig)

                lock_px = 20000.0 + sign * exp_lock
                adverse_bar = {
                    "open": lock_px + sign * 5,
                    "high": 20000.0 + trig if sign == 1 else lock_px + 2.0,
                    "low": lock_px - 2.0 if sign == 1 else 20000.0 - trig,
                    "close": lock_px - sign * 1,
                    "vwap": 20000.0,
                    "ema9": 20000.0,
                    "ema21": 20000.0,
                }
                res_bar = strat_bar.execute_bar(ts1, pd.Series(adverse_bar))
                res_val = strat_val.execute_values(
                    ts1,
                    adverse_bar["open"],
                    adverse_bar["high"],
                    adverse_bar["low"],
                    adverse_bar["close"],
                    adverse_bar["vwap"],
                    adverse_bar["ema9"],
                    adverse_bar["ema21"],
                )
                self.assertEqual(res_bar["action"], res_val["action"])
                self.assertEqual(res_bar["action"], "EXIT")
                self.assertEqual(res_bar["record"]["pnl_pts"], exp_lock)
                self.assertEqual(res_val["record"]["pnl_pts"], exp_lock)
                self.assertEqual(res_bar["record"]["exit_price"], lock_px)

            # 三階波段移動停利 (trailing=280, distance=60 -> +240 點出場)
            strat_tr = ORBStrategy(self.cfg, self.inst)
            strat_tr.position = sign
            strat_tr.entry_price = 20000.0
            strat_tr.entry_time = pd.Timestamp("2026-11-10 09:16:00")
            strat_tr.trade_count = 1
            strat_tr.highest_profit = 300.0
            trail_stop = 20000.0 + sign * (300.0 - self.cfg.trailing_distance_pts)
            bar_tr = {
                "open": trail_stop + sign * 5,
                "high": trail_stop + 10 if sign == 1 else trail_stop + 1,
                "low": trail_stop - 1 if sign == 1 else trail_stop - 10,
                "close": trail_stop,
                "vwap": 20000.0,
                "ema9": 20000.0,
                "ema21": 20000.0,
            }
            res_tr = strat_tr.execute_bar(ts1, pd.Series(bar_tr))
            self.assertEqual(res_tr["action"], "EXIT")
            self.assertAlmostEqual(res_tr["record"]["pnl_pts"], 240.0)

        # 2. 同 K 棒順勢創高/創低防誤觸保護 (Favorable Candle Guard)
        for sign in (1, -1):
            strat_guard = ORBStrategy(self.cfg, self.inst)
            strat_guard.position = sign
            strat_guard.entry_price = 20000.0
            strat_guard.entry_time = pd.Timestamp("2026-11-10 09:16:00")
            strat_guard.trade_count = 1
            strat_guard.highest_profit = 10.0

            if sign == 1:
                r1 = strat_guard.execute_values(ts1, 20020.0, 20090.0, 20010.0, 20060.0, 20000.0, 20050.0, 20010.0)
            else:
                r1 = strat_guard.execute_values(ts1, 19980.0, 19990.0, 19910.0, 19940.0, 20000.0, 19950.0, 19990.0)
            self.assertEqual(r1["action"], "HOLD")
            self.assertEqual(strat_guard.position, sign)

        # 3. 初始停損 + 假突破反手 SAR (多轉空 SAR_SELL / 空轉多 SAR_BUY)
        for sign, expected_sar in ((1, "SAR_SELL"), (-1, "SAR_BUY")):
            strat_sar = ORBStrategy(self.cfg, self.inst)
            strat_sar.position = sign
            strat_sar.entry_price = 20000.0
            strat_sar.entry_time = pd.Timestamp("2026-11-10 09:16:00")
            strat_sar.trade_count = 1
            sl_px = 20000.0 - sign * self.cfg.stop_loss_pts
            res_sar = strat_sar.execute_values(
                ts2,
                o=20000.0,
                h=20010.0 if sign == 1 else sl_px + 5.0,
                l=sl_px - 5.0 if sign == 1 else 19990.0,
                c=sl_px - sign * 10.0,
                vwap=20000.0,
                ema9=20000.0,
                ema21=20000.0,
            )
            self.assertEqual(res_sar["action"], expected_sar)
            self.assertEqual(strat_sar.position, -sign)
            self.assertEqual(strat_sar.entry_price, sl_px)
            self.assertEqual(strat_sar.trade_count, 2)

    def test_bar_aggregator_structure_version_latest_state_and_columnar_df(self):
        """驗證 BarAggregator 版號區隔、O(1) 最新狀態查詢、欄式 DataFrame 建構與直接修改 _bars 相容性"""
        agg = BarAggregator()
        dt0 = datetime.datetime(2026, 11, 10, 8, 45, 5)
        agg.update_tick(23000.0, 2, dt0)
        v1, sv1 = agg.version, agg.structure_version
        self.assertEqual(sv1, 1)

        # 同分鐘內更新只增加 version，不應增加 structure_version
        agg.update_tick(23020.0, 3, datetime.datetime(2026, 11, 10, 8, 45, 30))
        self.assertGreater(agg.version, v1)
        self.assertEqual(agg.structure_version, sv1)

        # 跨分鐘新增 K 棒才增加 structure_version
        agg.update_tick(23015.0, 4, datetime.datetime(2026, 11, 10, 8, 46, 10))
        self.assertEqual(agg.structure_version, sv1 + 1)

        # get_latest_bar_state 必須精確對應 to_dataframe 最後一根
        state = agg.get_latest_bar_state("2026-11-10")
        self.assertIsNotNone(state)
        struct_sig, ver, bar_cnt, latest_ts, o, h, l, c, vol = state
        df = agg.to_dataframe("2026-11-10")
        self.assertEqual(bar_cnt, len(df))
        self.assertEqual(ver, agg.version)
        self.assertEqual(struct_sig[1], len(agg))
        self.assertEqual(latest_ts, df.index[-1])
        self.assertEqual((o, h, l, c, vol), (23015.0, 23015.0, 23015.0, 23015.0, 4))
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(df.index.name, "ts")

        # 外部直接寫入 agg._bars (向後相容 _realtime_bars 介面)，也應被正確感測到
        ext_ts = pd.Timestamp("2026-11-10 08:47:00")
        agg._bars[ext_ts] = {"open": 23030.0, "high": 23040.0, "low": 23025.0, "close": 23035.0, "volume": 9}
        state2 = agg.get_latest_bar_state("2026-11-10")
        self.assertEqual(state2[2], 3)
        self.assertEqual(state2[3], ext_ts)
        self.assertEqual(state2[7], 23035.0)
        df2 = agg.to_dataframe("2026-11-10")
        self.assertEqual(len(df2), 3)
        self.assertEqual(float(df2.iloc[-1]["close"]), 23035.0)

        # 空日期區間回傳 None / 空 DataFrame
        self.assertIsNone(agg.get_latest_bar_state("2026-11-11"))
        self.assertTrue(agg.to_dataframe("2026-11-11").empty)

    def test_incremental_indicators_parity_with_prepare_indicators(self):
        """驗證純標量增量指標 (_compute_prefix_state + _compute_latest_indicators) 與整表 prepare_indicators 數值完全一致"""
        strat = ORBStrategy(self.cfg, self.inst)
        times = pd.date_range("2026-11-10 08:45:00", periods=40, freq="1min")
        # 故意混入開盤零成交量 K 棒 (volume=0) 測試 VWAP 降級為 close 之一致性
        vols = [0, 0, 0] + [10 + (i * 7) % 50 for i in range(3, 40)]
        closes = [22500.0 + ((i * 13) % 41) - 20.0 + i * 2.5 for i in range(40)]
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c + 10.0 for c in closes],
                "low": [c - 10.0 for c in closes],
                "close": closes,
                "volume": vols,
            },
            index=times,
        )

        # 逐根前綴重算與純標量增量比對 (從第 2 根到第 40 根)
        for k in range(2, len(df) + 1):
            sub = df.iloc[:k].copy()
            ref_full = strat.prepare_indicators(sub)
            prefix = ShioajiTMFTrader._compute_prefix_state(ref_full)
            last_row = sub.iloc[-1]
            vwap_inc, ema9_inc, ema21_inc = ShioajiTMFTrader._compute_latest_indicators(
                prefix[0], prefix[1], prefix[2], prefix[3],
                float(last_row["close"]), float(last_row["volume"]),
            )
            self.assertAlmostEqual(vwap_inc, float(ref_full["vwap"].iloc[-1]), places=9)
            self.assertAlmostEqual(ema9_inc, float(ref_full["ema9"].iloc[-1]), places=9)
            self.assertAlmostEqual(ema21_inc, float(ref_full["ema21"].iloc[-1]), places=9)

            # 同分鐘內進行中價格/量能變動也必須與整表重算完全一致
            mutated = sub.copy()
            mutated.iloc[-1, mutated.columns.get_loc("close")] += 18.0
            mutated.iloc[-1, mutated.columns.get_loc("volume")] += 25
            ref_mut = strat.prepare_indicators(mutated)
            vwap_m, ema9_m, ema21_m = ShioajiTMFTrader._compute_latest_indicators(
                prefix[0], prefix[1], prefix[2], prefix[3],
                float(mutated.iloc[-1]["close"]), float(mutated.iloc[-1]["volume"]),
            )
            self.assertAlmostEqual(vwap_m, float(ref_mut["vwap"].iloc[-1]), places=9)
            self.assertAlmostEqual(ema9_m, float(ref_mut["ema9"].iloc[-1]), places=9)
            self.assertAlmostEqual(ema21_m, float(ref_mut["ema21"].iloc[-1]), places=9)

    def test_single_day_live_vs_backtest_replay_parity(self):
        """驗證實盤熱路徑 (completed + intrabar scalar fast-path) 與 BacktestEngine 單日回測完全一致"""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        kbar_path = os.path.join(base_dir, "data", "tmf_20days_1m_kbars.csv")
        daily_path = os.path.join(base_dir, "data", "history_daily_kbars.csv")
        on_path = os.path.join(base_dir, "data", "overnight_levels_summary.csv")

        cached_df = pd.read_csv(kbar_path, index_col=0, parse_dates=True)
        daily_df = load_daily_history(os.path.join(base_dir, "data"))
        on_df = pd.read_csv(on_path) if os.path.exists(on_path) else pd.DataFrame()
        if not on_df.empty and "trading_date" in on_df.columns:
            on_df["trading_date"] = on_df["trading_date"].astype(str).str.strip()

        # 選取 2026-10-20 (長紅波段獲利日) 與 2026-10-16 (初始停損日)
        for target_date in ("2026-10-20", "2026-10-16"):
            day_cfg, _ = config_for_date(self.cfg, os.path.join(base_dir, "data"), target_date, daily=daily_df)
            engine = BacktestEngine(
                kbar_path,
                inst_config=self.inst,
                cached_kbars_df=cached_df,
                daily_history_df=daily_df,
                overnight_levels_df=on_df,
            )
            day_bars = engine.load_taifex_csv(target_date=target_date.replace("-", ""), contract_month="AUTO")
            bt_res = engine.run_orb_strategy(self.cfg)
            bt_tdf = bt_res["trades_df"]
            bt_trades = bt_tdf.to_dict("records") if not bt_tdf.empty else []
            self.assertGreaterEqual(len(bt_trades), 1)

            with tempfile.TemporaryDirectory() as tmpdir:
                trader = ShioajiTMFTrader(api_key="", secret_key="", simulation=True)
                trader.notifier = MagicMock()
                trader.api = MagicMock()
                trader.contract = MagicMock()
                trader.log_dir = tmpdir
                trader.state_manager.state_file = os.path.join(tmpdir, "state.json")
                trader._save_state = MagicMock()
                trader.get_real_position = MagicMock(return_value=0)
                trader._post_trade_check = MagicMock()

                def _instant_fill(action, qty, octype="Auto", price=0.0, price_type="MKT", signal_price=None):
                    fill_px = float(price) if price > 0 else float(signal_price or 0.0)
                    if octype == "Cover" or trader._active_strategy.position == 0:
                        trader.get_real_position.return_value = 0
                    else:
                        trader.get_real_position.return_value = trader._active_strategy.position * trader.quantity
                    return FillResult(True, qty, fill_px, "Filled")

                trader._send_and_verify = _instant_fill
                live_strat = ORBStrategy(config=day_cfg, inst_config=self.inst)
                trader._active_strategy = live_strat
                if not on_df.empty and "trading_date" in on_df.columns:
                    row_on = on_df[on_df["trading_date"] == target_date]
                    if not row_on.empty:
                        live_strat.set_overnight_levels(
                            overnight_high=float(row_on.iloc[0]["night_high"]),
                            overnight_low=float(row_on.iloc[0]["night_low"]),
                        )

                processed_ts = set()
                prefix_state = None
                last_sig = None

                # 逐分鐘模擬結構變化重算，並在同分鐘內再走一次純標量熱路徑驗證完全一致性
                for i in range(len(day_bars)):
                    ts_i = day_bars.index[i]
                    row_i = day_bars.iloc[i]
                    trader.bar_aggregator._bars[ts_i] = {
                        "open": float(row_i["open"]),
                        "high": float(row_i["high"]),
                        "low": float(row_i["low"]),
                        "close": float(row_i["close"]),
                        "volume": int(row_i["volume"]),
                    }
                    state = trader.bar_aggregator.get_latest_bar_state(target_date)
                    sig = state[0]
                    self.assertNotEqual(sig, last_sig)
                    last_sig = sig
                    raw_df = trader.bar_aggregator.to_dataframe(target_date)
                    ind_df = live_strat.prepare_indicators(raw_df)
                    trader._evaluate_completed_and_intrabar(live_strat, ind_df, processed_ts)
                    prefix_state = trader._compute_prefix_state(ind_df) if len(ind_df) >= 2 else None

                    # 模擬同一分鐘後續 Tick 進入純標量 O(1) 熱路徑 (sig 不變)
                    if prefix_state is not None:
                        vwap, ema9, ema21 = trader._compute_latest_indicators(
                            prefix_state[0], prefix_state[1], prefix_state[2], prefix_state[3],
                            float(row_i["close"]), float(row_i["volume"]),
                        )
                        self.assertAlmostEqual(vwap, float(ind_df["vwap"].iloc[-1]), places=9)
                        self.assertAlmostEqual(ema9, float(ind_df["ema9"].iloc[-1]), places=9)
                        self.assertAlmostEqual(ema21, float(ind_df["ema21"].iloc[-1]), places=9)
                        trader._evaluate_intrabar_core(
                            live_strat, ts_i,
                            float(row_i["open"]), float(row_i["high"]), float(row_i["low"]), float(row_i["close"]),
                            vwap, ema9, ema21, processed_ts,
                        )

                # 補觸發最後一根已完成 K 棒檢查 (若最後一根未經下一分鐘收盤)
                final_raw = trader.bar_aggregator.to_dataframe(target_date)
                final_ind = live_strat.prepare_indicators(final_raw)
                last_ts = final_ind.index[-1]
                if last_ts not in processed_ts:
                    trader._finalize_orb_if_closed(live_strat, final_ind, last_ts)
                    prev_pos = live_strat.position
                    res = live_strat.execute_bar(last_ts, final_ind.iloc[-1], final_ind.iloc[-2] if len(final_ind) >= 2 else None)
                    trader._handle_action(live_strat, res, last_ts, final_ind.iloc[-1], prev_pos)

                self.assertEqual(len(live_strat.trades), len(bt_trades), f"Date {target_date} trade count mismatch")
                for lt, bt in zip(live_strat.trades, bt_trades):
                    self.assertEqual(lt["action"], bt["action"])
                    self.assertEqual(lt["entry_time"], pd.Timestamp(bt["entry_time"]))
                    self.assertEqual(lt["exit_time"], pd.Timestamp(bt["exit_time"]))
                    self.assertAlmostEqual(lt["entry_price"], float(bt["entry_price"]), places=6)
                    self.assertAlmostEqual(lt["exit_price"], float(bt["exit_price"]), places=6)
                    self.assertAlmostEqual(lt["pnl_pts"], float(bt["pnl_pts"]), places=6)

    def test_misc_refactor_guards(self):
        """驗證單次查詢轉發、指標 MDD 點數、審計 CSV 快取與縮檔偵測、通知跳脫與冷卻隔離、load_env_file"""
        # 1. _check_external_command 只應查一次 get_real_position() 並透傳給 _force_flatten_all
        trader = ShioajiTMFTrader(api_key="", secret_key="", simulation=True)
        trader.notifier = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            trader.log_dir = tmpdir
            trader.state_manager.state_file = os.path.join(tmpdir, "state.json")
            cmd_path = os.path.join(tmpdir, "command.json")
            with open(cmd_path, "w", encoding="utf-8") as f:
                json.dump({"cmd": "flatten"}, f)
            strat = ORBStrategy(self.cfg, self.inst)
            strat.position = 1
            strat.entry_price = 23000.0
            trader.get_real_position = MagicMock(return_value=1)
            trader.flatten_real_position = MagicMock(return_value=True)
            trader._check_external_command(strat, cur_price=23010.0)
            self.assertEqual(trader.get_real_position.call_count, 1)
            self.assertEqual(trader.flatten_real_position.call_count, 1)
            self.assertFalse(os.path.exists(cmd_path))
            self.assertEqual(strat.position, 0)

        # 2. BacktestMetrics.calculate 包含 max_drawdown_pts (含空清單與真實回撤)
        empty_m = BacktestMetrics.calculate([], self.inst)
        self.assertIn("max_drawdown_pts", empty_m)
        self.assertEqual(empty_m["max_drawdown_pts"], 0.0)
        sample_trades = [
            {"entry_price": 23000.0, "exit_price": 23100.0, "pnl_pts": 100.0},
            {"entry_price": 23100.0, "exit_price": 23040.0, "pnl_pts": -60.0},
            {"entry_price": 23040.0, "exit_price": 22990.0, "pnl_pts": -50.0},
            {"entry_price": 23000.0, "exit_price": 23200.0, "pnl_pts": 200.0},
        ]
        m = BacktestMetrics.calculate(sample_trades, self.inst)
        self.assertAlmostEqual(m["max_drawdown_pts"], 110.0)
        self.assertEqual(m["total_points"], 190.0)

        # 3. append_order_audit 快取與同路徑縮小/舊格式切換偵測
        with tempfile.TemporaryDirectory() as tmpdir:
            now_dt = datetime.datetime(2026, 11, 10, 9, 30, 0)
            res = FillResult(True, 1, 23050.0, "Filled")
            append_order_audit(tmpdir, "BUY", 1, "New", res, signal_price=23048.0, now_dt=now_dt)
            append_order_audit(tmpdir, "SELL", 1, "Cover", res, signal_price=23090.0, now_dt=now_dt)
            csv_path = os.path.join(tmpdir, "orders_20261110.csv")
            with open(csv_path, "r", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(rows[0]), 9)
            self.assertEqual(rows[2][8], "23090")

            # 覆寫成較小的 8 欄舊檔 (測試 size shrink 讓快取失效並重新辨識)
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "action", "qty", "octype", "status", "filled_qty", "avg_price", "msg"])
                w.writerow(["09:15:00", "BUY", 1, "New", "Filled", 1, "23000", "old"])
            append_order_audit(tmpdir, "SELL", 1, "Cover", res, signal_price=23099.0, now_dt=now_dt)
            with open(csv_path, "r", encoding="utf-8") as f:
                rows_legacy = list(csv.reader(f))
            self.assertEqual(len(rows_legacy), 3)
            self.assertEqual(len(rows_legacy[2]), 8)

        # 4. TradeNotifier: LINE 429 熔斷不阻塞後續 Gmail，且 HTML 內容正確跳脫
        notifier = TradeNotifier()
        notifier.notify_email = "test@example.com"
        notifier.gmail_user = "test@example.com"
        notifier.gmail_app_password = "app-password"
        notifier.tg_token = ""
        notifier.tg_chat_id = ""
        notifier.discord_webhook = ""
        notifier.line_token = ""
        notifier.line_channel_access_token = "tok"
        notifier.line_user_id = "uid"
        notifier._line_cooldown_until = 0.0

        import urllib.error
        http_429 = urllib.error.HTTPError("https://api.line.me", 429, "Too Many Requests", {}, None)
        sent_raws = []

        class _DummySMTP:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def starttls(self): pass
            def login(self, u, p): pass
            def sendmail(self, from_addr, to_addrs, msg_str): sent_raws.append(msg_str)

        with patch("urllib.request.urlopen", side_effect=http_429) as mock_url, \
             patch("smtplib.SMTP", _DummySMTP):
            notifier._send_sync("alert <script>x&y</script>")
            self.assertGreater(notifier._line_cooldown_until, time.time())
            self.assertEqual(mock_url.call_count, 1)
            self.assertEqual(len(sent_raws), 1)
            # 第二次仍在冷卻期：不得呼叫 LINE URL，但仍須正常寄出 Gmail
            notifier._send_sync("second <tag>")
            self.assertEqual(mock_url.call_count, 1)
            self.assertEqual(len(sent_raws), 2)
            # 驗證 HTML 跳脫 (text/html part 含 &lt;script&gt; 且不含裸 <script>)
            parsed = email.message_from_string(sent_raws[0])
            html_part = parsed.get_payload()[1].get_payload(decode=True).decode("utf-8")
            self.assertIn("&lt;script&gt;x&amp;y&lt;/script&gt;", html_part)
            self.assertNotIn("<script>", html_part)

        # 5. load_env_file(override=False) 不覆寫已存在環境變數
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".env") as tf:
            tf.write('# comment\nTMF_TEST_KEEP="from_file"\nTMF_TEST_NEW=\'hello\'\n')
            env_file = tf.name
        try:
            with patch.dict(os.environ, {"TMF_TEST_KEEP": "original_val"}, clear=False):
                os.environ.pop("TMF_TEST_NEW", None)
                load_env_file(env_path=env_file, override=False)
                self.assertEqual(os.environ["TMF_TEST_KEEP"], "original_val")
                self.assertEqual(os.environ["TMF_TEST_NEW"], "hello")
                load_env_file(env_path=env_file, override=True)
                self.assertEqual(os.environ["TMF_TEST_KEEP"], "from_file")
        finally:
            os.remove(env_file)


if __name__ == "__main__":
    unittest.main()
