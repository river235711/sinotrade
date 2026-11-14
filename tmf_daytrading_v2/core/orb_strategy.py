"""
ORB Pro V2 純函式策略核心 (Zero I/O, Pure State Transition)

職責切分：
1. evaluate_bar(state, enriched_bar, cfg) -> OrderIntent
   完全純函式：絕不修改傳入的 TradingDayState，只回傳交易意圖 OrderIntent。
2. apply_intent_fill(state, intent, fill_price=None, fill_time=None) -> (new_state, trade_record)
   在實際成交（或回測擬真成交）後才推進 TradingDayState 的部位、均價與交易紀錄。
3. ORBStrategy 保留輕量外觀類別供回測與單日模擬方便呼叫，內部完全委派給上述純函式。
"""
import dataclasses
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config import InstrumentConfig, ORBStrategyConfig
from core.indicators import calculate_orb_levels_from_df, prepare_batch_indicators
from core.models import Bar, EnrichedBar, OrderIntent, TradeRecord, TradingDayState


def with_overnight_levels(
    state: TradingDayState,
    overnight_high: Optional[float] = None,
    overnight_low: Optional[float] = None,
) -> TradingDayState:
    """回傳套用昨夜盤極值後的全新狀態"""
    oh = float(overnight_high) if overnight_high is not None and pd.notna(overnight_high) else None
    ol = float(overnight_low) if overnight_low is not None and pd.notna(overnight_low) else None
    return dataclasses.replace(state, overnight_high=oh, overnight_low=ol)


def with_orb_levels(
    state: TradingDayState,
    kbars: pd.DataFrame,
    cfg: ORBStrategyConfig,
) -> TradingDayState:
    """依早盤與開盤區間 K 棒計算 ORB 高低點，回傳全新狀態"""
    pre_h, pre_l, orb_h, orb_l = calculate_orb_levels_from_df(kbars, cfg)
    updates: Dict[str, Any] = {}
    if pre_h is not None and pre_l is not None:
        updates["pre_orb_high"] = pre_h
        updates["pre_orb_low"] = pre_l
    if orb_h is not None and orb_l is not None:
        updates["orb_high"] = orb_h
        updates["orb_low"] = orb_l
        updates["orb_calculated"] = True
    return dataclasses.replace(state, **updates) if updates else state


