"""
永豐金 Shioaji 實盤配接器 (Live Broker Gateway Adapter)
專責：API 登入、CA 憑證啟用、合約解析 (TMFR1 -> 實際交易月)、逐筆行情訂閱、150ms 下單輪詢與實體帳務核對。
"""
import datetime
import logging
import os
import time
import warnings
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

from config import ShioajiConfig
from gateways.base import BrokerGateway, FillResult

logger = logging.getLogger("TMFTrader.ShioajiLive")

FINAL_STATUSES = {"Filled", "Cancelled", "Failed", "Inactive"}
PENDING_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}


def status_name(s: Any) -> str:
    """把 Shioaji 列舉值統一轉成純文字，如 Status.Filled -> 'Filled'"""
    v = getattr(s, "value", s)
    return str(v).split(".")[-1]


def resolve_tmf_contract(api: Any, code: str = "TMFR1", allow_fallback: bool = True) -> Any:
    """
    從 Shioaji API 解析 TMF 主力或指定合約；若為連續月合約 (如 TMFR1) 自動解出實際可下單月份合約。
    若 allow_fallback=False 且查無指定合約則直接回傳 None (用於跨月平倉防呆)。
    """
    if api is None:
        return None

    tmf_contracts = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            tmf_contracts = list(api.contracts.futures("TMF"))
        except Exception:
            tmf_contracts = []
        if not tmf_contracts:
            futs = getattr(getattr(getattr(api, "Contracts", None), "Futures", None), "TMF", None)
            if futs is not None:
                tmf_contracts = list(futs)

    if not tmf_contracts:
        return None

    def _unwrap(c: Any) -> Any:
        return getattr(c, "contract", c)

    selected = None
    target_code_req = code or "TMFR1"
    for c in tmf_contracts:
        raw_c = _unwrap(c)
        c_code = getattr(raw_c, "code", getattr(c, "code", ""))
        c_name = getattr(raw_c, "name", getattr(c, "name", "")) or ""
        if c_code == target_code_req or (c_name and target_code_req in c_name):
            selected = raw_c
            break

    if selected is None and allow_fallback:
        for c in tmf_contracts:
            raw_c = _unwrap(c)
            if getattr(raw_c, "code", getattr(c, "code", "")) == "TMFR1":
                selected = raw_c
                break

    if selected is None:
        return None

    resolved_target = getattr(selected, "target_code", None)
    if resolved_target:
        for c in tmf_contracts:
            raw_c = _unwrap(c)
            if getattr(raw_c, "code", getattr(c, "code", "")) == resolved_target:
                logger.info(
                    f"自動將連續合約 {getattr(selected, 'code', target_code_req)} "
                    f"解析為當月主力可下單合約: {resolved_target} ({getattr(raw_c, 'name', '')})"
                )
                return raw_c

    return selected


def normalize_shioaji_kbars(raw_kbars: Any, subtract_minute: bool = True) -> pd.DataFrame:
    """將 Shioaji api.kbars 回傳標準化為以 ts (Start Timestamp) 為索引的 OHLCV DataFrame"""
    if raw_kbars is None:
        return pd.DataFrame()
    df = raw_kbars.copy() if isinstance(raw_kbars, pd.DataFrame) else pd.DataFrame({**raw_kbars})
    if df.empty:
        return pd.DataFrame()
    df.columns = [str(c).lower() for c in df.columns]
    if "ts" not in df.columns:
        return pd.DataFrame()
    df["ts"] = pd.to_datetime(df["ts"])
    if subtract_minute:
        df["ts"] = df["ts"] - pd.Timedelta(minutes=1)
    return df.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").set_index("ts")


def is_today_day_session_snapshot(snap: Any, today_str: str) -> bool:
    """驗證 Shioaji 快照是否屬於「今日日盤 (08:45:00 ~ 13:46:00)」"""
    snap_ts_raw = getattr(snap, "ts", None)
    if snap_ts_raw is None or not isinstance(snap_ts_raw, (int, float, str, datetime.datetime, pd.Timestamp)):
        return True
    try:
        if isinstance(snap_ts_raw, (int, float)):
            if snap_ts_raw <= 0:
                return False
            snap_dt = pd.to_datetime(int(snap_ts_raw)).to_pydatetime()
        else:
            snap_dt = pd.to_datetime(snap_ts_raw).to_pydatetime()
        return (
            snap_dt.strftime("%Y-%m-%d") == today_str
            and datetime.time(8, 45, 0) <= snap_dt.time() <= datetime.time(13, 46, 0)
        )
    except Exception:
        return False


