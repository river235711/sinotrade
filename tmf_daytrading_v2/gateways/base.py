"""
券商配接抽象介面 (BrokerGateway Port)
讓上層執行引擎 (SessionFSM / PositionGuard) 完全脫鉤永豐金 Shioaji SDK 與模擬撮合細節。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd


@dataclass
class FillResult:
    ok: bool                 # 是否「全部成交」
    filled_qty: int          # 實際成交口數
    avg_price: float         # 實際成交均價 (未成交為 0)
    status: str              # 最終委託狀態


class BrokerGateway(ABC):
    """券商連線、報價訂閱、下單與帳務查詢抽象介面"""

    is_simulation: bool = True
    connected: bool = False
    ca_activated: bool = False
    contract: Any = None
    contract_code: str = ""
    requested_contract_code: str = "TMFR1"

    @abstractmethod
    def connect(self, contract_code: str = "TMFR1") -> bool:
        """登入券商並綁定主力或指定合約"""

    @abstractmethod
    def reconnect(self, contract_code: str = "TMFR1") -> bool:
        """斷線重連並恢復合約綁定與報價訂閱"""

    @abstractmethod
    def resolve_contract(self, code: str = "TMFR1", allow_fallback: bool = True) -> Any:
        """解析 TMF 合約；跨月清倉場景可傳 allow_fallback=False 防止誤平他月合約"""

    @abstractmethod
    def subscribe_ticks(self, on_tick_cb: Callable[[float, int, datetime.datetime], None]) -> bool:
        """訂閱即時逐筆 Tick 報價"""

    @abstractmethod
    def fetch_kbars(self, start_date: str, end_date: str, timeout: int = 5000) -> pd.DataFrame:
        """抓取標準化 1 分K (已對齊起始時間戳 Start Timestamp)"""

    @abstractmethod
    def fetch_snapshot_price(self, today_str: str) -> Optional[Tuple[float, int, datetime.datetime]]:
        """抓取今日日盤有效快照 (close, volume, timestamp)；無效或非今日日盤回傳 None"""

    @abstractmethod
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
        """送出買/賣委託並確認成交結果"""

    @abstractmethod
    def query_position_details(self, contract_code: Optional[str] = None) -> Dict[str, Any]:
        """查詢帳上實際/模擬部位詳細資訊 (含 target contract 與 other_positions)"""

    @abstractmethod
    def cancel_pending_orders(self) -> None:
        """緊急撤銷所有未成交掛單"""

    @abstractmethod
    def close(self) -> None:
        """關閉券商連線與釋放資源"""

    def keep_alive(self) -> bool:
        """定時連線保活檢查 (預設回傳目前連線狀態)"""
        return bool(self.connected)