def _evaluate_open_position(
    state: TradingDayState,
    ts: pd.Timestamp,
    o: float,
    h: float,
    l: float,
    c: float,
    vwap: float,
    t_str: str,
    cfg: ORBStrategyConfig,
) -> OrderIntent:
    """純函式評估多/空持倉的移動停利、三階保本鎖利與初始停損 / SAR 反手"""
    if state.hp_before_bar_ts != ts:
        hp_ts = ts
        hp_val = state.highest_profit
    else:
        hp_ts = state.hp_before_bar_ts
        hp_val = state.hp_before_bar_val
    prev_hp = hp_val

    sign = 1 if state.position == 1 else -1
    side_action = "BUY" if sign == 1 else "SELL"
    adverse_px = l if sign == 1 else h
    cur_profit = (h - state.entry_price) if sign == 1 else (state.entry_price - l)
    new_hp = cur_profit if cur_profit > state.highest_profit else state.highest_profit

    t15_trig = getattr(cfg, "tier1_5_trigger_pts", 160.0)
    t15_lock = getattr(cfg, "tier1_5_lock_pts", 100.0)

    def _hit(px: float, level: float) -> bool:
        return px <= level if sign == 1 else px >= level

    pullback_px = adverse_px
    is_favorable_candle = (c > o) if sign == 1 else (c < o)
    if cur_profit > prev_hp and is_favorable_candle:
        already_trig = (
            (prev_hp >= cfg.trailing_activation_pts and _hit(adverse_px, (state.entry_price + sign * prev_hp) - sign * cfg.trailing_distance_pts))
            or (prev_hp >= cfg.tier2_trigger_pts and _hit(adverse_px, state.entry_price + sign * cfg.tier2_lock_pts))
            or (prev_hp >= t15_trig and _hit(adverse_px, state.entry_price + sign * t15_lock))
            or (prev_hp >= cfg.breakeven_trigger_pts and _hit(adverse_px, state.entry_price + sign * cfg.breakeven_lock_pts))
        )
        if not already_trig:
            pullback_px = c

    # (A) 三階：大波段移動停利
    trail_lvl = (state.entry_price + sign * new_hp) - sign * cfg.trailing_distance_pts
    if new_hp >= cfg.trailing_activation_pts and _hit(pullback_px, trail_lvl):
        pnl_pts = (trail_lvl - state.entry_price) if sign == 1 else (state.entry_price - trail_lvl)
        return OrderIntent(
            action="EXIT",
            side_sign=0,
            price=trail_lvl,
            reason=f"三階波段移動停利 (極值+{new_hp:.0f}點)",
            pnl_pts=pnl_pts,
            closed_side_action=side_action,
            new_highest_profit=new_hp,
            hp_before_bar_ts=hp_ts,
            hp_before_bar_val=hp_val,
        )

    # (B) 二階 / 1.5 階 / 一階：階梯式保本鎖利
    for trig_pts, lock_pts, tier_name in (
        (cfg.tier2_trigger_pts, cfg.tier2_lock_pts, "二階波段鎖利"),
        (t15_trig, t15_lock, "中階保本鎖利"),
        (cfg.breakeven_trigger_pts, cfg.breakeven_lock_pts, "一階保本鎖利"),
    ):
        if new_hp >= trig_pts:
            exit_p = state.entry_price + sign * lock_pts
            if _hit(pullback_px, exit_p):
                return OrderIntent(
                    action="EXIT",
                    side_sign=0,
                    price=exit_p,
                    reason=f"{tier_name} (曾獲利+{new_hp:.0f}點, 鎖定+{lock_pts:.0f}點)",
                    pnl_pts=lock_pts,
                    closed_side_action=side_action,
                    new_highest_profit=new_hp,
                    hp_before_bar_ts=hp_ts,
                    hp_before_bar_val=hp_val,
                )

    # (C) 初始固定停損 (含假突破反手 SAR)
    sl_lvl = state.entry_price - sign * cfg.stop_loss_pts
    if _hit(adverse_px, sl_lvl):
        vwap_reversed = (c < vwap) if sign == 1 else (c > vwap)
        if cfg.enable_sar and state.trade_count < 2 and t_str <= cfg.sar_end_time and vwap_reversed:
            return OrderIntent(
                action="SAR_SELL" if sign == 1 else "SAR_BUY",
                side_sign=-sign,
                price=sl_lvl,
                reason=f"初始停損 (-{cfg.stop_loss_pts:.0f}點)",
                pnl_pts=-cfg.stop_loss_pts,
                closed_side_action=side_action,
                is_sar=True,
                new_highest_profit=0.0,
                hp_before_bar_ts=None,
                hp_before_bar_val=0.0,
            )
        return OrderIntent(
            action="EXIT",
            side_sign=0,
            price=sl_lvl,
            reason=f"初始停損 (-{cfg.stop_loss_pts:.0f}點)",
            pnl_pts=-cfg.stop_loss_pts,
            closed_side_action=side_action,
            new_highest_profit=new_hp,
            hp_before_bar_ts=hp_ts,
            hp_before_bar_val=hp_val,
        )

    return OrderIntent(
        action="HOLD",
        new_highest_profit=new_hp,
        hp_before_bar_ts=hp_ts,
        hp_before_bar_val=hp_val,
    )