def query_real_position_details(
    api: Any,
    simulation: bool,
    reconnect_cb: Optional[Callable[[], Any]] = None,
    contract_code: Optional[str] = None,
) -> Dict[str, Any]:
    """查詢券商帳上 TMF 詳細部位資訊 (區分當前目標合約與跨月/結算殘倉 other_positions)"""
    if api is None or simulation:
        return {
            "net": None,
            "avg_price": 0.0,
            "direction": "None",
            "positions": [],
            "other_positions": [],
            "target_code": None,
        }
    try:
        positions = api.list_positions(api.futopt_account)
    except Exception as e:
        logger.error(f"查詢帳上部位詳細資訊失敗: {e}")
        err_str = str(e).lower()
        if reconnect_cb and any(k in err_str for k in ("authenticated", "session", "token", "disconnect", "connect")):
            reconnect_cb()
        return {
            "net": None,
            "avg_price": 0.0,
            "direction": "None",
            "positions": [],
            "other_positions": [],
            "target_code": None,
        }

    filter_code = (
        contract_code
        if (isinstance(contract_code, str) and contract_code and not contract_code.endswith("R1"))
        else None
    )
    tmf_positions = []
    by_code: Dict[str, Dict[str, Any]] = {}

    for p in positions or []:
        code = str(getattr(p, "code", ""))
        if not code.startswith("TMF"):
            continue
        qty = int(getattr(p, "quantity", 0))
        d = status_name(getattr(p, "direction", ""))
        price = float(getattr(p, "price", 0.0))
        tmf_positions.append({"code": code, "direction": d, "qty": qty, "price": price})

        bucket = by_code.setdefault(
            code,
            {"net": 0, "buy_qty": 0, "buy_cost": 0.0, "sell_qty": 0, "sell_cost": 0.0},
        )
        if d == "Buy":
            bucket["net"] += qty
            bucket["buy_qty"] += qty
            bucket["buy_cost"] += price * qty
        else:
            bucket["net"] -= qty
            bucket["sell_qty"] += qty
            bucket["sell_cost"] += price * qty

    net = 0
    buy_qty = 0
    buy_cost = 0.0
    sell_qty = 0
    sell_cost = 0.0
    other_positions = []

    for code, bucket in by_code.items():
        if filter_code is None or code == filter_code:
            net += bucket["net"]
            buy_qty += bucket["buy_qty"]
            buy_cost += bucket["buy_cost"]
            sell_qty += bucket["sell_qty"]
            sell_cost += bucket["sell_cost"]
        elif bucket["net"] != 0:
            o_net = bucket["net"]
            o_qty = bucket["buy_qty"] if o_net > 0 else bucket["sell_qty"]
            o_cost = bucket["buy_cost"] if o_net > 0 else bucket["sell_cost"]
            other_positions.append({
                "code": code,
                "net": o_net,
                "qty": abs(o_net),
                "direction": "Buy" if o_net > 0 else "Sell",
                "avg_price": (o_cost / o_qty) if o_qty > 0 else 0.0,
            })

    if net > 0 and buy_qty > 0:
        avg_price = buy_cost / buy_qty
    elif net < 0 and sell_qty > 0:
        avg_price = sell_cost / sell_qty
    else:
        avg_price = 0.0

    direction = "Buy" if net > 0 else ("Sell" if net < 0 else "Flat")
    return {
        "net": net,
        "avg_price": avg_price,
        "direction": direction,
        "positions": tmf_positions,
        "other_positions": other_positions,
        "target_code": filter_code,
    }


def wait_for_order_fill(
    api: Any, trade: Any, qty: int, fill_timeout: float, poll_interval: float
) -> FillResult:
    """150ms 輪詢委託狀態直到終態；逾時仍未成交則撤單"""
    deadline = time.time() + fill_timeout
    status = status_name(trade.status.status)

    while True:
        try:
            api.update_status(api.futopt_account)
        except Exception as e:
            logger.warning(f"更新委託狀態失敗: {e}")
        status = status_name(trade.status.status)
        if status in FINAL_STATUSES or time.time() >= deadline:
            break
        time.sleep(poll_interval)

    if status in PENDING_STATUSES:
        logger.warning(f"⚠️ 委託 {fill_timeout:.0f} 秒仍為 {status}，嘗試撤單...")
        try:
            api.cancel_order(trade)
            time.sleep(poll_interval)
            api.update_status(api.futopt_account)
        except Exception as e:
            logger.error(f"撤單失敗: {e}")
        status = status_name(trade.status.status)

    deals = list(getattr(trade.status, "deals", None) or [])
    filled = sum(int(d.quantity) for d in deals)
    avg_price = (sum(float(d.price) * int(d.quantity) for d in deals) / filled) if filled else 0.0
    if filled == 0:
        filled = int(getattr(trade.status, "deal_quantity", 0) or 0)
    if filled == 0 and status == "Filled":
        filled = qty
    return FillResult(ok=filled >= qty, filled_qty=filled, avg_price=avg_price, status=status)


