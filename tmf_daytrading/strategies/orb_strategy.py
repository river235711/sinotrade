"""
開盤區間突破策略 Pro 翻修獲利版 (ORB V2 + VWAP/EMA 濾網 + 保本鎖利 + 假突破反手 SAR)
適用於台指期 (TXF / MXF / TMF) 當沖交易。

核心翻修進化 (參考 GitHub 與機構法人知名量化策略)：
1. 現貨開盤區間 (09:00 ~ 09:15 Spot-Open ORB)：
   避開 08:45~09:00 期貨單獨開盤、09:00 台股現貨開盤瞬間的洗盤假突破，以 09:00~09:15 現貨開盤首 15 分鐘高低點為多空分水嶺。
2. 法人雙重趨勢濾網 (VWAP + EMA9/EMA21)：
   做多需同時滿足：突破 ORB 上界 + 收盤價站上 VWAP (日內均量成本線) + EMA9 > EMA21。
   做空需同時滿足：跌破 ORB 下界 + 收盤價跌破 VWAP + EMA9 < EMA21。
3. 二段式保本鎖利與波段抱單機制 (Breakeven Lock + Trend Runner)：
   - 初始停損放寬至 90 點（適應台指 45,000~50,000 點位約 0.18% 的正常波動，避免被小反抽洗出）。
   - 第一段【保本鎖利】：當浮動獲利達 +80 點時，停損立刻上移至「進場價 +30 點」，確保獲利單絕不變虧損單。
   - 第二段【大波段抱單】：獲利達 +400 點啟動移動停利（拉回 60 點出場），否則一路抱到 13:30 收盤平倉，完整吃下單邊趨勢日。
4. 假突破停損反手機制 (Stop & Reverse, SAR)：
   若 11:00 前第一筆突破單觸發 90 點停損，且收盤價已反向跌破/站上 VWAP（確認為早盤誘多/誘空假突破），立即反手進場順應真趨勢（每日最多反手 1 次）。
"""
import pandas as pd
from typing import Dict, Any, List, Optional
from config import ORBStrategyConfig, InstrumentConfig

