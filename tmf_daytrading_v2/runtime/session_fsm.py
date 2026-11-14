"""
日內 9 階段狀態機與 24/7 常駐調度引擎 (Session FSM)
"""
import dataclasses
import datetime
import logging
import signal
import threading
import time
from typing import Any, Callable, Optional, Tuple

import pandas as pd

from config import ORBStrategyConfig, atomic_write_csv
from core.atr_scaler import describe
from core.indicators import OnlineIndicators, calculate_orb_levels_from_df, prepare_batch_indicators
from core.models import Bar, EnrichedBar, OrderIntent, SessionPhase, TradeRecord, TradingDayState
from core.orb_strategy import (
    apply_exit_only,
    apply_intent_fill,
    evaluate_bar,
    with_orb_levels,
    with_overnight_levels,
)
from gateways.base import BrokerGateway
from gateways.data_repo import (
    StateRepo,
    append_order_audit,
    config_for_date,
    fetch_cnyes_day_kbars,
    load_command,
    load_or_fetch_overnight_levels,
)
from runtime.bar_aggregator import BarAggregator
from runtime.health_check import run_health_check
from runtime.notifier import TradeNotifier
from runtime.position_guard import PositionGuard, build_0915_orb_report, max_trades_for_config

logger = logging.getLogger("TMFTrader.SessionFSM")


def determine_session_phase(
    now_dt: datetime.datetime,
    cfg: ORBStrategyConfig,
) -> SessionPhase:
    """
    將當日時間精準映射至 9 個互斥的交易階段：
      1. PREMARKET_STANDBY : < 08:35:00
      2. HEALTH_CHECK      : 08:35:00 ~ 08:44:59
      3. PRE_ORB_BUILD     : 08:45:00 ~ 08:59:59
      4. ORB_BUILD         : 09:00:00 ~ 09:00:59
      5. EARLY_BREAKOUT    : 09:01:00 ~ 09:15:59 (含 09:15 ORB 鎖定與早盤 3A 強勢突破)
      6. MAIN_BREAKOUT     : 09:16:00 ~ entry_end_time (11:30:00)
      7. POST_ENTRY_HOLD   : entry_end_time < t < force_close_time (13:08:00)
      8. FORCE_CLOSE       : force_close_time (13:08:00) ~ shutdown_time (13:10:00)
      9. POST_CLOSE_SETTLE : > shutdown_time (13:10:00)
    """
    t = now_dt.strftime("%H:%M:%S")
    if t < "08:35:00":
        return SessionPhase.PREMARKET_STANDBY
    if t < cfg.pre_orb_start_time:
        return SessionPhase.HEALTH_CHECK
    if t < cfg.orb_start_time:
        return SessionPhase.PRE_ORB_BUILD
    if t < "09:01:00":
        return SessionPhase.ORB_BUILD
    if t < cfg.entry_start_time:
        return SessionPhase.EARLY_BREAKOUT
    if t <= cfg.entry_end_time:
        return SessionPhase.MAIN_BREAKOUT
    if t < cfg.force_close_time:
        return SessionPhase.POST_ENTRY_HOLD
    if t <= cfg.shutdown_time:
        return SessionPhase.FORCE_CLOSE
    return SessionPhase.POST_CLOSE_SETTLE