class ShioajiLiveGateway(BrokerGateway):
    """永豐金 Shioaji 實盤閘道器"""

    def __init__(self, sj_cfg: Optional[ShioajiConfig] = None, contract_code: str = "TMFR1"):
        self.sj_cfg = sj_cfg or ShioajiConfig()
        self.is_simulation = False
        self.api: Any = None
        self.contract: Any = None
        self.contract_code: str = contract_code
        self.connected: bool = False
        self.ca_activated: bool = False
        self._tick_cb: Optional[Callable[[float, int, datetime.datetime], None]] = None

    def connect(self, contract_code: str = "TMFR1") -> bool:
        import shioaji as sj

        self.contract_code = contract_code or self.contract_code or "TMFR1"
        if not self.sj_cfg.api_key or not self.sj_cfg.secret_key:
            logger.error("❌ 未設定 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY！")
            return False

        self.api = sj.Shioaji(simulation=False)
        logger.info("📡 正在連線至永豐金 Shioaji [實盤] 伺服器...")
        self.api.login(api_key=self.sj_cfg.api_key, secret_key=self.sj_cfg.secret_key)

        self.ca_activated = False
        ca_path = os.path.expanduser(self.sj_cfg.ca_path or "")
        if ca_path and os.path.exists(ca_path):
            self.api.activate_ca(
                ca_path=ca_path,
                ca_passwd=self.sj_cfg.ca_passwd,
                person_id=self.sj_cfg.person_id,
            )
            self.ca_activated = True
            logger.info("✅ 實盤數位憑證 (CA) 驗證通過！")
        else:
            logger.warning(f"⚠️ 找不到憑證檔 ({ca_path})，實盤下單可能失敗。")

        self.contract = self.resolve_contract(self.contract_code, allow_fallback=True)
        if self.contract is None:
            logger.error(f"❌ 找不到合約代碼: {self.contract_code}")
            return False
        self.contract_code = getattr(self.contract, "code", self.contract_code)
        self.connected = True
        return True

    def reconnect(self, contract_code: str = "TMFR1") -> bool:
        logger.warning("🔄 正在重新連線 Shioaji 實盤伺服器...")
        try:
            self.close()
        except Exception:
            pass
        ok = self.connect(contract_code or self.contract_code)
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
                    on_tick_cb(price, volume, dt)
            except Exception:
                pass

        self.api.quote.subscribe(self.contract, quote_type="tick", version="v1")
        return True

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
            vol = int(getattr(snap, "volume", 1) or 1)
            return price, vol, datetime.datetime.now()
        except Exception:
            return None

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
        if self.api is None:
            return FillResult(ok=False, filled_qty=0, avg_price=0.0, status="NotConnected")
        target_contract = contract_override or self.contract
        if target_contract is None:
            return FillResult(ok=False, filled_qty=0, avg_price=0.0, status="NoContract")

        import shioaji as sj
        sj_action = sj.constant.Action.Buy if action == "BUY" else sj.constant.Action.Sell
        sj_price_type = (
            sj.constant.FuturesPriceType.MKP
            if price_type == "MKP"
            else (
                sj.constant.FuturesPriceType.MKT
                if price_type == "MKT"
                else sj.constant.FuturesPriceType.LMT
            )
        )
        sj_octype = (
            sj.constant.FuturesOCType.New
            if octype == "New"
            else (
                sj.constant.FuturesOCType.Cover
                if octype == "Cover"
                else sj.constant.FuturesOCType.Auto
            )
        )
        sj_tif = sj.constant.TimeInForce.IOC if price_type in ("MKP", "MKT") else sj.constant.TimeInForce.ROD
        order = self.api.Order(
            action=sj_action,
            price=float(price) if price_type == "LMT" else 0.0,
            quantity=int(qty),
            price_type=sj_price_type,
            order_type=sj_tif,
            octype=sj_octype,
        )
        try:
            trade = self.api.place_order(target_contract, order)
        except Exception as e:
            logger.error(f"❌ Shioaji 送單失敗: {e}")
            return FillResult(ok=False, filled_qty=0, avg_price=0.0, status=f"Error:{e}")
        return wait_for_order_fill(self.api, trade, int(qty), fill_timeout=fill_timeout, poll_interval=poll_interval)

    def query_position_details(self, contract_code: Optional[str] = None) -> Dict[str, Any]:
        target = contract_code if contract_code is not None else self.contract_code
        return query_real_position_details(
            self.api,
            simulation=False,
            reconnect_cb=self.reconnect,
            contract_code=target,
        )

    def cancel_pending_orders(self) -> None:
        if self.api is None:
            return
        try:
            self.api.update_status(self.api.futopt_account)
            for trade in self.api.list_trades():
                if status_name(trade.status.status) in PENDING_STATUSES:
                    self.api.cancel_order(trade)
        except Exception as e:
            logger.error(f"緊急撤單異常: {e}")

    def close(self) -> None:
        if self.api is not None:
            try:
                self.api.logout()
            except Exception:
                pass
            self.api = None
        self.connected = False
        self.ca_activated = False
