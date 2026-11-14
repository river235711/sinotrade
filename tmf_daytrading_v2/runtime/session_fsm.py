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
from gateways.base import BrokerGateway, FillResult
from gateways.data_repo import (
    StateRepo,
    append_order_audit,
    config_for_date,
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
        eod_roll_fn: Optional[Callable[[str], bool]] = None,
        holiday_grace_seconds: float = 30.0,
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
        self._eod_roll_fn = eod_roll_fn
        self.holiday_grace_seconds = float(holiday_grace_seconds)
        self.online_ind = OnlineIndicators()

        self._stop_requested = False
        self._wake_event = threading.Event()
        self._processed_timestamps: set = set()
        self._intrabar_sar_state: Optional[dict] = None
        self._closed_bar_count: int = 0
        self._holiday_empty_since_ts: Optional[float] = None
        self.state: TradingDayState = self.rollover_day(datetime.date.today().strftime("%Y-%m-%d"))

    @staticmethod
    def determine_phase(now_dt: datetime.datetime, cfg: Optional[ORBStrategyConfig] = None) -> SessionPhase:
        return determine_session_phase(now_dt, cfg or ORBStrategyConfig())

    def _on_tick(self, price: float, volume: int, dt: datetime.datetime) -> None:
        self.bar_aggregator.update_tick(price, volume, dt)
        if self.state.position != 0:
            self._wake_event.set()

    def logout_and_sleep(self, reason: str = "") -> None:
        """非交易時段或休市日安全登出券商 API，進入離線深度省電休眠"""
        if not self.gateway.connected:
            return
        logger.info(f"💤 [{reason}] 安全登出券商 API，進入離線深度省電睡眠...")
        self.gateway.close()

    def wake_and_login(self, reason: str = "") -> bool:
        """盤前 / 盤中喚醒：重新登入、重解析當月主力合約、重訂閱 Tick 並同步夜盤與部位"""
        if self.gateway.connected:
            return True
        logger.info(f"🌅 [{reason}] 正在喚醒並重新登入券商 API、重抓主力合約...")
        if not self.gateway.connect():
            logger.error(f"❌ [{reason}] 喚醒登入失敗，將於下一輪重試。")
            return False
        self.gateway.subscribe_ticks(self._on_tick)
        c_code = getattr(self.gateway, "contract_code", "") or self.state.contract_code
        oh, ol, _ = load_or_fetch_overnight_levels(
            self.data_dir,
            self.state.trading_date,
            gateway=self.gateway if self.gateway.connected else None,
            allow_cnyes_fallback=True,
        )
        updates: dict = {"contract_code": c_code, "morning_wake_logged": True}
        if oh is not None:
            updates["overnight_high"] = oh
        if ol is not None:
            updates["overnight_low"] = ol
        self.state = dataclasses.replace(self.state, **updates)
        self.state = self.position_guard.startup_reconcile(
            self.state,
            self.gateway,
            self.quantity,
            state_repo=self.state_repo,
            notifier=self.notifier,
            cfg=self.state.effective_config or self.base_cfg,
        )
        logger.info(f"✅ [{reason}] 券商重連與合約同步完成！合約={self.state.contract_code}")
        return True

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
        self._holiday_empty_since_ts = None
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
            self.state = self.position_guard.post_trade_verify(
                self.state,
                self.gateway,
                self.quantity,
                res,
                intent.action,
                state_repo=self.state_repo,
                notifier=self.notifier,
            )
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
            self.state = self.position_guard.post_trade_verify(
                self.state,
                self.gateway,
                self.quantity,
                res,
                intent.action,
                state_repo=self.state_repo,
                notifier=self.notifier,
            )
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
                verify_res = rev_res
                msg = f"🔄 [SAR 假突破反手] 先平倉 @ {close_px:.0f} ({rec.pnl_pts:+.0f}點)，立即反手 {rev_act} @ {rev_px:.0f}"
            else:
                self.state, rec = apply_exit_only(self.state, intent, fill_price=close_px, fill_time=bar_ts)
                verify_res = close_res
                msg = f"⚠️ [SAR 部分執行] 原倉已平 @ {close_px:.0f}，但反手開新倉失敗 ({rev_res.status})，轉空手。"
            logger.warning(msg)
            self.notifier.send(msg, async_send=True)
            self.state_repo.save_state(self.state)
            self.state = self.position_guard.post_trade_verify(
                self.state,
                self.gateway,
                self.quantity,
                verify_res,
                intent.action,
                state_repo=self.state_repo,
                notifier=self.notifier,
            )
            return self.state, rec

        return self.state, None

    def _emit_orb_report(
        self,
        cfg: ORBStrategyConfig,
        updated: bool = False,
        prev_h: Optional[float] = None,
        prev_l: Optional[float] = None,
    ) -> None:
        """發送 09:15 ORB 初次確立或 09:16 完整收盤更新通知"""
        if updated and prev_h is not None and prev_l is not None:
            logger.info(
                f"🔄 [09:15 完整收盤更新 ORB] 區間由 {prev_l:.0f}~{prev_h:.0f} "
                f"更新為 {self.state.orb_low:.0f}~{self.state.orb_high:.0f} "
                f"(振幅={self.state.orb_high - self.state.orb_low:.0f}點，對齊回測)"
            )
        _, report_msg = build_0915_orb_report(self.state, cfg)
        if updated:
            report_msg = report_msg.replace(
                "📌 [09:15 ORB 區間確立]",
                "📌 [09:15 ORB 完整收盤更新]",
                1,
            )
        logger.info("\n" + report_msg)
        self.notifier.send(report_msg, async_send=True)
        self.state = dataclasses.replace(self.state, orb_report_sent=True)
        self.state_repo.save_state(self.state)

    def _process_closed_bars(self, df_day: pd.DataFrame, now_dt: datetime.datetime) -> None:
        """依已收盤 1 分 K 棒更新 ORB (09:15 初次鎖定 + 09:16 收線二次校正) 並僅於新 K 棒收盤時重算指標"""
        if df_day.empty:
            return
        cfg = self.state.effective_config or self.base_cfg
        now_str = now_dt.strftime("%H:%M:%S")
        today_str = now_dt.strftime("%Y-%m-%d")
        now_min = pd.Timestamp(now_dt.replace(second=0, microsecond=0))
        orb_end_ts = pd.Timestamp(f"{today_str} {cfg.orb_end_time}")

        closed_raw = df_day[df_day.index < now_min]
        need_orb_prelim = (now_str >= cfg.orb_end_time and not self.state.orb_calculated)
        need_orb_finalize = (
            not self.state.orb_finalized
            and now_min > orb_end_ts
            and self.state.orb_calculated
        )
        has_new_closed = (
            not closed_raw.empty
            and (
                len(closed_raw) != self._closed_bar_count
                or closed_raw.index[-1] not in self._processed_timestamps
            )
        )
        if not (need_orb_prelim or need_orb_finalize or has_new_closed):
            return

        df_ind = prepare_batch_indicators(df_day)
        closed_df = df_ind[df_ind.index < now_min]

        if not closed_df.empty and self._closed_bar_count != len(closed_df):
            self.online_ind = OnlineIndicators.from_dataframe(closed_df)
            self._closed_bar_count = len(closed_df)

        # 與回測／V1 嚴格同步：09:15:00 初次鎖定 ORB，09:16:00 待 09:15 K 棒完整收盤再做二次校正定稿
        if need_orb_prelim:
            orb_slice = df_day if now_min <= orb_end_ts else df_day[df_day.index <= orb_end_ts]
            self.state = with_orb_levels(self.state, orb_slice, cfg)
            if self.state.orb_calculated:
                if now_min > orb_end_ts:
                    self.state = dataclasses.replace(self.state, orb_finalized=True)
                if not self.state.orb_report_sent:
                    self._emit_orb_report(cfg, updated=False)
                else:
                    self.state_repo.save_state(self.state)
        elif need_orb_finalize:
            prev_h, prev_l = self.state.orb_high, self.state.orb_low
            was_sent = self.state.orb_report_sent
            orb_slice = df_day[df_day.index <= orb_end_ts]
            updated_state = with_orb_levels(self.state, orb_slice, cfg)
            self.state = dataclasses.replace(updated_state, orb_finalized=True)
            changed = (
                prev_h is not None
                and prev_l is not None
                and (self.state.orb_high != prev_h or self.state.orb_low != prev_l)
            )
            if changed or not was_sent:
                self._emit_orb_report(
                    cfg,
                    updated=(changed and was_sent),
                    prev_h=prev_h,
                    prev_l=prev_l,
                )
            else:
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

    def _run_eod_roll(self, today_str: str) -> None:
        """13:45:15 日盤正式收盤後自動執行滾動 20/60 日資料庫更新與明日 ATR 參數預載通知"""
        logger.info(f"🏁 日盤正式收盤 (13:45:15)！開始執行今日 ({today_str}) 盤後數據滾動與結算報告...")
        roll_ok = False
        try:
            if self._eod_roll_fn is not None:
                roll_ok = bool(self._eod_roll_fn(today_str))
            else:
                from runtime.update_20days import roll_20days
                roll_ok = bool(roll_20days(target_date=today_str, push_git=True))
        except Exception as roll_err:
            logger.warning(f"⚠️ 盤後自動滾動更新 20 日資料異常: {roll_err}")

        next_cfg, next_info = config_for_date(self.base_cfg, self.data_dir, "9999-12-31")
        logger.info(f"📐 下一交易日預載參數: {describe(next_cfg, next_info)} (roll_ok={roll_ok})")
        self.notifier.send(
            f"🏁 [13:45 日盤正式收盤總結]\n"
            f"今日 ({today_str}) 日盤正式收盤！\n"
            f"• 20/60 日滾動更新狀態: {'✅ 完成' if roll_ok else '⚠️ 略過或資料不足'}\n"
            f"• 下一交易日預載 ATR 參數:\n  {describe(next_cfg, next_info)}",
            async_send=True,
        )

    def step(self, now_dt: Optional[datetime.datetime] = None) -> SessionPhase:
        """執行單次主狀態機迴圈邏輯 (便於整合與單元測試確定性驅動)"""
        now = now_dt or datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        now_str = now.strftime("%H:%M:%S")
        if today_str != self.state.trading_date:
            logger.info(f"📅 偵測跨日 ({self.state.trading_date} -> {today_str})，執行狀態重設與滾動準備...")
            self.state = self.rollover_day(today_str)

        cfg = self.state.effective_config or self.base_cfg
        phase = self.determine_phase(now, cfg)

        self._handle_command_if_any()

        if now.weekday() >= 5:
            if self.gateway.connected and self.state.position == 0:
                self.logout_and_sleep("週末休市")
            return phase

        if phase == SessionPhase.PREMARKET_STANDBY:
            if now_str < "08:30:00" and self.gateway.connected and self.state.position == 0:
                self.logout_and_sleep("盤前離線待命")
            elif now_str >= "08:30:00" and not self.gateway.connected:
                self.wake_and_login("08:30 開盤前自動喚醒")
            return phase

        if phase == SessionPhase.HEALTH_CHECK and not self.state.health_check_sent:
            if not self.gateway.connected:
                self.wake_and_login("08:35 健檢前自動喚醒")
            if self.gateway.connected:
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
                self.state_repo.save_state(self.state)
            elif not self.state.offline_hc_alert_sent:
                self.state = dataclasses.replace(self.state, offline_hc_alert_sent=True)
                self.state_repo.save_state(self.state)
                self.notifier.send("⚠️ [08:35 開盤前健檢延後] 尚未連上券商，將於連線恢復後補做完整健檢！", async_send=True)
            return phase

        if phase in (
            SessionPhase.PRE_ORB_BUILD,
            SessionPhase.ORB_BUILD,
            SessionPhase.EARLY_BREAKOUT,
            SessionPhase.MAIN_BREAKOUT,
            SessionPhase.POST_ENTRY_HOLD,
        ):
            if self.state.holiday_detected:
                return phase

            if not self.gateway.connected:
                self.wake_and_login("盤中自動重連")

            now_ts = pd.Timestamp(now.replace(second=0, microsecond=0))
            if not self.bar_aggregator.history_loaded and self.gateway.connected:
                hist_df = self.gateway.fetch_kbars(today_str, today_str, timeout=5000)
                if not hist_df.empty:
                    self.bar_aggregator.load_history_kbars(hist_df, today_str=today_str, now_min=now_ts)

            if self.bar_aggregator.needs_snapshot_sync(now_ts) and self.gateway.connected:
                snap = self.gateway.fetch_snapshot_price(today_str)
                if snap is not None:
                    self.bar_aggregator.update_snapshot(snap[0], snap[1], snap[2])

            if (
                self.gateway.connected
                and len(self.bar_aggregator) == 0
                and now_str >= "09:05:00"
                and self.state.position == 0
            ):
                now_epoch = now.timestamp()
                if self._holiday_empty_since_ts is None:
                    self._holiday_empty_since_ts = now_epoch
                if now_epoch - self._holiday_empty_since_ts >= self.holiday_grace_seconds:
                    logger.info(f"🏖️ [{now_str}] 今日 ({today_str}) 已過 09:05 且無任何日盤開盤行情，判定休市並深度休眠！")
                    self.state = dataclasses.replace(self.state, holiday_detected=True, entries_halted=True)
                    self.state_repo.save_state(self.state)
                    self.notifier.send(
                        f"🏖️ [休市自動偵測] {today_str}\n"
                        f"已過 09:05 仍無任何日盤成交行情，今日不執行任何交易，已登出進入離線深度睡眠。",
                        async_send=True,
                    )
                    self.logout_and_sleep("國定假日/休市日 09:05 無報價休眠")
                return phase
            self._holiday_empty_since_ts = None

            df_day = self.bar_aggregator.to_dataframe(today_str=today_str)
            self._process_closed_bars(df_day, now)
            self._evaluate_open_intrabar(today_str, now)
            return phase

        if phase == SessionPhase.FORCE_CLOSE:
            if now_str >= cfg.failsafe_close_time:
                self.state = self.position_guard.enforce_failsafe_close(
                    self.state, self.gateway, notifier=self.notifier, state_repo=self.state_repo
                )
            else:
                df_day = self.bar_aggregator.to_dataframe(today_str=today_str)
                self._process_closed_bars(df_day, now)
            return phase

        if phase == SessionPhase.POST_CLOSE_SETTLE:
            if self.state.holiday_detected:
                if self.gateway.connected and self.state.position == 0:
                    self.logout_and_sleep("休市日收盤待機")
                return phase

            if not self.state.post_close_done:
                self.state = self.position_guard.enforce_failsafe_close(
                    self.state, self.gateway, notifier=self.notifier, state_repo=self.state_repo
                )
                if self.state.position == 0:
                    self.state = dataclasses.replace(self.state, post_close_done=True)
                    self.notifier.send(
                        f"🛡️ [13:10 當沖平倉完成] {today_str} 帳上部位確認 0 口，零過夜風險，等待 13:45:15 數據結算。",
                        async_send=True,
                    )
                self.state_repo.save_state(self.state)

            if now_str >= "13:45:15" and not self.state.eod_roll_done and self.state.position == 0:
                self.state = dataclasses.replace(self.state, eod_roll_done=True)
                self.state_repo.save_state(self.state)
                self._run_eod_roll(today_str)

            if now_str >= "13:50:00" and self.gateway.connected and self.state.position == 0:
                self.logout_and_sleep("盤後收盤休眠")
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

        c_code = getattr(self.gateway, "contract_code", "")
        updates: dict = {"contract_code": c_code}
        if self.state.overnight_high is None or self.state.overnight_low is None:
            oh, ol, _ = load_or_fetch_overnight_levels(
                self.data_dir,
                self.state.trading_date,
                gateway=self.gateway,
                allow_cnyes_fallback=True,
            )
            if oh is not None:
                updates["overnight_high"] = oh
            if ol is not None:
                updates["overnight_low"] = ol
        self.state = dataclasses.replace(self.state, **updates)

        self.gateway.subscribe_ticks(self._on_tick)
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
        last_heartbeat = time.time()
        active_phases = (
            SessionPhase.PRE_ORB_BUILD,
            SessionPhase.ORB_BUILD,
            SessionPhase.EARLY_BREAKOUT,
            SessionPhase.MAIN_BREAKOUT,
            SessionPhase.POST_ENTRY_HOLD,
        )
        try:
            while not self._stop_requested:
                phase = self.step()
                now_ts = time.time()
                if now_ts - last_reconcile >= 60.0 and phase in active_phases and self.gateway.connected:
                    self.state = self.position_guard.periodic_reconcile(
                        self.state,
                        self.gateway,
                        state_repo=self.state_repo,
                        notifier=self.notifier,
                        cfg=self.state.effective_config or self.base_cfg,
                    )
                    last_reconcile = now_ts
                if now_ts - last_heartbeat >= 900.0 and phase in active_phases and self.gateway.connected:
                    self.gateway.keep_alive()
                    last_heartbeat = now_ts
                self._wake_event.clear()
                if not self._stop_requested:
                    self._wake_event.wait(timeout=poll_seconds)
        finally:
            self.state_repo.save_state(self.state)
            self.gateway.close()
