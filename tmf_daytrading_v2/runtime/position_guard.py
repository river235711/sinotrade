"""
部位守護與對帳引擎 (Position Guard)
職責：
1. 啟動帳務核對與斷線接管 (以券商帳上部位為唯一真實依據)
2. 盤中定時對帳，偵測外部手動平倉或幽靈部位
3. 跨月/換約殘倉嚴格清理 (resolve_contract 必須 allow_fallback=False，未解析嚴禁退回 TMFR1 下單)
4. 13:09 安全底線死守平倉 (Failsafe Flatten) 與 09:15 ORB 即時提示報告生成
"""
import dataclasses
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config import ORBStrategyConfig
from core.models import TradingDayState
from gateways.base import BrokerGateway, FillResult
from gateways.data_repo import StateRepo, append_order_audit
from runtime.notifier import TradeNotifier

logger = logging.getLogger("TMFTrader.PositionGuard")


def max_trades_for_config(cfg: Any) -> int:
    """計算策略設定允許的當日最大進場次數上限"""
    max_t = getattr(cfg, "max_trades_per_day", None)
    if max_t is not None:
        return int(max_t)
    return int(getattr(cfg, "max_initial_trades", 1)) + (1 if getattr(cfg, "enable_sar", False) else 0)


def _format_stop_and_profit_lines(cfg: ORBStrategyConfig, direction: int, ref_price: float) -> str:
    sl = float(cfg.stop_loss_pts)
    b1_t, b1_l = float(cfg.breakeven_trigger_pts), float(cfg.breakeven_lock_pts)
    t15_t = float(getattr(cfg, "tier1_5_trigger_pts", 160.0))
    t15_l = float(getattr(cfg, "tier1_5_lock_pts", 100.0))
    t2_t, t2_l = float(cfg.tier2_trigger_pts), float(cfg.tier2_lock_pts)
    tr_a, tr_d = float(cfg.trailing_activation_pts), float(cfg.trailing_distance_pts)

    sign = 1.0 if direction == 1 else -1.0
    sl_px = ref_price - sign * sl
    b1_trig_px = ref_price + sign * b1_t
    b1_lock_px = ref_price + sign * b1_l
    t15_trig_px = ref_price + sign * t15_t
    t15_lock_px = ref_price + sign * t15_l
    t2_trig_px = ref_price + sign * t2_t
    t2_lock_px = ref_price + sign * t2_l
    tr_act_px = ref_price + sign * tr_a
    tr_floor_px = ref_price + sign * (tr_a - tr_d)

    return (
        f"  • 停損價: {sl_px:.0f} (-{sl:.0f}點)\n"
        f"  • 一階保本: 觸 {b1_trig_px:.0f} (+{b1_t:.0f}) → 鎖 {b1_lock_px:.0f} (+{b1_l:.0f})\n"
        f"  • 中階鎖利: 觸 {t15_trig_px:.0f} (+{t15_t:.0f}) → 鎖 {t15_lock_px:.0f} (+{t15_l:.0f})\n"
        f"  • 二階鎖利: 觸 {t2_trig_px:.0f} (+{t2_t:.0f}) → 鎖 {t2_lock_px:.0f} (+{t2_l:.0f})\n"
        f"  • 三階移停: 觸 {tr_act_px:.0f} (+{tr_a:.0f}) → 回撤 {tr_d:.0f} 點 (保底 {tr_floor_px:.0f} / +{tr_a - tr_d:.0f})"
    )