class SessionFSM:
    """台指期當沖 24/7 狀態機調度器"""

    def __init__(
        self,
        gateway: BrokerGateway,
        base_dir: str,
        log_dir: str,
        quantity: int = 1,
        base_cfg: Optional[ORBStrategyConfig] = None,
        notifier: Optional[TradeNotifier] = None,
        bar_aggregator: Optional[BarAggregator] = None,
        position_guard: Optional[PositionGuard] = None,
        state_repo: Optional[StateRepo] = None,
    ):
        self.gateway = gateway
        self.base_dir = base_dir
        self.data_dir = f"{base_dir}/data"
        self.log_dir = log_dir
        self.command_path = f"{log_dir}/command.json"
        self.quantity = int(quantity)
        self.base_cfg = base_cfg or ORBStrategyConfig()
        self.notifier = notifier or TradeNotifier()
        self.bar_aggregator = bar_aggregator or BarAggregator()
        self.position_guard = position_guard or PositionGuard(log_dir=log_dir)
        self.state_repo = state_repo or StateRepo(log_dir=log_dir)
        self.online_ind = OnlineIndicators()

        self._stop_requested = False
        self._wake_event = threading.Event()
        self._processed_timestamps: set = set()
        self._intrabar_sar_state: Optional[dict] = None
        self._closed_bar_count: int = 0
        self.state: TradingDayState = self.rollover_day(datetime.date.today().strftime("%Y-%m-%d"))

    @staticmethod
    def determine_phase(now_dt: datetime.datetime, cfg: Optional[ORBStrategyConfig] = None) -> SessionPhase:
        return determine_session_phase(now_dt, cfg or ORBStrategyConfig())

    def rollover_day(self, new_date_str: str) -> TradingDayState:
        """
        換日至新交易日：直接建立全新 TradingDayState 實例，
        從結構上確保所有當日部位與排程旗標一次歸零，同時依當日日期重算 ATR 參數與重置 K 棒聚合器。
        """
        eff_cfg, atr_info = config_for_date(self.base_cfg, self.data_dir, new_date_str)
        c_code = getattr(self.gateway, "contract_code", "") or ""
        oh, ol, _ = load_or_fetch_overnight_levels(
            self.data_dir,
            new_date_str,
            gateway=self.gateway if self.gateway.connected else None,
            allow_cnyes_fallback=True,
        )
        new_state = TradingDayState(
            trading_date=new_date_str,
            contract_code=c_code,
            overnight_high=oh,
            overnight_low=ol,
            effective_config=eff_cfg,
            atr_info=atr_info,
        )
        self.state = new_state
        self.bar_aggregator.clear()
        self.online_ind.reset()
        self._processed_timestamps.clear()
        self._intrabar_sar_state = None
        self._closed_bar_count = 0
        return new_state

    def execute_order_intent(
        self,
        intent: OrderIntent,
        bar_ts: pd.Timestamp,
        current_bar: Optional[Bar] = None,
    ) -> Tuple[TradingDayState, Optional[TradeRecord]]:
        """將策略純產出的 OrderIntent 轉送 BrokerGateway 下單，成交後才推進 TradingDayState"""
        cfg = self.state.effective_config or self.base_cfg
        max_trades = max_trades_for_config(cfg)

        if intent.action == "HOLD":
            self.state, _ = apply_intent_fill(self.state, intent)
            return self.state, None

        if intent.action in ("BUY", "SELL"):
            res = self.gateway.place_order(
                action=intent.action,
                qty=self.quantity,
                price_type="MKP",
                octype="New",
                price=intent.price,
            )
            append_order_audit(self.log_dir, intent.action, self.quantity, "New", res, signal_price=intent.price)
            if not res.ok and res.filled_qty <= 0:
                logger.error(f"❌ 進場委託未成交 ({intent.action}): {res.status}，保持空手不更新策略狀態。")
                return self.state, None

            fill_px = res.avg_price if res.avg_price > 0 else intent.price
            self.state, _ = apply_intent_fill(self.state, intent, fill_price=fill_px, fill_time=bar_ts)
            msg = (
                f"{'🟢 多單進場' if intent.side_sign == 1 else '🔴 空單進場'} ({self.quantity}口 @ {fill_px:.0f})\n"
                f"原因: {intent.reason}"
            )
            logger.info(msg)
            self.notifier.send(msg, async_send=True)
            self.state_repo.save_state(self.state)
            return self.state, None

        if intent.action == "EXIT":
            close_act = "SELL" if self.state.position == 1 else "BUY"
            res = self.gateway.place_order(
                action=close_act,
                qty=self.quantity,
                price_type="MKP",
                octype="Cover",
                price=intent.price,
            )
            append_order_audit(self.log_dir, close_act, self.quantity, "Cover", res, signal_price=intent.price)
            if not res.ok and res.filled_qty <= 0:
                logger.error(f"❌ 平倉委託未成交 ({close_act}): {res.status}")
                return self.state, None

            fill_px = res.avg_price if res.avg_price > 0 else intent.price
            self.state, rec = apply_intent_fill(self.state, intent, fill_price=fill_px, fill_time=bar_ts)
            if self.state.trade_count >= max_trades:
                self.state = dataclasses.replace(self.state, entries_halted=True)
            pnl_pts = rec.pnl_pts if rec else 0.0
            msg = f"🏁 [策略平倉] {close_act} {self.quantity}口 @ {fill_px:.0f} ({pnl_pts:+.0f}點)\n原因: {intent.reason}"
            logger.info(msg)
            self.notifier.send(msg, async_send=True)
            self.state_repo.save_state(self.state)
            return self.state, rec

        if intent.action in ("SAR_BUY", "SAR_SELL"):
            close_act = "SELL" if self.state.position == 1 else "BUY"
            close_res = self.gateway.place_order(
                action=close_act,
                qty=self.quantity,
                price_type="MKP",
                octype="Cover",
                price=intent.price,
            )
            append_order_audit(self.log_dir, close_act, self.quantity, "SAR_Cover", close_res, signal_price=intent.price)
            if not close_res.ok and close_res.filled_qty <= 0:
                logger.error(f"❌ SAR 第一腳平倉未成交 ({close_act}): {close_res.status}")
                return self.state, None

            close_px = close_res.avg_price if close_res.avg_price > 0 else intent.price
            rev_act = "BUY" if intent.side_sign == 1 else "SELL"
            rev_res = self.gateway.place_order(
                action=rev_act,
                qty=self.quantity,
                price_type="MKP",
                octype="New",
                price=close_px,
            )
            append_order_audit(self.log_dir, rev_act, self.quantity, "SAR_New", rev_res, signal_price=close_px)
            if rev_res.ok or rev_res.filled_qty > 0:
                rev_px = rev_res.avg_price if rev_res.avg_price > 0 else close_px
                self.state, rec = apply_intent_fill(
                    self.state,
                    intent,
                    fill_price=close_px,
                    fill_time=bar_ts,
                    new_entry_price=rev_px,
                )
                msg = f"🔄 [SAR 假突破反手] 先平倉 @ {close_px:.0f} ({rec.pnl_pts:+.0f}點)，立即反手 {rev_act} @ {rev_px:.0f}"
            else:
                self.state, rec = apply_exit_only(self.state, intent, fill_price=close_px, fill_time=bar_ts)
                msg = f"⚠️ [SAR 部分執行] 原倉已平 @ {close_px:.0f}，但反手開新倉失敗 ({rev_res.status})，轉空手。"
            logger.warning(msg)
            self.notifier.send(msg, async_send=True)
            self.state_repo.save_state(self.state)
            return self.state, rec

        return self.state, None

    def _process_closed_bars(self, df_day: pd.DataFrame, now_dt: datetime.datetime) -> None:
        """依已收盤 1 分 K 棒更新 ORB 並逐根呼叫純策略評估"""
        if df_day.empty:
            return
        cfg = self.state.effective_config or self.base_cfg
        now_min = pd.Timestamp(now_dt.replace(second=0, microsecond=0))
        df_ind = prepare_batch_indicators(df_day)
        closed_df = df_ind[df_ind.index < now_min]
        if closed_df.empty:
            return

        # 先更新 O(1) 線上指標至已收盤最後一根，供分鐘內即時風控使用
        if self._closed_bar_count != len(closed_df):
            self.online_ind = OnlineIndicators.from_dataframe(closed_df)
            self._closed_bar_count = len(closed_df)

        # 更新前導 / 現貨開盤區間
        self.state = with_orb_levels(self.state, df_day, cfg)
        if not self.state.orb_finalized and now_dt.strftime("%H:%M:%S") >= cfg.orb_end_time:
            if self.state.orb_calculated:
                self.state = dataclasses.replace(self.state, orb_finalized=True)
                if not self.state.orb_report_sent:
                    _, report_msg = build_0915_orb_report(self.state, cfg)
                    logger.info(report_msg)
                    self.notifier.send(report_msg, async_send=True)
                    self.state = dataclasses.replace(self.state, orb_report_sent=True)
                    self.state_repo.save_state(self.state)

        for ts, row in closed_df.iterrows():
            if ts in self._processed_timestamps:
                continue
            bar = Bar(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            eb = EnrichedBar(
                bar=bar,
                vwap=float(row["vwap"]),
                ema9=float(row["ema9"]),
                ema21=float(row["ema21"]),
            )
            intent = evaluate_bar(self.state, eb, cfg)
            self.execute_order_intent(intent, ts, bar)
            self._processed_timestamps.add(ts)

    def _evaluate_open_intrabar(self, today_str: str, now_dt: datetime.datetime) -> None:
        """持倉期間於分鐘尚未收線前以 O(1) OnlineIndicators 即時檢查停損、鎖利、移停與反手"""
        if self.state.position == 0:
            return
        latest = self.bar_aggregator.get_latest_bar_state(today_str)
        if latest is None:
            return
        _, _, _, latest_ts, o, h, l, c, v = latest
        now_min = pd.Timestamp(now_dt.replace(second=0, microsecond=0))
        if latest_ts < now_min or latest_ts in self._processed_timestamps:
            return

        if self._intrabar_sar_state is not None and self._intrabar_sar_state.get("ts") == latest_ts:
            sar_s = self._intrabar_sar_state
            if c > sar_s["high"]:
                sar_s["high"] = c
            if c < sar_s["low"]:
                sar_s["low"] = c
            o = sar_s["high"] if self.state.position == 1 else sar_s["low"]
            h = max(sar_s["high"], c)
            l = min(sar_s["low"], c)
        else:
            self._intrabar_sar_state = None

        vwap, ema9, ema21 = self.online_ind.peek(c, float(v))
        eb = EnrichedBar(
            bar=Bar(timestamp=latest_ts, open=o, high=h, low=l, close=c, volume=float(v)),
            vwap=vwap,
            ema9=ema9,
            ema21=ema21,
        )
        cfg = self.state.effective_config or self.base_cfg
        intent = evaluate_bar(self.state, eb, cfg)
        if intent.action == "HOLD":
            self.state, _ = apply_intent_fill(self.state, intent)
            return

        self.execute_order_intent(intent, latest_ts, eb.bar)
        if intent.action in ("SAR_BUY", "SAR_SELL") and self.state.position != 0:
            self._intrabar_sar_state = {"ts": latest_ts, "high": c, "low": c}
        elif self.state.position == 0:
            self._processed_timestamps.add(latest_ts)

    def _handle_command_if_any(self) -> None:
        cmd = load_command(self.command_path)
        if not cmd:
            return
        act = str(cmd.get("cmd") or cmd.get("action") or "").lower()
        if act in ("close", "flatten"):
            logger.warning("⚠️ 收到外部指令: 立即強制平倉！")
            self.state = self.position_guard.enforce_failsafe_close(
                self.state, self.gateway, notifier=self.notifier, state_repo=self.state_repo
            )
        elif act == "close_and_halt":
            logger.warning("⚠️ 收到外部指令: 立即強制平倉並停開新倉！")
            self.state = self.position_guard.enforce_failsafe_close(
                self.state, self.gateway, notifier=self.notifier, state_repo=self.state_repo
            )
            self.state = dataclasses.replace(self.state, entries_halted=True)
            self.state_repo.save_state(self.state)
        elif act == "halt":
            self.state = dataclasses.replace(self.state, entries_halted=True)
            self.state_repo.save_state(self.state)
            logger.warning("🛑 收到外部指令: 今日停開新倉！")
        elif act == "resume":
            self.state = dataclasses.replace(self.state, entries_halted=False)
            self.state_repo.save_state(self.state)
            logger.info("▶️ 收到外部指令: 恢復允許進場。")

    def step(self, now_dt: Optional[datetime.datetime] = None) -> SessionPhase:
        """執行單次主狀態機迴圈邏輯 (便於整合與單元測試確定性驅動)"""
        now = now_dt or datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self.state.trading_date:
            logger.info(f"📅 偵測跨日 ({self.state.trading_date} -> {today_str})，執行狀態重設與滾動準備...")
            self.state = self.rollover_day(today_str)

        cfg = self.state.effective_config or self.base_cfg
        phase = self.determine_phase(now, cfg)

        self._handle_command_if_any()

        if now.weekday() >= 5:
            return phase

        if phase == SessionPhase.HEALTH_CHECK and not self.state.health_check_sent:
            res = run_health_check(
                mode="paper" if self.gateway.is_simulation else "live",
                qty=self.quantity,
                send_notify=False,
                api=getattr(self.gateway, "api", None),
                contract=self.gateway.contract,
                ca_activated=self.gateway.ca_activated,
            )
            if res.get("report_message"):
                self.notifier.send(res["report_message"], async_send=True)
            self.state = dataclasses.replace(self.state, health_check_sent=True, morning_wake_logged=True)
            return phase

        if phase in (
            SessionPhase.PRE_ORB_BUILD,
            SessionPhase.ORB_BUILD,
            SessionPhase.EARLY_BREAKOUT,
            SessionPhase.MAIN_BREAKOUT,
            SessionPhase.POST_ENTRY_HOLD,
        ):
            now_ts = pd.Timestamp(now.replace(second=0, microsecond=0))
            if not self.bar_aggregator.history_loaded and self.gateway.connected:
                hist_df = self.gateway.fetch_kbars(today_str, today_str, timeout=5000)
                if not hist_df.empty:
                    self.bar_aggregator.load_history_kbars(hist_df, today_str=today_str, now_min=now_ts)

            if self.bar_aggregator.needs_snapshot_sync(now_ts) and self.gateway.connected:
                snap = self.gateway.fetch_snapshot_price(today_str)
                if snap is not None:
                    self.bar_aggregator.update_snapshot(snap[0], snap[1], snap[2])

            df_day = self.bar_aggregator.to_dataframe(today_str=today_str)
            self._process_closed_bars(df_day, now)
            self._evaluate_open_intrabar(today_str, now)
            return phase

        if phase == SessionPhase.FORCE_CLOSE:
            if now.strftime("%H:%M:%S") >= cfg.failsafe_close_time:
                self.state = self.position_guard.enforce_failsafe_close(
                    self.state, self.gateway, notifier=self.notifier, state_repo=self.state_repo
                )
            else:
                df_day = self.bar_aggregator.to_dataframe(today_str=today_str)
                self._process_closed_bars(df_day, now)
            return phase

        if phase == SessionPhase.POST_CLOSE_SETTLE and not self.state.post_close_done:
            self.state = self.position_guard.enforce_failsafe_close(
                self.state, self.gateway, notifier=self.notifier, state_repo=self.state_repo
            )
            self.state = dataclasses.replace(self.state, post_close_done=True)
            self.state_repo.save_state(self.state)
        return phase

    def run(self, poll_seconds: float = 1.0) -> None:
        """24/7 常駐主執行迴圈"""
        def _stop_handler(signum=None, frame=None):
            self._stop_requested = True
            self._wake_event.set()

        signal.signal(signal.SIGINT, _stop_handler)
        signal.signal(signal.SIGTERM, _stop_handler)

        if not self.gateway.connect():
            logger.error("❌ 無法連線至 Shioaji，無法啟動盤中監控。")
            return

        self.state = dataclasses.replace(
            self.state,
            contract_code=getattr(self.gateway, "contract_code", ""),
        )

        def _on_tick(p: float, v: int, dt: datetime.datetime):
            self.bar_aggregator.update_tick(p, v, dt)
            if self.state.position != 0:
                self._wake_event.set()

        self.gateway.subscribe_ticks(_on_tick)
        self.state = self.position_guard.startup_reconcile(
            self.state,
            self.gateway,
            self.quantity,
            state_repo=self.state_repo,
            notifier=self.notifier,
            cfg=self.state.effective_config or self.base_cfg,
        )

        eff_cfg = self.state.effective_config or self.base_cfg
        logger.info(f"🚀 V2 交易引擎啟動成功 | 合約: {self.state.contract_code} | {describe(eff_cfg, self.state.atr_info or {})}")

        last_reconcile = time.time()
        try:
            while not self._stop_requested:
                phase = self.step()
                if time.time() - last_reconcile >= 60.0 and phase in (
                    SessionPhase.PRE_ORB_BUILD,
                    SessionPhase.ORB_BUILD,
                    SessionPhase.EARLY_BREAKOUT,
                    SessionPhase.MAIN_BREAKOUT,
                    SessionPhase.POST_ENTRY_HOLD,
                ):
                    self.state = self.position_guard.periodic_reconcile(
                        self.state,
                        self.gateway,
                        state_repo=self.state_repo,
                        notifier=self.notifier,
                        cfg=self.state.effective_config or self.base_cfg,
                    )
                    last_reconcile = time.time()
                self._wake_event.clear()
                if not self._stop_requested:
                    self._wake_event.wait(timeout=poll_seconds)
        finally:
            self.state_repo.save_state(self.state)
            self.gateway.close()