def evaluate_bar(
    state: TradingDayState,
    enriched_bar: EnrichedBar,
    cfg: ORBStrategyConfig,
    t_str: Optional[str] = None,
) -> OrderIntent:
    """
    對單根含指標 K 棒進行純函式策略評估，回傳 OrderIntent。
    保證完全不修改 state。
    """
    b = enriched_bar.bar
    ts = b.timestamp
    o, h, l, c = b.open, b.high, b.low, b.close
    vwap, ema9, ema21 = enriched_bar.vwap, enriched_bar.ema9, enriched_bar.ema21
    if t_str is None:
        t_str = ts.strftime("%H:%M:%S")

    # 1. 強制收盤平倉
    if t_str >= cfg.force_close_time:
        if state.position != 0:
            pnl_pts = (c - state.entry_price) if state.position == 1 else (state.entry_price - c)
            side_action = "BUY" if state.position == 1 else "SELL"
            return OrderIntent(
                action="EXIT",
                side_sign=0,
                price=c,
                reason=f"{cfg.force_close_time[:5]} 收盤前安全平倉",
                pnl_pts=pnl_pts,
                closed_side_action=side_action,
                new_highest_profit=state.highest_profit,
                hp_before_bar_ts=state.hp_before_bar_ts,
                hp_before_bar_val=state.hp_before_bar_val,
            )
        return OrderIntent(action="HOLD")

    # 2. 已持部位：評估移動停利 / 保本鎖利 / 停損與 SAR
    if state.position != 0:
        return _evaluate_open_position(state, ts, o, h, l, c, vwap, t_str, cfg)

    if state.entries_halted:
        return OrderIntent(action="HOLD")

    # 3A. 09:01 ~ 09:15 早盤強勢爆發突破進場
    if (
        getattr(cfg, "enable_early_super_breakout", True)
        and state.pre_orb_high is not None
        and state.pre_orb_low is not None
        and state.trade_count < cfg.max_initial_trades
        and "09:01:00" <= t_str <= cfg.orb_end_time
    ):
        long_filter = (not cfg.use_vwap_ema_filter) or (c > vwap and ema9 > ema21)
        short_filter = (not cfg.use_vwap_ema_filter) or (c < vwap and ema9 < ema21)
        sb = cfg.early_super_breakout_pts

        pos_long_ok = True
        pos_short_ok = True
        if state.orb_calculated and state.orb_high is not None and state.orb_low is not None:
            orb_range_3a = state.orb_high - state.orb_low
            max_orb_3a = getattr(cfg, "max_orb_range_pts", 320.0)
            max_ratio_3a = getattr(cfg, "early_breakout_max_range_ratio", 1.25)
            max_chase_3a = getattr(cfg, "max_breakout_chase_pts", 60.0)
            range_ok = orb_range_3a <= max_orb_3a * max_ratio_3a
            pos_long_ok = range_ok and ((state.orb_high - c) <= max_chase_3a) and (h < state.orb_high) and (c > o)
            pos_short_ok = range_ok and ((c - state.orb_low) <= max_chase_3a) and (l > state.orb_low) and (c < o)

        if c >= (state.pre_orb_high + sb) and long_filter and pos_long_ok:
            return OrderIntent(
                action="BUY",
                side_sign=1,
                price=c,
                reason=f"早盤強勢蓄勢突破 ({state.pre_orb_high}+{sb:.0f}點) + 位置/VWAP/EMA 多頭確認",
            )
        if c <= (state.pre_orb_low - sb) and short_filter and pos_short_ok:
            return OrderIntent(
                action="SELL",
                side_sign=-1,
                price=c,
                reason=f"早盤強勢蓄勢跳水 ({state.pre_orb_low}-{sb:.0f}點) + 位置/VWAP/EMA 空頭確認",
            )

    # 3B. 09:16 ~ 11:30 標準現貨開盤區間突破 + 夜盤防撞牆 + VWAP/EMA 共振
    orb_range = (state.orb_high - state.orb_low) if state.orb_calculated else 0.0
    max_orb_range = getattr(cfg, "max_orb_range_pts", 320.0)
    max_chase = getattr(cfg, "max_breakout_chase_pts", 60.0)

    if (
        state.orb_calculated
        and orb_range <= max_orb_range
        and state.trade_count < cfg.max_initial_trades
        and cfg.entry_start_time <= t_str <= cfg.entry_end_time
    ):
        long_filter = (not cfg.use_vwap_ema_filter) or (c > vwap and ema9 > ema21)
        short_filter = (not cfg.use_vwap_ema_filter) or (c < vwap and ema9 < ema21)

        buy_threshold = state.orb_high
        sell_threshold = state.orb_low
        wall_filter_active_long = False
        wall_filter_active_short = False

        if getattr(cfg, "use_overnight_wall_filter", False):
            thresh_pts = getattr(cfg, "overnight_wall_threshold_pts", 40.0)
            if (
                state.overnight_high is not None
                and state.orb_high < state.overnight_high
                and (state.overnight_high - state.orb_high) <= thresh_pts
            ):
                buy_threshold = state.overnight_high
                wall_filter_active_long = True

            if (
                state.overnight_low is not None
                and state.orb_low > state.overnight_low
                and (state.orb_low - state.overnight_low) <= thresh_pts
            ):
                sell_threshold = state.overnight_low
                wall_filter_active_short = True

        if (buy_threshold < c <= (buy_threshold + max_chase)) and long_filter:
            detail_str = (
                f"突破夜高防撞牆 ({state.overnight_high:.0f}，原ORB上界{state.orb_high:.0f})"
                if wall_filter_active_long
                else f"突破現貨開盤高點 ({state.orb_high:.0f})"
            )
            return OrderIntent(
                action="BUY",
                side_sign=1,
                price=c,
                reason=f"{detail_str} + VWAP/EMA 多頭確認",
            )

        if ((sell_threshold - max_chase) <= c < sell_threshold) and short_filter:
            detail_str = (
                f"跌破夜低防撞牆 ({state.overnight_low:.0f}，原ORB下界{state.orb_low:.0f})"
                if wall_filter_active_short
                else f"跌破現貨開盤低點 ({state.orb_low:.0f})"
            )
            return OrderIntent(
                action="SELL",
                side_sign=-1,
                price=c,
                reason=f"{detail_str} + VWAP/EMA 空頭確認",
            )

    return OrderIntent(action="HOLD")