def build_0915_orb_report(
    state: TradingDayState,
    cfg: Optional[ORBStrategyConfig] = None,
) -> Tuple[str, str]:
    """依 TradingDayState 與當日有效參數產出 09:15 ORB 鎖定即時通知"""
    eff_cfg = cfg or state.effective_config or ORBStrategyConfig()
    orb_h = float(state.orb_high)
    orb_l = float(state.orb_low)
    orb_range = orb_h - orb_l
    max_orb = float(getattr(eff_cfg, "max_orb_range_pts", 320.0))
    max_chase = float(getattr(eff_cfg, "max_breakout_chase_pts", 60.0))
    max_init = int(getattr(eff_cfg, "max_initial_trades", 1))
    entry_start = str(getattr(eff_cfg, "entry_start_time", "09:16:00"))[:5]
    entry_end = str(getattr(eff_cfg, "entry_end_time", "11:30:00"))[:5]

    buy_th = orb_h
    sell_th = orb_l
    wall_lines = []
    if getattr(eff_cfg, "use_overnight_wall_filter", False):
        thresh_pts = float(getattr(eff_cfg, "overnight_wall_threshold_pts", 40.0))
        if (
            state.overnight_high is not None
            and orb_h < state.overnight_high
            and (state.overnight_high - orb_h) <= thresh_pts
        ):
            buy_th = float(state.overnight_high)
            wall_lines.append(f"• 🛡️ 啟動做多防撞牆: ORB上界距夜高僅 {buy_th - orb_h:.0f} 點，需突破夜高 {buy_th:.0f} 才開多！")
        if (
            state.overnight_low is not None
            and orb_l > state.overnight_low
            and (orb_l - state.overnight_low) <= thresh_pts
        ):
            sell_th = float(state.overnight_low)
            wall_lines.append(f"• 🛡️ 啟動做空防撞地: ORB下界距夜低僅 {orb_l - sell_th:.0f} 點，需跌破夜低 {sell_th:.0f} 才開空！")
        if not wall_lines and state.overnight_high is not None and state.overnight_low is not None:
            wall_lines.append(f"• 🌙 夜盤參考水位: 夜高 {state.overnight_high:.0f} / 夜低 {state.overnight_low:.0f} (距離安全)")

    wall_block = ("\n" + "\n".join(wall_lines)) if wall_lines else ""
    header = (
        f"📌 [09:15 ORB 區間確立]\n"
        f"現貨開盤 15 分鐘關鍵區間已鎖定！\n"
        f"• 原始 ORB 區間: {orb_l:.0f} ~ {orb_h:.0f} (振幅 {orb_range:.0f} 點 / 上限 {max_orb:.0f} 點){wall_block}"
    )

    sl = float(eff_cfg.stop_loss_pts)
    b1_t, b1_l = float(eff_cfg.breakeven_trigger_pts), float(eff_cfg.breakeven_lock_pts)
    t15_t = float(getattr(eff_cfg, "tier1_5_trigger_pts", 160.0))
    t15_l = float(getattr(eff_cfg, "tier1_5_lock_pts", 100.0))
    t2_t, t2_l = float(eff_cfg.tier2_trigger_pts), float(eff_cfg.tier2_lock_pts)
    tr_a, tr_d = float(eff_cfg.trailing_activation_pts), float(eff_cfg.trailing_distance_pts)
    pts_spec = (
        f"停損 -{sl:.0f} | 一階 +{b1_t:.0f}→+{b1_l:.0f} | 中階 +{t15_t:.0f}→+{t15_l:.0f} | "
        f"二階 +{t2_t:.0f}→+{t2_l:.0f} | 三階 +{tr_a:.0f}(回撤{tr_d:.0f})"
    )

    if state.position != 0 and state.entry_price > 0:
        dir_txt = "🟢多單" if state.position == 1 else "🔴空單"
        levels_txt = _format_stop_and_profit_lines(eff_cfg, state.position, state.entry_price)
        msg = (
            f"{header}\n"
            f"• 📢 進場狀態: ✅ 已於 09:15 前早盤強勢突破進場 ({dir_txt} @ {state.entry_price:.0f}，今日不再開新倉)\n"
            f"• 🎯 今日點數規格: {pts_spec}\n"
            f"• 📍 實際持倉停損與各階停利價位 (依成交價 {state.entry_price:.0f} 計算):\n{levels_txt}"
        )
        return "IN_POSITION", msg

    no_entry_reasons = []
    if orb_range > max_orb:
        no_entry_reasons.append(f"ORB 振幅 {orb_range:.0f} 點超過上限 {max_orb:.0f} 點 ({entry_start} 後不追突破)")
    if state.entries_halted:
        no_entry_reasons.append("目前已設定停開新倉")
    if state.trade_count >= max_init:
        no_entry_reasons.append(f"今日首次進場次數已滿 ({state.trade_count}/{max_init})")

    if no_entry_reasons:
        msg = (
            f"{header}\n"
            f"• 📢 進場判斷: 🚫 今日不進場\n"
            f"• ℹ️ 原因說明: {'；'.join(no_entry_reasons)}，今日空手觀望。"
        )
        return "NO_ENTRY", msg

    long_levels = _format_stop_and_profit_lines(eff_cfg, 1, buy_th)
    short_levels = _format_stop_and_profit_lines(eff_cfg, -1, sell_th)
    msg = (
        f"{header}\n"
        f"• 📢 進場判斷: ✅ 可進場 ({entry_start} ~ {entry_end} 符合空間上限)\n"
        f"• 突破觸發區間: 做多 {buy_th:.0f}~{buy_th + max_chase:.0f} / 做空 {sell_th - max_chase:.0f}~{sell_th:.0f} (+ VWAP/EMA 共振)\n"
        f"• 🎯 今日 ATR 點數規格: {pts_spec}\n"
        f"• 📈 若做多 (以界線 {buy_th:.0f} 試算，實際以成交價為準):\n{long_levels}\n"
        f"• 📉 若做空 (以界線 {sell_th:.0f} 試算，實際以成交價為準):\n{short_levels}"
    )
    return "CAN_ENTER", msg