class ORBStrategy:
    def __init__(self, config: ORBStrategyConfig = None, inst_config: InstrumentConfig = None):
        self.config = config or ORBStrategyConfig()
        self.inst_config = inst_config or InstrumentConfig()
        self.reset()

    def reset(self):
        """重置每日盤中狀態"""
        self.pre_orb_high = None
        self.pre_orb_low = None
        self.orb_high = None
        self.orb_low = None
        self.orb_calculated = False
        self.overnight_high = None
        self.overnight_low = None
        
        self.position = 0             # 0: 無部位, 1: 多單, -1: 空單
        self.entry_price = 0.0
        self.entry_time = None
        self.highest_profit = 0.0     # 用於保本鎖利與移動停利計算
        self._hp_before_bar_ts = None # 記錄當根 K 棒開始前的最高浮盈時戳 (防同K開盤高低點誤觸)
        self._hp_before_bar_val = 0.0 # 記錄當根 K 棒開始前的最高浮盈數值
        self.trade_count = 0
        self.trades: List[Dict[str, Any]] = []

    def set_overnight_levels(self, overnight_high: float = None, overnight_low: float = None):
        """設定當日對應之前一日夜盤 (15:00~05:00) 最高價與最低價，供防撞牆保護濾網使用"""
        self.overnight_high = float(overnight_high) if overnight_high is not None and pd.notna(overnight_high) else None
        self.overnight_low = float(overnight_low) if overnight_low is not None and pd.notna(overnight_low) else None

    def prepare_indicators(self, kbars: pd.DataFrame) -> pd.DataFrame:
        """計算日內 VWAP 與 EMA9 / EMA21 指標 (累計量為 0 時以收盤價防呆取代 0.0)"""
        df = kbars.copy()
        cum_vol_raw = df['volume'].cumsum()
        raw_vwap = (df['close'] * df['volume']).cumsum() / cum_vol_raw.replace(0, 1)
        df['vwap'] = raw_vwap.where(cum_vol_raw > 0, df['close'])
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        return df

    def calculate_orb_levels(self, kbars: pd.DataFrame):
        """計算指定日期的早盤區間 (08:45~09:00) 與現貨開盤區間 (09:00~09:15) 高低點"""
        pre_bars = kbars.between_time(
            self.config.pre_orb_start_time[:5],
            self.config.pre_orb_end_time[:5]
        )
        if len(pre_bars) > 0:
            self.pre_orb_high = float(pre_bars['high'].max())
            self.pre_orb_low = float(pre_bars['low'].min())

        orb_bars = kbars.between_time(
            self.config.orb_start_time[:5], 
            self.config.orb_end_time[:5]
        )
        if len(orb_bars) > 0:
            self.orb_high = float(orb_bars['high'].max())
            self.orb_low = float(orb_bars['low'].min())
            self.orb_calculated = True
            return self.orb_high, self.orb_low
        return None, None

    def _append_trade_record(
        self, ts: pd.Timestamp, action: str, exit_price: float, pnl_pts: float, reason: str
    ) -> Dict[str, Any]:
        """建立並記錄單筆平倉成交明細"""
        rec = {
            "entry_time": self.entry_time,
            "exit_time": ts,
            "action": action,
            "entry_price": self.entry_price,
            "exit_price": exit_price,
            "pnl_pts": pnl_pts,
            "reason": reason,
        }
        self.trades.append(rec)
        return rec

    def _exit_position(
        self, ts: pd.Timestamp, action: str, exit_price: float, pnl_pts: float, reason: str
    ) -> Dict[str, Any]:
        """記錄平倉並重置部位狀態為空手"""
        rec = self._append_trade_record(ts, action, exit_price, pnl_pts, reason)
        self.position = 0
        return {"action": "EXIT", "record": rec}

    def _enter_position(
        self, side_sign: int, price: float, ts: pd.Timestamp, detail: str
    ) -> Dict[str, Any]:
        """建立多/空進場部位並重設當筆最高浮盈狀態"""
        self.position = side_sign
        self.entry_price = price
        self.entry_time = ts
        self.trade_count += 1
        self.highest_profit = 0.0
        return {
            "action": "BUY" if side_sign == 1 else "SELL",
            "price": price,
            "time": ts,
            "detail": detail,
        }

    def _evaluate_open_position(
        self, ts: pd.Timestamp, o: float, h: float, l: float, c: float, vwap: float, t_str: str
    ) -> Optional[Dict[str, Any]]:
        """統一評估多/空持倉的移動停利、三階保本鎖利與初始停損 / SAR 反手"""
        if self._hp_before_bar_ts != ts:
            self._hp_before_bar_ts = ts
            self._hp_before_bar_val = self.highest_profit
        prev_hp = self._hp_before_bar_val

        sign = 1 if self.position == 1 else -1
        side_action = "BUY" if sign == 1 else "SELL"
        favorable_px, adverse_px = (h, l) if sign == 1 else (l, h)
        cur_profit = (h - self.entry_price) if sign == 1 else (self.entry_price - l)
        if cur_profit > self.highest_profit:
            self.highest_profit = cur_profit

        cfg = self.config
        t15_trig = getattr(cfg, "tier1_5_trigger_pts", 160.0)
        t15_lock = getattr(cfg, "tier1_5_lock_pts", 100.0)

        def _hit(px: float, level: float) -> bool:
            return px <= level if sign == 1 else px >= level

        # 同 K 棒高低點時序保護 (真實拉回才出場)：
        # 若本根 K 棒為順勢創極值棒 (多單紅 K / 空單黑 K)，先確認進棒前浮盈 prev_hp 是否已能讓逆勢端觸價；
        # 否則新跨過門檻的鎖利判定以當前收盤/即時價 c 為準。
        pullback_px = adverse_px
        is_favorable_candle = (c > o) if sign == 1 else (c < o)
        if cur_profit > prev_hp and is_favorable_candle:
            already_trig = (
                (prev_hp >= cfg.trailing_activation_pts and _hit(adverse_px, (self.entry_price + sign * prev_hp) - sign * cfg.trailing_distance_pts)) or
                (prev_hp >= cfg.tier2_trigger_pts and _hit(adverse_px, self.entry_price + sign * cfg.tier2_lock_pts)) or
                (prev_hp >= t15_trig and _hit(adverse_px, self.entry_price + sign * t15_lock)) or
                (prev_hp >= cfg.breakeven_trigger_pts and _hit(adverse_px, self.entry_price + sign * cfg.breakeven_lock_pts))
            )
            if not already_trig:
                pullback_px = c

        # (A) 三階：大波段移動停利
        trail_lvl = (self.entry_price + sign * self.highest_profit) - sign * cfg.trailing_distance_pts
        if self.highest_profit >= cfg.trailing_activation_pts and _hit(pullback_px, trail_lvl):
            pnl_pts = (trail_lvl - self.entry_price) if sign == 1 else (self.entry_price - trail_lvl)
            return self._exit_position(
                ts, side_action, trail_lvl, pnl_pts,
                f"三階波段移動停利 (極值+{self.highest_profit:.0f}點)"
            )

        # (B) 二階 / 1.5 階 / 一階：階梯式保本鎖利
        for trig_pts, lock_pts, tier_name in (
            (cfg.tier2_trigger_pts, cfg.tier2_lock_pts, "二階波段鎖利"),
            (t15_trig, t15_lock, "中階保本鎖利"),
            (cfg.breakeven_trigger_pts, cfg.breakeven_lock_pts, "一階保本鎖利"),
        ):
            if self.highest_profit >= trig_pts:
                exit_p = self.entry_price + sign * lock_pts
                if _hit(pullback_px, exit_p):
                    return self._exit_position(
                        ts, side_action, exit_p, lock_pts,
                        f"{tier_name} (曾獲利+{self.highest_profit:.0f}點, 鎖定+{lock_pts:.0f}點)"
                    )

        # (C) 初始固定停損 (含假突破反手 SAR)
        sl_lvl = self.entry_price - sign * cfg.stop_loss_pts
        if _hit(adverse_px, sl_lvl):
            trade_record = self._append_trade_record(
                ts, side_action, sl_lvl, -cfg.stop_loss_pts,
                f"初始停損 (-{cfg.stop_loss_pts:.0f}點)"
            )
            vwap_reversed = (c < vwap) if sign == 1 else (c > vwap)
            if cfg.enable_sar and self.trade_count < 2 and t_str <= cfg.sar_end_time and vwap_reversed:
                self.position = -sign
                self.entry_price = sl_lvl
                self.entry_time = ts
                self.trade_count += 1
                self.highest_profit = 0.0
                self._hp_before_bar_ts = None
                self._hp_before_bar_val = 0.0
                return {"action": "SAR_SELL" if sign == 1 else "SAR_BUY", "record": trade_record}
            self.position = 0
            return {"action": "EXIT", "record": trade_record}

        return None

    def execute_bar(self, ts: pd.Timestamp, bar: pd.Series, prev_bar: pd.Series = None) -> Dict[str, Any]:
        """處理單根 1分K 棒"""
        c = float(bar['close'])
        o = float(bar.get('open', c))
        h = float(bar['high'])
        l = float(bar['low'])
        vwap = float(bar.get('vwap', c))
        ema9 = float(bar.get('ema9', c))
        ema21 = float(bar.get('ema21', c))
        return self.execute_values(ts, o, h, l, c, vwap, ema9, ema21)

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
        """零 Series 開銷純標量版單根 K 棒策略執行 (與 execute_bar 結果 100% 等價)"""
        if t_str is None:
            t_str = ts.strftime('%H:%M:%S')
        cfg = self.config

        # 1. 檢查強制收盤平倉
        if t_str >= cfg.force_close_time:
            if self.position != 0:
                pnl_pts = (c - self.entry_price) if self.position == 1 else (self.entry_price - c)
                side_action = "BUY" if self.position == 1 else "SELL"
                return self._exit_position(
                    ts, side_action, c, pnl_pts, f"{cfg.force_close_time[:5]} 收盤前安全平倉"
                )
            return {"action": "HOLD", "detail": ""}

        # 2. 如果已有部位，依序檢查：大波段移動停利 -> 保本鎖利 -> 初始停損 (含假突破反手 SAR)
        if self.position != 0:
            res = self._evaluate_open_position(ts, o, h, l, c, vwap, t_str)
            if res is not None:
                return res

        # 3A. 09:01 ~ 09:15 早盤強勢爆發突破進場 (方案 B 精準改良版：空間未過度耗盡 + 蓄勢二次突破位置濾網)
        if (getattr(cfg, "enable_early_super_breakout", True) and
            self.position == 0 and
            self.pre_orb_high is not None and
            self.trade_count < cfg.max_initial_trades and
            "09:01:00" <= t_str <= cfg.orb_end_time):

            long_filter = (not cfg.use_vwap_ema_filter) or (c > vwap and ema9 > ema21)
            short_filter = (not cfg.use_vwap_ema_filter) or (c < vwap and ema9 < ema21)
            sb = cfg.early_super_breakout_pts

            pos_long_ok = True
            pos_short_ok = True
            if self.orb_calculated and self.orb_high is not None and self.orb_low is not None:
                orb_range_3a = self.orb_high - self.orb_low
                max_orb_3a = getattr(cfg, "max_orb_range_pts", 320.0)
                max_ratio_3a = getattr(cfg, "early_breakout_max_range_ratio", 1.25)
                max_chase_3a = getattr(cfg, "max_breakout_chase_pts", 60.0)
                range_ok = (orb_range_3a <= max_orb_3a * max_ratio_3a)
                pos_long_ok = range_ok and ((self.orb_high - c) <= max_chase_3a) and (h < self.orb_high) and (c > o)
                pos_short_ok = range_ok and ((c - self.orb_low) <= max_chase_3a) and (l > self.orb_low) and (c < o)

            if c >= (self.pre_orb_high + sb) and long_filter and pos_long_ok:
                return self._enter_position(
                    1, c, ts,
                    f"早盤強勢蓄勢突破 ({self.pre_orb_high}+{sb:.0f}點) + 位置/VWAP/EMA 多頭確認"
                )
            elif c <= (self.pre_orb_low - sb) and short_filter and pos_short_ok:
                return self._enter_position(
                    -1, c, ts,
                    f"早盤強勢蓄勢跳水 ({self.pre_orb_low}-{sb:.0f}點) + 位置/VWAP/EMA 空頭確認"
                )

        # 3B. 09:16 ~ 11:30 標準現貨開盤區間突破 + ORB 寬度空間保護 + VWAP & EMA 延遲共振進場
        orb_range = (self.orb_high - self.orb_low) if self.orb_calculated else 0.0
        max_orb_range = getattr(cfg, "max_orb_range_pts", 320.0)
        max_chase = getattr(cfg, "max_breakout_chase_pts", 60.0)

        if (self.position == 0 and
            self.orb_calculated and
            orb_range <= max_orb_range and
            self.trade_count < cfg.max_initial_trades and
            cfg.entry_start_time <= t_str <= cfg.entry_end_time):

            long_filter = (not cfg.use_vwap_ema_filter) or (c > vwap and ema9 > ema21)
            short_filter = (not cfg.use_vwap_ema_filter) or (c < vwap and ema9 < ema21)

            buy_threshold = self.orb_high
            sell_threshold = self.orb_low
            wall_filter_active_long = False
            wall_filter_active_short = False

            if getattr(cfg, "use_overnight_wall_filter", False):
                thresh_pts = getattr(cfg, "overnight_wall_threshold_pts", 40.0)
                if (self.overnight_high is not None and
                    self.orb_high < self.overnight_high and
                    (self.overnight_high - self.orb_high) <= thresh_pts):
                    buy_threshold = self.overnight_high
                    wall_filter_active_long = True

                if (self.overnight_low is not None and
                    self.orb_low > self.overnight_low and
                    (self.orb_low - self.overnight_low) <= thresh_pts):
                    sell_threshold = self.overnight_low
                    wall_filter_active_short = True

            if (buy_threshold < c <= (buy_threshold + max_chase)) and long_filter:
                detail_str = (
                    f"突破夜高防撞牆 ({self.overnight_high:.0f}，原ORB上界{self.orb_high:.0f})"
                    if wall_filter_active_long
                    else f"突破現貨開盤高點 ({self.orb_high:.0f})"
                )
                return self._enter_position(1, c, ts, f"{detail_str} + VWAP/EMA 多頭確認")

            elif ((sell_threshold - max_chase) <= c < sell_threshold) and short_filter:
                detail_str = (
                    f"跌破夜低防撞牆 ({self.overnight_low:.0f}，原ORB下界{self.orb_low:.0f})"
                    if wall_filter_active_short
                    else f"跌破現貨開盤低點 ({self.orb_low:.0f})"
                )
                return self._enter_position(-1, c, ts, f"{detail_str} + VWAP/EMA 空頭確認")

        return {"action": "HOLD", "detail": ""}