def apply_intent_fill(
    state: TradingDayState,
    intent: OrderIntent,
    fill_price: Optional[float] = None,
    fill_time: Optional[pd.Timestamp] = None,
    new_entry_price: Optional[float] = None,
) -> Tuple[TradingDayState, Optional[TradeRecord]]:
    """
    依據下單成交結果推進 TradingDayState。
    - action == "HOLD": 僅推進盤中持倉最高浮盈追踪欄位，不新增成交紀錄。
    - action in ("BUY", "SELL"): 依實際成交價與時間建立新部位。
    - action == "EXIT": 寫入已實現 TradeRecord 並清零部位。
    - action in ("SAR_BUY", "SAR_SELL"): 記錄停損平倉紀錄，並反手建立新部位。
    """
    if intent.action == "HOLD":
        if state.position == 0:
            return state, None
        return dataclasses.replace(
            state,
            highest_profit=intent.new_highest_profit,
            hp_before_bar_ts=intent.hp_before_bar_ts,
            hp_before_bar_val=intent.hp_before_bar_val,
        ), None

    px = float(intent.price if fill_price is None else fill_price)
    ts = fill_time

    if intent.action in ("BUY", "SELL"):
        new_state = dataclasses.replace(
            state,
            position=intent.side_sign,
            entry_price=px,
            entry_time=ts,
            trade_count=state.trade_count + 1,
            highest_profit=0.0,
            hp_before_bar_ts=None,
            hp_before_bar_val=0.0,
        )
        return new_state, None

    if intent.action == "EXIT":
        sign = 1 if state.position == 1 else -1
        pnl = (
            intent.pnl_pts
            if (fill_price is None or abs(px - intent.price) < 1e-12)
            else ((px - state.entry_price) * sign)
        )
        rec = TradeRecord(
            entry_time=state.entry_time,
            exit_time=ts,
            action=intent.closed_side_action or ("BUY" if sign == 1 else "SELL"),
            entry_price=state.entry_price,
            exit_price=px,
            pnl_pts=pnl,
            reason=intent.reason,
        )
        new_state = dataclasses.replace(
            state,
            position=0,
            highest_profit=intent.new_highest_profit,
            hp_before_bar_ts=intent.hp_before_bar_ts,
            hp_before_bar_val=intent.hp_before_bar_val,
            trades=state.trades + (rec,),
        )
        return new_state, rec

    if intent.action in ("SAR_BUY", "SAR_SELL"):
        sign = 1 if state.position == 1 else -1
        pnl = (
            intent.pnl_pts
            if (fill_price is None or abs(px - intent.price) < 1e-12)
            else ((px - state.entry_price) * sign)
        )
        rec = TradeRecord(
            entry_time=state.entry_time,
            exit_time=ts,
            action=intent.closed_side_action or ("BUY" if sign == 1 else "SELL"),
            entry_price=state.entry_price,
            exit_price=px,
            pnl_pts=pnl,
            reason=intent.reason,
        )
        rev_px = float(px if new_entry_price is None else new_entry_price)
        new_state = dataclasses.replace(
            state,
            position=intent.side_sign,
            entry_price=rev_px,
            entry_time=ts,
            trade_count=state.trade_count + 1,
            highest_profit=0.0,
            hp_before_bar_ts=None,
            hp_before_bar_val=0.0,
            trades=state.trades + (rec,),
        )
        return new_state, rec

    return state, None


