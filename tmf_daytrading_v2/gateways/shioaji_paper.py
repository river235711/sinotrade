"""
模擬交易配接器 (Paper Trading Gateway Adapter)
即時行情透過 Shioaji 模擬連線接收，委託撮合與帳務部位則由本地虛擬帳本精準維護，
並實作與 ShioajiLiveGateway 完全一致的 BrokerGateway 介面。
"""
import datetime
import logging
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

from config import ShioajiConfig
from gateways.base import BrokerGateway, FillResult
from gateways.shioaji_live import (
    is_today_day_session_snapshot,
    normalize_shioaji_kbars,
    resolve_tmf_contract,
)

logger = logging.getLogger("TMFTrader.ShioajiPaper")


class ShioajiPaperGateway(BrokerGateway):
    """Shioaji 即時行情 + 本地模擬撮合帳本"""

    def __init__(self, sj_cfg: Optional[ShioajiConfig] = None, contract_code: str = "TMFR1"):
        self.sj_cfg = sj_cfg or ShioajiConfig()
        self.is_simulation = True
        self.api: Any = None
        self.contract: Any = None
        self.requested_contract_code: str = contract_code or "TMFR1"
        self.contract_code: str = self.requested_contract_code
        self.connected: bool = False
        self.ca_activated: bool = True
        self._tick_cb: Optional[Callable[[float, int, datetime.datetime], None]] = None
        self._last_price: float = 0.0
        # 虛擬持倉帳本 {contract_code: {"net": int, "avg_price": float}}
        self._ledger: Dict[str, Dict[str, Any]] = {}

    def connect(self, contract_code: str = "") -> bool:
        import shioaji as sj

        if contract_code:
            self.requested_contract_code = contract_code
        target_req = contract_code or self.requested_contract_code or "TMFR1"
        if not self.sj_cfg.api_key or not self.sj_cfg.secret_key:
            logger.error("❌ 未設定 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY！")
            return False

        api = None
        try:
            self.connected = False
            api = sj.Shioaji(simulation=True)
            logger.info("📡 正在連線至永豐金 Shioaji [模擬行情] 伺服器...")
            api.login(api_key=self.sj_cfg.api_key, secret_key=self.sj_cfg.secret_key)

            self.api = api
            contract = self.resolve_contract(target_req, allow_fallback=True)
            if contract is None:
                logger.error(f"❌ 找不到合約代碼: {target_req}")
                self.close()
                return False
            self.contract = contract
            self.contract_code = getattr(contract, "code", target_req)
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"❌ Shioaji 模擬連線/登入失敗: {e}")
            if api is not None:
                try:
                    api.logout()
                except Exception:
                    pass
            self.api = None
            self.contract = None
            self.connected = False
            return False

    def reconnect(self, contract_code: str = "") -> bool:
        try:
            self.close()
        except Exception:
            pass
        target_req = contract_code or self.requested_contract_code or "TMFR1"
        ok = self.connect(target_req)
        if ok and self._tick_cb is not None:
            self.subscribe_ticks(self._tick_cb)
        return ok

    def resolve_contract(self, code: str = "TMFR1", allow_fallback: bool = True) -> Any:
        return resolve_tmf_contract(self.api, code=code, allow_fallback=allow_fallback)

    def subscribe_ticks(self, on_tick_cb: Callable[[float, int, datetime.datetime], None]) -> bool:
        self._tick_cb = on_tick_cb
        if self.api is None or self.contract is None:
            return False

        @self.api.on_tick_fop_v1()
        def _on_tick(exchange, tick):
            try:
                if getattr(tick, "simtrade", 0) == 1:
                    return
                price = float(getattr(tick, "close", 0.0))
                volume = int(getattr(tick, "volume", 0))
                dt = getattr(tick, "datetime", None) or datetime.datetime.now()
                if price > 0:
                    self._last_price = price
                    on_tick_cb(price, volume, dt)
            except Exception:
                pass

        try:
            self.api.quote.subscribe(self.contract, quote_type="tick", version="v1")
            return True
        except Exception as e:
            logger.error(f"❌ 訂閱模擬 Tick 行情失敗: {e}")
            return False

    def fetch_kbars(self, start_date: str, end_date: str, timeout: int = 5000) -> pd.DataFrame:
        if self.api is None or self.contract is None:
            return pd.DataFrame()
        raw = self.api.kbars(self.contract, start=start_date, end=end_date, timeout=timeout)
        return normalize_shioaji_kbars(raw, subtract_minute=True)

    def fetch_snapshot_price(self, today_str: str) -> Optional[Tuple[float, int, datetime.datetime]]:
        if self.api is None or self.contract is None:
            return None
        try:
            snaps = self.api.snapshots([self.contract])
            if not snaps:
                return None
            snap = snaps[0]
            if not is_today_day_session_snapshot(snap, today_str):
                return None
            price = float(getattr(snap, "close", 0.0) or 0.0)
            if price <= 0:
                return None
            self._last_price = price
            vol = int(getattr(snap, "volume", 1) or 1)
            return price, vol, datetime.datetime.now()
        except Exception:
            return None

    def set_last_price(self, price: float) -> None:
        if price > 0:
            self._last_price = float(price)

    def seed_position(self, net: int, avg_price: float, code: Optional[str] = None) -> None:
        """供啟動狀態接管或測試寫入初始模擬帳本部位"""
        c = code or self.contract_code or "TMF"
        if net == 0:
            self._ledger.pop(c, None)
        else:
            self._ledger[c] = {"net": int(net), "avg_price": float(avg_price)}

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
        """模擬即時成交並更新虛擬持倉帳本"""
        c_code = getattr(contract_override, "code", None) if contract_override else None
        c_code = c_code or self.contract_code or "TMF"
        fill_px = float(price if price > 0 else self._last_price)
        if fill_px <= 0:
            return FillResult(ok=False, filled_qty=0, avg_price=0.0, status="NoPrice")

        signed = int(qty) if action == "BUY" else -int(qty)
        cur = self._ledger.get(c_code, {"net": 0, "avg_price": 0.0})
        cur_net = int(cur["net"])
        new_net = cur_net + signed

        if new_net == 0:
            self._ledger.pop(c_code, None)
        elif (cur_net == 0) or (cur_net * signed > 0):
            tot = abs(cur_net) + abs(signed)
            avg = ((cur["avg_price"] * abs(cur_net)) + (fill_px * abs(signed))) / tot
            self._ledger[c_code] = {"net": new_net, "avg_price": avg}
        elif abs(signed) < abs(cur_net):
            self._ledger[c_code] = {"net": new_net, "avg_price": cur["avg_price"]}
        else:
            self._ledger[c_code] = {"net": new_net, "avg_price": fill_px}

        return FillResult(ok=True, filled_qty=int(qty), avg_price=fill_px, status="Filled")

    def query_position_details(self, contract_code: Optional[str] = None) -> Dict[str, Any]:
        target = contract_code if contract_code is not None else self.contract_code
        filter_code = (
            target if (isinstance(target, str) and target and not target.endswith("R1")) else None
        )
        cur_net = 0
        cur_avg = 0.0
        tmf_positions = []
        other_positions = []

        for code, b in self._ledger.items():
            net = int(b["net"])
            if net == 0:
                continue
            d = "Buy" if net > 0 else "Sell"
            tmf_positions.append({"code": code, "direction": d, "qty": abs(net), "price": b["avg_price"]})
            if filter_code is None or code == filter_code:
                cur_net += net
                cur_avg = b["avg_price"]
            else:
                other_positions.append({
                    "code": code,
                    "net": net,
                    "qty": abs(net),
                    "direction": d,
                    "avg_price": b["avg_price"],
                })

        direction = "Buy" if cur_net > 0 else ("Sell" if cur_net < 0 else "Flat")
        return {
            "net": cur_net,
            "avg_price": cur_avg,
            "direction": direction,
            "positions": tmf_positions,
            "other_positions": other_positions,
            "target_code": filter_code,
        }

    def cancel_pending_orders(self) -> None:
        return None

    def close(self) -> None:
        if self.api is not None:
            try:
                self.api.logout()
            except Exception:
                pass
            self.api = None
        self.connected = False
