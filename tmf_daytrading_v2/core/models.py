"""
領域模型定義 (Pure Domain Models)
完全不依賴 Shioaji、網路或檔案 I/O。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from config import ORBStrategyConfig


class SessionPhase(str, Enum):
    """台指日盤 9 階段交易生命週期"""
    PREMARKET_STANDBY = "PREMARKET_STANDBY"   # 00:00~08:34:59 / 跨日待機
    HEALTH_CHECK = "HEALTH_CHECK"             # 08:35~08:44:59 盤前 7 大系統健檢
    PRE_ORB_BUILD = "PRE_ORB_BUILD"           # 08:45~08:59:59 早盤期貨前導區間建立
    ORB_BUILD = "ORB_BUILD"                   # 09:00~09:15:59 現貨開盤區間建立 + 早盤強勢爆發觀察
    EARLY_BREAKOUT = "EARLY_BREAKOUT"         # 09:01~09:15:59 早盤 3A 強勢突破窗口 (與 ORB_BUILD 重疊細分)
    MAIN_BREAKOUT = "MAIN_BREAKOUT"           # 09:16~11:30:00 標準 ORB 突破進場與 SAR 窗口
    POST_ENTRY_HOLD = "POST_ENTRY_HOLD"       # 11:30:01~13:07:59 停止新進場，純持倉守護 / 鎖利 / 移動停利
    FORCE_CLOSE = "FORCE_CLOSE"               # 13:08:00~13:10:00 強制平倉 + Failsafe + 收盤零部位確認
    POST_CLOSE_SETTLE = "POST_CLOSE_SETTLE"   # 13:10:01~23:59:59 盤後結算報告 + 滾動資料歸檔


@dataclass(frozen=True)
class Bar:
    """標準化 1 分K 資料結構"""
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class EnrichedBar:
    """含技術指標的 1 分K 資料結構"""
    bar: Bar
    vwap: float
    ema9: float
    ema21: float


@dataclass(frozen=True)
class TradeRecord:
    """單筆已實現交易明細 (不可變值物件)"""
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    action: str          # "BUY" | "SELL" (原部位方向)
    entry_price: float
    exit_price: float
    pnl_pts: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "action": self.action,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl_pts": self.pnl_pts,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OrderIntent:
    """
    策略對單根 K 棒評估後產生之純交易意圖。
    注意：evaluate_bar 只回傳 OrderIntent，絕不直接修改 TradingDayState 或下單。
    """
    action: str = "HOLD"                # "HOLD" | "BUY" | "SELL" | "EXIT" | "SAR_BUY" | "SAR_SELL"
    side_sign: int = 0                  # +1 多 / -1 空 (進場或 SAR 新方向)
    price: float = 0.0                  # 理論訊號價格
    reason: str = ""
    pnl_pts: float = 0.0                # 若有平倉，該段平倉理論點數
    closed_side_action: str = ""        # 若有平倉，原部位方向 ("BUY" | "SELL")
    is_sar: bool = False
    new_highest_profit: float = 0.0
    hp_before_bar_ts: Optional[pd.Timestamp] = None
    hp_before_bar_val: float = 0.0


@dataclass(frozen=True)
class TradingDayState:
    """
    單一交易日的完整不可變狀態容器。
    所有策略部位、ORB 區間與每日排程旗標皆集中於此；
    跨日換日時直接建立全新 TradingDayState 實例，從結構上消除漏重置任何旗標的風險。
    """
    trading_date: str = ""
    contract_code: str = ""
    pre_orb_high: Optional[float] = None
    pre_orb_low: Optional[float] = None
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    orb_calculated: bool = False
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None

    position: int = 0                   # 0: 空手, 1: 多單, -1: 空單
    entry_price: float = 0.0
    entry_time: Optional[pd.Timestamp] = None
    highest_profit: float = 0.0
    hp_before_bar_ts: Optional[pd.Timestamp] = None
    hp_before_bar_val: float = 0.0
    trade_count: int = 0
    entries_halted: bool = False
    trades: Tuple[TradeRecord, ...] = ()

    # 每日排程旗標 (隨 TradingDayState 換日自動歸零)
    morning_wake_logged: bool = False
    health_check_sent: bool = False
    orb_finalized: bool = False
    orb_report_sent: bool = False
    offline_hc_alert_sent: bool = False
    post_close_done: bool = False
    settlement_alert_sent: bool = False
    eod_roll_done: bool = False
    holiday_detected: bool = False

    effective_config: Optional[ORBStrategyConfig] = None
    atr_info: Optional[Dict[str, Any]] = None