def apply_exit_only(
    state: TradingDayState,
    intent: OrderIntent,
    fill_price: Optional[float] = None,
    fill_time: Optional[pd.Timestamp] = None,
) -> Tuple[TradingDayState, Optional[TradeRecord]]:
    """實盤 SAR 若第一腳平倉成交但反手開新倉失敗時，退化為純平倉空手狀態"""
    px = float(intent.price if fill_price is None else fill_price)
    sign = 1 if state.position == 1 else -1
    pnl = (
        intent.pnl_pts
        if (fill_price is None or abs(px - intent.price) < 1e-12)
        else ((px - state.entry_price) * sign)
    )
    rec = TradeRecord(
        entry_time=state.entry_time,
        exit_time=fill_time,
        action=intent.closed_side_action or ("BUY" if sign == 1 else "SELL"),
        entry_price=state.entry_price,
        exit_price=px,
        pnl_pts=pnl,
        reason=intent.reason,
    )
    new_state = dataclasses.replace(
        state,
        position=0,
        highest_profit=0.0,
        hp_before_bar_ts=None,
        hp_before_bar_val=0.0,
        trades=state.trades + (rec,),
    )
    return new_state, rec


class ORBStrategy:
    """狀態包裝外觀類：將不可變 TradingDayState 與純函式 evaluate_bar/apply_intent_fill 包裝為易用介面對象"""

    def __init__(self, config: ORBStrategyConfig = None, inst_config: InstrumentConfig = None):
        self.config = config or ORBStrategyConfig()
        self.inst_config = inst_config or InstrumentConfig()
        self.state = TradingDayState(effective_config=self.config)

    def reset(self):
        self.state = TradingDayState(effective_config=self.config)

    @property
    def orb_calculated(self) -> bool:
        return self.state.orb_calculated

    @property
    def orb_high(self) -> Optional[float]:
        return self.state.orb_high

    @property
    def orb_low(self) -> Optional[float]:
        return self.state.orb_low

    @property
    def pre_orb_high(self) -> Optional[float]:
        return self.state.pre_orb_high

    @property
    def pre_orb_low(self) -> Optional[float]:
        return self.state.pre_orb_low

    @property
    def trades(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self.state.trades]

    def set_overnight_levels(self, overnight_high: float = None, overnight_low: float = None):
        self.state = with_overnight_levels(self.state, overnight_high, overnight_low)

    def prepare_indicators(self, kbars: pd.DataFrame) -> pd.DataFrame:
        return prepare_batch_indicators(kbars)

    def calculate_orb_levels(self, kbars: pd.DataFrame):
        self.state = with_orb_levels(self.state, kbars, self.config)
        return self.state.orb_high, self.state.orb_low

    def execute_values(
        self,
        ts: pd.Timestamp,
        o: float,
        h: float,
        l: float,
        c: float,
        vwap: float,
        ema9: float,
        ema21: float,
        t_str: str = None,
    ) -> Dict[str, Any]:
        bar = EnrichedBar(
            bar=Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=0.0),
            vwap=vwap,
            ema9=ema9,
            ema21=ema21,
        )
        intent = evaluate_bar(self.state, bar, self.config, t_str=t_str)
        self.state, rec = apply_intent_fill(self.state, intent, fill_price=intent.price, fill_time=ts)
        if intent.action == "HOLD":
            return {"action": "HOLD", "detail": ""}
        if intent.action in ("BUY", "SELL"):
            return {"action": intent.action, "price": intent.price, "time": ts, "detail": intent.reason}
        return {"action": intent.action, "record": rec.to_dict() if rec else None}
