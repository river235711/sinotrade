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
from typing import Dict, Any, List
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
        self.trade_count = 0
        self.trades: List[Dict[str, Any]] = []

    def set_overnight_levels(self, overnight_high: float = None, overnight_low: float = None):
        """設定當日對應之前一日夜盤 (15:00~05:00) 最高價與最低價，供防撞牆保護濾網使用"""
        self.overnight_high = float(overnight_high) if overnight_high is not None and pd.notna(overnight_high) else None
        self.overnight_low = float(overnight_low) if overnight_low is not None and pd.notna(overnight_low) else None

    def prepare_indicators(self, kbars: pd.DataFrame) -> pd.DataFrame:
        """計算日內 VWAP 與 EMA9 / EMA21 指標"""
        df = kbars.copy()
        cum_vol = df['volume'].cumsum().replace(0, 1)
        df['vwap'] = (df['close'] * df['volume']).cumsum() / cum_vol
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

    def execute_bar(self, ts: pd.Timestamp, bar: pd.Series, prev_bar: pd.Series = None) -> Dict[str, Any]:
        """處理單根 1分K 棒"""
        t_str = ts.strftime('%H:%M:%S')
        c = float(bar['close'])
        o = float(bar.get('open', c))
        h = float(bar['high'])
        l = float(bar['low'])
        vwap = float(bar.get('vwap', c))
        ema9 = float(bar.get('ema9', c))
        ema21 = float(bar.get('ema21', c))
        prev_c = float(prev_bar['close']) if prev_bar is not None else c
        
        action_result = {"action": "HOLD", "detail": ""}

        # 1. 檢查 13:25 強制收盤平倉 (避開結算日 13:30 停止交易與現貨尾盤甩尾)
        if t_str >= self.config.force_close_time:
            if self.position != 0:
                pnl_pts = (c - self.entry_price) if self.position == 1 else (self.entry_price - c)
                trade_record = {
                    "entry_time": self.entry_time,
                    "exit_time": ts,
                    "action": "BUY" if self.position == 1 else "SELL",
                    "entry_price": self.entry_price,
                    "exit_price": c,
                    "pnl_pts": pnl_pts,
                    "reason": f"{self.config.force_close_time[:5]} 收盤前安全平倉"
                }
                self.trades.append(trade_record)
                action_result = {"action": "EXIT", "record": trade_record}
                self.position = 0
            return action_result

        # 2. 如果已有部位，依序檢查：大波段移動停利 -> 保本鎖利 -> 初始停損 (含假突破反手 SAR)
        if self.position != 0:
            if self.position == 1:
                cur_profit = h - self.entry_price
                if cur_profit > self.highest_profit:
                    self.highest_profit = cur_profit

                # (A) 三階：大波段移動停利 (+400點啟動)
                if (self.highest_profit >= self.config.trailing_activation_pts and 
                    l <= (self.entry_price + self.highest_profit - self.config.trailing_distance_pts)):
                    exit_p = self.entry_price + self.highest_profit - self.config.trailing_distance_pts
                    pnl_pts = exit_p - self.entry_price
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "BUY",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": pnl_pts,
                        "reason": f"三階波段移動停利 (極值+{self.highest_profit:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (B) 二階：中波段鎖利 (獲利曾達 +200 點，拉回時鎖住 +120 點)
                if (self.highest_profit >= self.config.tier2_trigger_pts and 
                    l <= (self.entry_price + self.config.tier2_lock_pts)):
                    exit_p = self.entry_price + self.config.tier2_lock_pts
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "BUY",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": self.config.tier2_lock_pts,
                        "reason": f"二階波段鎖利 (曾獲利+{self.highest_profit:.0f}點, 鎖定+{self.config.tier2_lock_pts:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (B2) 1.5階：中階鎖利 (獲利曾達 +110 點，拉回時鎖住 +75 點，補齊 80~200 點真空區)
                t15_trig = getattr(self.config, "tier1_5_trigger_pts", 110.0)
                t15_lock = getattr(self.config, "tier1_5_lock_pts", 75.0)
                if (self.highest_profit >= t15_trig and 
                    l <= (self.entry_price + t15_lock)):
                    exit_p = self.entry_price + t15_lock
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "BUY",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": t15_lock,
                        "reason": f"中階保本鎖利 (曾獲利+{self.highest_profit:.0f}點, 鎖定+{t15_lock:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (C) 一階：保本鎖利機制 (獲利曾達 +80 點，拉回時鎖住 +30 點)
                if (self.highest_profit >= self.config.breakeven_trigger_pts and 
                    l <= (self.entry_price + self.config.breakeven_lock_pts)):
                    exit_p = self.entry_price + self.config.breakeven_lock_pts
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "BUY",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": self.config.breakeven_lock_pts,
                        "reason": f"一階保本鎖利 (曾獲利+{self.highest_profit:.0f}點, 鎖定+{self.config.breakeven_lock_pts:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (C) 初始固定停損 (含假突破反手 SAR)
                if l <= (self.entry_price - self.config.stop_loss_pts):
                    exit_p = self.entry_price - self.config.stop_loss_pts
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "BUY",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": -self.config.stop_loss_pts,
                        "reason": f"初始停損 (-{self.config.stop_loss_pts:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    # 檢查假突破反手做空 (SAR)
                    if (self.config.enable_sar and 
                        self.trade_count < 2 and 
                        t_str <= self.config.sar_end_time and 
                        c < vwap):
                        self.position = -1
                        self.entry_price = exit_p
                        self.entry_time = ts
                        self.trade_count += 1
                        self.highest_profit = 0.0
                        return {"action": "SAR_SELL", "record": trade_record}
                    else:
                        self.position = 0
                        return {"action": "EXIT", "record": trade_record}

            elif self.position == -1:
                cur_profit = self.entry_price - l
                if cur_profit > self.highest_profit:
                    self.highest_profit = cur_profit

                # (A) 三階：大波段移動停利 (+400點啟動)
                if (self.highest_profit >= self.config.trailing_activation_pts and 
                    h >= (self.entry_price - self.highest_profit + self.config.trailing_distance_pts)):
                    exit_p = self.entry_price - self.highest_profit + self.config.trailing_distance_pts
                    pnl_pts = self.entry_price - exit_p
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "SELL",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": pnl_pts,
                        "reason": f"三階波段移動停利 (極值+{self.highest_profit:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (B) 二階：中波段鎖利 (獲利曾達 +200 點，拉回時鎖住 +120 點)
                if (self.highest_profit >= self.config.tier2_trigger_pts and 
                    h >= (self.entry_price - self.config.tier2_lock_pts)):
                    exit_p = self.entry_price - self.config.tier2_lock_pts
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "SELL",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": self.config.tier2_lock_pts,
                        "reason": f"二階波段鎖利 (曾獲利+{self.highest_profit:.0f}點, 鎖定+{self.config.tier2_lock_pts:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (B2) 1.5階：中階鎖利 (獲利曾達 +110 點，拉回時鎖住 +75 點，補齊 80~200 點真空區)
                t15_trig = getattr(self.config, "tier1_5_trigger_pts", 110.0)
                t15_lock = getattr(self.config, "tier1_5_lock_pts", 75.0)
                if (self.highest_profit >= t15_trig and 
                    h >= (self.entry_price - t15_lock)):
                    exit_p = self.entry_price - t15_lock
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "SELL",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": t15_lock,
                        "reason": f"中階保本鎖利 (曾獲利+{self.highest_profit:.0f}點, 鎖定+{t15_lock:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (C) 一階：保本鎖利機制 (獲利曾達 +80 點，拉回時鎖住 +30 點)
                if (self.highest_profit >= self.config.breakeven_trigger_pts and 
                    h >= (self.entry_price - self.config.breakeven_lock_pts)):
                    exit_p = self.entry_price - self.config.breakeven_lock_pts
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "SELL",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": self.config.breakeven_lock_pts,
                        "reason": f"一階保本鎖利 (曾獲利+{self.highest_profit:.0f}點, 鎖定+{self.config.breakeven_lock_pts:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    self.position = 0
                    return {"action": "EXIT", "record": trade_record}

                # (C) 初始固定停損 (含假跌破反手 SAR)
                if h >= (self.entry_price + self.config.stop_loss_pts):
                    exit_p = self.entry_price + self.config.stop_loss_pts
                    trade_record = {
                        "entry_time": self.entry_time,
                        "exit_time": ts,
                        "action": "SELL",
                        "entry_price": self.entry_price,
                        "exit_price": exit_p,
                        "pnl_pts": -self.config.stop_loss_pts,
                        "reason": f"初始停損 (-{self.config.stop_loss_pts:.0f}點)"
                    }
                    self.trades.append(trade_record)
                    # 檢查假跌破反手做多 (SAR)
                    if (self.config.enable_sar and 
                        self.trade_count < 2 and 
                        t_str <= self.config.sar_end_time and 
                        c > vwap):
                        self.position = 1
                        self.entry_price = exit_p
                        self.entry_time = ts
                        self.trade_count += 1
                        self.highest_profit = 0.0
                        return {"action": "SAR_BUY", "record": trade_record}
                    else:
                        self.position = 0
                        return {"action": "EXIT", "record": trade_record}

        # 3A. 09:01 ~ 09:15 早盤強勢爆發突破進場 (方案 B 精準改良版：空間未過度耗盡 + 蓄勢二次突破位置濾網)
        if (getattr(self.config, "enable_early_super_breakout", True) and
            self.position == 0 and
            self.pre_orb_high is not None and
            self.trade_count < self.config.max_initial_trades and
            "09:01:00" <= t_str <= self.config.orb_end_time):

            long_filter = (not self.config.use_vwap_ema_filter) or (c > vwap and ema9 > ema21)
            short_filter = (not self.config.use_vwap_ema_filter) or (c < vwap and ema9 < ema21)
            sb = self.config.early_super_breakout_pts

            # 方案 B 位置與動能保護濾網 (當 09:15 ORB 已鎖定時生效):
            # 1. 空間未過度耗盡: orb_range <= max_orb_range * 1.25 (過濾如 10/30, 11/05 已狂瀉 500~840 點之地板衰竭追空)
            # 2. 蓄勢位置確認: 緊貼 ORB 突破邊緣 (<= max_chase)，且當根非急拉/急殺極值棒 (h < orb_high / l > orb_low)，且 K 棒實體順勢
            pos_long_ok = True
            pos_short_ok = True
            if self.orb_calculated and self.orb_high is not None and self.orb_low is not None:
                orb_range_3a = self.orb_high - self.orb_low
                max_orb_3a = getattr(self.config, "max_orb_range_pts", 320.0)
                max_ratio_3a = getattr(self.config, "early_breakout_max_range_ratio", 1.25)
                max_chase_3a = getattr(self.config, "max_breakout_chase_pts", 60.0)
                range_ok = (orb_range_3a <= max_orb_3a * max_ratio_3a)
                pos_long_ok = range_ok and ((self.orb_high - c) <= max_chase_3a) and (h < self.orb_high) and (c > o)
                pos_short_ok = range_ok and ((c - self.orb_low) <= max_chase_3a) and (l > self.orb_low) and (c < o)

            if c >= (self.pre_orb_high + sb) and long_filter and pos_long_ok:
                self.position = 1
                self.entry_price = c
                self.entry_time = ts
                self.trade_count += 1
                self.highest_profit = 0.0
                return {
                    "action": "BUY",
                    "price": c,
                    "time": ts,
                    "detail": f"早盤強勢蓄勢突破 ({self.pre_orb_high}+{sb:.0f}點) + 位置/VWAP/EMA 多頭確認"
                }
            elif c <= (self.pre_orb_low - sb) and short_filter and pos_short_ok:
                self.position = -1
                self.entry_price = c
                self.entry_time = ts
                self.trade_count += 1
                self.highest_profit = 0.0
                return {
                    "action": "SELL",
                    "price": c,
                    "time": ts,
                    "detail": f"早盤強勢蓄勢跳水 ({self.pre_orb_low}-{sb:.0f}點) + 位置/VWAP/EMA 空頭確認"
                }

        # 3B. 09:16 ~ 11:30 標準現貨開盤區間突破 + ORB 寬度空間保護 + VWAP & EMA 延遲共振進場
        orb_range = (self.orb_high - self.orb_low) if self.orb_calculated else 0.0
        max_orb_range = getattr(self.config, "max_orb_range_pts", 320.0)
        max_chase = getattr(self.config, "max_breakout_chase_pts", 60.0)

        if (self.position == 0 and 
            self.orb_calculated and 
            orb_range <= max_orb_range and
            self.trade_count < self.config.max_initial_trades and
            self.config.entry_start_time <= t_str <= self.config.entry_end_time):

            long_filter = (not self.config.use_vwap_ema_filter) or (c > vwap and ema9 > ema21)
            short_filter = (not self.config.use_vwap_ema_filter) or (c < vwap and ema9 < ema21)

            # 計算進場門檻 (含夜盤防撞牆保護 Wall Filter)
            buy_threshold = self.orb_high
            sell_threshold = self.orb_low
            wall_filter_active_long = False
            wall_filter_active_short = False

            if getattr(self.config, "use_overnight_wall_filter", False):
                thresh_pts = getattr(self.config, "overnight_wall_threshold_pts", 40.0)
                # 做多防撞牆：若 ORB 上界在夜高下方且距離 <= 門檻點數，需突破夜高才進場
                if (self.overnight_high is not None and 
                    self.orb_high < self.overnight_high and 
                    (self.overnight_high - self.orb_high) <= thresh_pts):
                    buy_threshold = self.overnight_high
                    wall_filter_active_long = True

                # 做空防撞地：若 ORB 下界在夜檔上方且距離 <= 門檻點數，需跌破夜低才進場
                if (self.overnight_low is not None and 
                    self.orb_low > self.overnight_low and 
                    (self.orb_low - self.overnight_low) <= thresh_pts):
                    sell_threshold = self.overnight_low
                    wall_filter_active_short = True

            can_buy = (buy_threshold < c <= (buy_threshold + max_chase)) and long_filter
            can_sell = ((sell_threshold - max_chase) <= c < sell_threshold) and short_filter

            if can_buy:
                self.position = 1
                self.entry_price = c
                self.entry_time = ts
                self.trade_count += 1
                self.highest_profit = 0.0
                detail_str = f"突破現貨開盤高點 ({self.orb_high:.0f})"
                if wall_filter_active_long:
                    detail_str = f"突破夜高防撞牆 ({self.overnight_high:.0f}，原ORB上界{self.orb_high:.0f})"
                return {
                    "action": "BUY", 
                    "price": c, 
                    "time": ts,
                    "detail": f"{detail_str} + VWAP/EMA 多頭確認"
                }

            elif can_sell:
                self.position = -1
                self.entry_price = c
                self.entry_time = ts
                self.trade_count += 1
                self.highest_profit = 0.0
                detail_str = f"跌破現貨開盤低點 ({self.orb_low:.0f})"
                if wall_filter_active_short:
                    detail_str = f"跌破夜低防撞牆 ({self.overnight_low:.0f}，原ORB下界{self.orb_low:.0f})"
                return {
                    "action": "SELL", 
                    "price": c, 
                    "time": ts,
                    "detail": f"{detail_str} + VWAP/EMA 空頭確認"
                }

        return action_result