class PositionGuard:
    """啟動對帳、盤中定期對帳、成交後 3x0.5s 重試核對、跨月嚴格清倉與 13:09 收盤死守平倉防護模組"""

    def __init__(
        self,
        log_dir: Optional[str] = None,
        post_trade_retries: int = 3,
        post_trade_retry_delay: float = 0.5,
    ):
        self.log_dir = log_dir
        self.post_trade_retries = max(0, int(post_trade_retries))
        self.post_trade_retry_delay = float(post_trade_retry_delay)
        self.last_failsafe_verified_flat: bool = False

    def post_trade_verify(
        self,
        state: TradingDayState,
        gateway: BrokerGateway,
        expected_qty: int,
        last_result: FillResult,
        action_tag: str,
        state_repo: Optional[StateRepo] = None,
        notifier: Optional[TradeNotifier] = None,
    ) -> TradingDayState:
        """
        下單成交後即時核對券商帳上部位，防範 Shioaji 帳務更新延遲 (最多 3 次 × 0.5 秒重試)。
        若確有殘倉、方向或口數不符，立即觸發安全補平。
        """
        expected_net = int(state.position) * int(expected_qty)
        target_code = state.contract_code or gateway.contract_code
        details = gateway.query_position_details(contract_code=target_code)
        real_net = details.get("net")

        if real_net is not None and real_net != expected_net and not gateway.is_simulation:
            for _ in range(self.post_trade_retries):
                if self.post_trade_retry_delay > 0:
                    time.sleep(self.post_trade_retry_delay)
                details = gateway.query_position_details(contract_code=target_code)
                real_net = details.get("net")
                if real_net is None or real_net == expected_net:
                    break

        if real_net is None or real_net == expected_net:
            if real_net == expected_net:
                logger.info(f"📒 成交後對帳通過 ({action_tag}): 帳上 {real_net:+d} 口 = 預期 {expected_net:+d} 口")
            return state

        logger.error(f"📒 成交後對帳不符 ({action_tag}): 帳上 {real_net:+d} 口，策略預期 {expected_net:+d} 口")

        if expected_net == 0 and real_net != 0:
            logger.critical(
                f"🚨 [成交後對帳異常] 平倉後券商帳上仍有殘留部位 ({real_net:+d} 口)，立即啟動強制補平！"
            )
            new_state = self.enforce_failsafe_close(state, gateway, notifier=notifier, state_repo=state_repo)
            if not new_state.entries_halted:
                new_state = dataclasses.replace(new_state, entries_halted=True)
                if state_repo:
                    state_repo.save_state(new_state)
            return new_state

        if expected_net != 0 and real_net == 0:
            if last_result.ok and last_result.status == "Filled" and last_result.filled_qty >= int(expected_qty):
                logger.warning(
                    f"⚠️ 成交回報已確認 Filled ({last_result.filled_qty}口 @ {last_result.avg_price:.0f})，"
                    f"但帳務查詢仍顯示 0 口，保留策略持倉並等待定期對帳同步！"
                )
                return state
            flat_state = dataclasses.replace(
                state,
                position=0,
                open_qty=0,
                entry_price=0.0,
                entry_time=None,
                highest_profit=0.0,
                hp_before_bar_ts=None,
                hp_before_bar_val=0.0,
                entries_halted=True,
            )
            if state_repo:
                state_repo.save_state(flat_state)
            return flat_state

        # 其他不一致 (方向相反或口數不符)：為安全起見強制平倉並停開新倉
        new_state = self.enforce_failsafe_close(state, gateway, notifier=notifier, state_repo=state_repo)
        new_state = dataclasses.replace(new_state, entries_halted=True)
        if state_repo:
            state_repo.save_state(new_state)
        if notifier:
            notifier.send(
                f"🚨 [部位對帳異常] {action_tag} 後帳上為 {real_net:+d} 口 (預期 {expected_net:+d} 口)，"
                f"已介入安全平倉與停開新倉，請確認帳務。",
                async_send=True,
            )
        return new_state

    def flatten_other_month_positions(
        self,
        other_positions: List[Dict[str, Any]],
        gateway: BrokerGateway,
        notifier: Optional[TradeNotifier] = None,
        price_type: str = "MKP",
    ) -> bool:
        """
        對非當前主力月份的 TMF 殘留部位執行嚴格合約平倉。
        必須以 allow_fallback=False 解析舊月份合約；若解析失敗嚴禁退回 TMFR1 下單，以免誤平主力部位。
        """
        if not other_positions:
            return True
        all_ok = True
        for pos in other_positions:
            other_code = str(pos.get("code", "")).strip()
            other_net = int(pos.get("net", 0))
            if not other_code or other_net == 0:
                continue

            other_contract = gateway.resolve_contract(other_code, allow_fallback=False)
            if other_contract is None:
                msg = (
                    f"🚨 [跨月殘倉警訊] 帳上發現非主力月份 TMF 部位 ({other_code}: {other_net:+d}口)，"
                    f"但無法精準解析該合約 (allow_fallback=False)，嚴禁退回 TMFR1 下單！請立即人工確認。"
                )
                logger.critical(msg)
                if notifier:
                    notifier.send(msg, async_send=False)
                all_ok = False
                continue

            act = "SELL" if other_net > 0 else "BUY"
            res: FillResult = gateway.place_order(
                action=act,
                qty=abs(other_net),
                price_type=price_type,
                octype="Cover",
                contract_override=other_contract,
            )
            if self.log_dir:
                append_order_audit(self.log_dir, act, abs(other_net), f"Cover({other_code})", res)
            if res.ok:
                logger.info(f"✅ 已清理跨月/殘留部位 {other_code} ({act} {abs(other_net)}口 @ {res.avg_price:.0f})")
            else:
                all_ok = False
                logger.error(f"❌ 清理跨月部位 {other_code} 未全數成交: {res.status} ({res.filled_qty}/{abs(other_net)})")
        return all_ok

    def startup_reconcile(
        self,
        state: TradingDayState,
        gateway: BrokerGateway,
        qty: int,
        state_repo: Optional[StateRepo] = None,
        notifier: Optional[TradeNotifier] = None,
        cfg: Optional[ORBStrategyConfig] = None,
    ) -> TradingDayState:
        """開盤/重啟時對齊券商真實部位與本地狀態檔"""
        eff_cfg = cfg or state.effective_config or ORBStrategyConfig()
        max_trades = max_trades_for_config(eff_cfg)
        saved = state_repo.load_state(state.trading_date) if state_repo else None
        target_code = state.contract_code or gateway.contract_code
        details = gateway.query_position_details(contract_code=target_code)

        other_positions = details.get("other_positions") or []
        if other_positions:
            self.flatten_other_month_positions(other_positions, gateway, notifier=notifier)

        real_net = details.get("net")
        if real_net is None:
            new_state = state_repo.restore_into_state(state, saved) if (state_repo and saved) else state
            if state_repo:
                state_repo.save_state(new_state)
            return new_state

        restored = state_repo.restore_into_state(state, saved) if (state_repo and saved) else state
        if real_net != 0:
            pos_dir = 1 if real_net > 0 else -1
            broker_avg = float(details.get("avg_price", 0.0) or 0.0)
            entry_px = broker_avg if broker_avg > 0 else restored.entry_price
            same_dir = saved is not None and int(saved.get("position", 0)) == pos_dir
            hp = restored.highest_profit if same_dir else 0.0
            new_state = dataclasses.replace(
                restored,
                position=pos_dir,
                open_qty=abs(int(real_net)),
                entry_price=entry_px,
                entry_time=restored.entry_time or pd.Timestamp.now(),
                trade_count=max(restored.trade_count, 1),
                highest_profit=hp,
                hp_before_bar_ts=None,
                hp_before_bar_val=0.0,
            )
            logger.warning(
                f"🔄 [啟動對帳接管] 券商帳上有 {real_net:+d} 口 TMF 部位 (均價 {entry_px:.0f})，已無縫接管風控！"
            )
            if state_repo:
                state_repo.save_state(new_state)
            return new_state

        # real_net == 0 (空手)
        if saved and int(saved.get("position", 0)) != 0:
            logger.warning("⚠️ 本地狀態原記錄為持倉，但券商帳上目前為 0 口，已自動拉回空手。")

        halted = restored.entries_halted or (restored.trade_count >= max_trades) or bool(
            saved and saved.get("entries_halted", False)
        )
        new_state = dataclasses.replace(
            restored,
            position=0,
            open_qty=0,
            entry_price=0.0,
            entry_time=None,
            highest_profit=0.0,
            hp_before_bar_ts=None,
            hp_before_bar_val=0.0,
            entries_halted=halted,
        )
        if state_repo:
            state_repo.save_state(new_state)
        return new_state

    def periodic_reconcile(
        self,
        state: TradingDayState,
        gateway: BrokerGateway,
        state_repo: Optional[StateRepo] = None,
        notifier: Optional[TradeNotifier] = None,
        cfg: Optional[ORBStrategyConfig] = None,
    ) -> TradingDayState:
        """盤中定期對帳，發現實盤與本地不一致時即時同步或清除他月殘倉"""
        target_code = state.contract_code or gateway.contract_code
        details = gateway.query_position_details(contract_code=target_code)

        other_positions = details.get("other_positions") or []
        if other_positions:
            self.flatten_other_month_positions(other_positions, gateway, notifier=notifier)

        real_net = details.get("net")
        if real_net is None:
            return state

        if real_net == 0 and state.position != 0:
            logger.warning("⚠️ [盤中對帳] 券商帳上已無部位 (可能已手動平倉)，策略同步歸零並停止今日開新倉。")
            new_state = dataclasses.replace(
                state,
                position=0,
                open_qty=0,
                entry_price=0.0,
                entry_time=None,
                highest_profit=0.0,
                hp_before_bar_ts=None,
                hp_before_bar_val=0.0,
                entries_halted=True,
            )
            if notifier:
                notifier.send("⚠️ [盤中對帳] 偵測到券商部位已歸零，已同步策略為空手並停開新倉。", async_send=True)
            if state_repo:
                state_repo.save_state(new_state)
            return new_state

        if real_net != 0 and state.position == 0:
            pos_dir = 1 if real_net > 0 else -1
            avg_px = float(details.get("avg_price", 0.0) or 0.0)
            logger.warning(f"🚨 [盤中對帳] 偵測到未受策略納管的券商部位 {real_net:+d} 口 @ {avg_px:.0f}，立即納入守護！")
            new_state = dataclasses.replace(
                state,
                position=pos_dir,
                open_qty=abs(int(real_net)),
                entry_price=avg_px,
                entry_time=pd.Timestamp.now(),
                trade_count=max(state.trade_count, 1),
                highest_profit=0.0,
                hp_before_bar_ts=None,
                hp_before_bar_val=0.0,
            )
            if notifier:
                notifier.send(f"🚨 [盤中對帳接管] 發現券商部位 {real_net:+d} 口 @ {avg_px:.0f}，已納入停損/尾盤守護！", async_send=True)
            if state_repo:
                state_repo.save_state(new_state)
            return new_state

        if real_net != 0 and state.position != 0 and (real_net * state.position) < 0:
            pos_dir = 1 if real_net > 0 else -1
            avg_px = float(details.get("avg_price", 0.0) or 0.0)
            logger.critical(
                f"🚨 [盤中對帳方向衝突] 策略為 {state.position:+d} 但券商帳上為 {real_net:+d} 口 @ {avg_px:.0f}，強制對齊券商真實部位並停開新倉！"
            )
            new_state = dataclasses.replace(
                state,
                position=pos_dir,
                open_qty=abs(int(real_net)),
                entry_price=avg_px if avg_px > 0 else state.entry_price,
                entry_time=pd.Timestamp.now(),
                highest_profit=0.0,
                hp_before_bar_ts=None,
                hp_before_bar_val=0.0,
                entries_halted=True,
            )
            if notifier:
                notifier.send(
                    f"🚨 [盤中對帳方向異常] 券商實際部位 {real_net:+d} 口 @ {avg_px:.0f} 與策略不符，已強制對齊並停開新倉！",
                    async_send=True,
                )
            if state_repo:
                state_repo.save_state(new_state)
            return new_state

        if real_net != 0 and state.position != 0 and state.open_qty != abs(int(real_net)):
            logger.warning(
                f"⚠️ [盤中對帳口數更新] 策略記錄 {state.open_qty} 口，券商實際 {abs(int(real_net))} 口，同步實際平倉口數！"
            )
            new_state = dataclasses.replace(state, open_qty=abs(int(real_net)))
            if notifier:
                notifier.send(
                    f"⚠️ [盤中對帳] 部位口數調整為 {abs(int(real_net))} 口 (券商真實淨口數 {real_net:+d})，後續平倉將對齊此口數。",
                    async_send=True,
                )
            if state_repo:
                state_repo.save_state(new_state)
            return new_state

        return state

    def enforce_failsafe_close(
        self,
        state: TradingDayState,
        gateway: BrokerGateway,
        notifier: Optional[TradeNotifier] = None,
        state_repo: Optional[StateRepo] = None,
        max_attempts: int = 3,
    ) -> TradingDayState:
        """13:09:00 ~ 13:10:00 安全底線死守平倉：依券商帳上淨部位 MKP IOC (末試升級 MKT) 強制補平，遇斷線自動重連"""
        target_code = state.contract_code or gateway.contract_code
        cur_state = state
        self.last_failsafe_verified_flat = False

        if not gateway.connected:
            logger.warning("🔄 [收盤死守平倉] 偵測到券商未連線，嘗試立即重連...")
            gateway.reconnect(target_code)

        last_real_net: Optional[int] = None
        for attempt in range(1, max_attempts + 1):
            px_type = "MKT" if (attempt == max_attempts and attempt > 1) else "MKP"
            details = gateway.query_position_details(contract_code=target_code)
            other_positions = details.get("other_positions") or []
            if other_positions:
                self.flatten_other_month_positions(other_positions, gateway, notifier=notifier, price_type=px_type)

            real_net = details.get("net")
            last_real_net = real_net
            if real_net is None:
                logger.error(f"⚠️ [13:09 收盤死守平倉 #{attempt}] 查詢帳務回傳 None，嘗試重連後重試...")
                gateway.reconnect(target_code)
                time.sleep(0.3)
                continue

            if real_net == 0:
                if cur_state.position != 0 or cur_state.open_qty != 0:
                    cur_state = dataclasses.replace(cur_state, position=0, open_qty=0, entries_halted=True)
                self.last_failsafe_verified_flat = True
                break

            act = "SELL" if real_net > 0 else "BUY"
            res = gateway.place_order(action=act, qty=abs(real_net), price_type=px_type, octype="Cover")
            if self.log_dir:
                append_order_audit(self.log_dir, act, abs(real_net), f"FailsafeCover({px_type})", res)
            logger.warning(
                f"🛡️ [13:09 收盤死守平倉 #{attempt} ({px_type})] {act} {abs(real_net)}口 -> {res.status} @ {res.avg_price:.0f}"
            )
            if res.ok:
                cur_state = dataclasses.replace(cur_state, position=0, open_qty=0, entries_halted=True)
                last_real_net = 0
                self.last_failsafe_verified_flat = True
                if notifier:
                    notifier.send(
                        f"🛡️ [13:09 收盤強制補平完成 ({px_type})] {act} {abs(real_net)} 口 @ {res.avg_price:.0f}",
                        async_send=True,
                    )
                break
            elif res.filled_qty > 0:
                rem = max(0, abs(real_net) - int(res.filled_qty))
                cur_state = dataclasses.replace(cur_state, open_qty=rem)
            time.sleep(0.3)

        if cur_state.position != 0 and last_real_net != 0:
            logger.critical(
                f"🚨 [收盤死守平倉警訊] 重試 {max_attempts} 次後仍未確認平倉完成 (local={cur_state.position}, broker={last_real_net})，將於下一輪持續追平！"
            )
            if notifier:
                notifier.send(
                    f"🚨 [收盤強平未確認] {target_code} 仍未能確認歸零 (broker={last_real_net})，機器人將持續重連追平，請留意！",
                    async_send=True,
                )

        if state_repo:
            state_repo.save_state(cur_state)
        return cur_state
