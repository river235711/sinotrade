# -*- coding: utf-8 -*-
"""
委託下單、成交確認、帳務查詢與稽核紀錄模組
"""

import os
import csv
import time
import logging
import datetime
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("ShioajiTrader")

FINAL_STATUSES = {"Filled", "Cancelled", "Failed", "Inactive"}
PENDING_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}


def status_name(s: Any) -> str:
    """把 Shioaji 的列舉值統一轉成純文字，如 Status.Filled -> 'Filled'"""
    v = getattr(s, "value", s)
    return str(v).split(".")[-1]


@dataclass
class FillResult:
    ok: bool                 # 是否「全部成交」
    filled_qty: int          # 實際成交口數
    avg_price: float         # 實際成交均價 (未成交為 0)
    status: str              # 最終委託狀態


def query_real_position_details(api: Any, simulation: bool, reconnect_cb=None) -> dict:
    """
    查詢券商帳上 TMF 詳細部位資訊 (以券商帳上為唯一真實依據 Single Source of Truth)
    回傳: {"net": int | None, "avg_price": float, "direction": str, "positions": list}
    """
    if api is None or simulation:
        return {"net": None, "avg_price": 0.0, "direction": "None", "positions": []}
    try:
        positions = api.list_positions(api.futopt_account)
    except Exception as e:
        logger.error(f"查詢帳上部位詳細資訊失敗: {e}")
        err_str = str(e).lower()
        if reconnect_cb and any(k in err_str for k in ("authenticated", "session", "token", "disconnect", "connect")):
            reconnect_cb()
        return {"net": None, "avg_price": 0.0, "direction": "None", "positions": []}

    net = 0
    tmf_positions = []
    total_cost = 0.0
    total_qty = 0

    for p in positions:
        code = str(getattr(p, "code", ""))
        if not code.startswith("TMF"):
            continue
        qty = int(getattr(p, "quantity", 0))
        d = status_name(getattr(p, "direction", ""))
        price = float(getattr(p, "price", 0.0))
        tmf_positions.append({"code": code, "direction": d, "qty": qty, "price": price})
        if d == "Buy":
            net += qty
        else:
            net -= qty
        total_cost += price * qty
        total_qty += qty

    avg_price = (total_cost / total_qty) if total_qty > 0 else 0.0
    direction = "Buy" if net > 0 else ("Sell" if net < 0 else "Flat")
    return {"net": net, "avg_price": avg_price, "direction": direction, "positions": tmf_positions}


def wait_for_order_fill(api: Any, trade: Any, qty: int, fill_timeout: float, poll_interval: float) -> FillResult:
    """輪詢委託狀態直到終態；逾時仍未成交就撤單，避免單子之後才偷偷成交"""
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


def append_order_audit(
    log_dir: str,
    action: str,
    qty: int,
    octype: str,
    result: FillResult,
    signal_price: Optional[float] = None,
    now_dt: Optional[datetime.datetime] = None,
):
    """
    把每次委託結果寫入 logs/<live|paper>/orders_YYYYMMDD.csv。
    新建立的稽核表會包含 signal_price 欄位供事後統計實盤滑價分佈；
    若遇到舊版 8 欄已存在檔案，則相容維持既有欄位數寫入避免錯欄。
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
        now = now_dt or datetime.datetime.now()
        path = os.path.join(log_dir, f"orders_{now.strftime('%Y%m%d')}.csv")
        is_new = not os.path.exists(path)
        has_signal_col = True
        if not is_new:
            try:
                with open(path, "r", encoding="utf-8") as rf:
                    first_line = rf.readline().strip()
                has_signal_col = "signal_price" in first_line.split(",")
            except Exception:
                has_signal_col = False

        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["time", "action", "qty", "octype", "status", "filled_qty", "avg_price", "ok", "signal_price"])
                has_signal_col = True
            row = [
                now.strftime("%H:%M:%S"),
                action,
                qty,
                octype,
                result.status,
                result.filled_qty,
                f"{result.avg_price:.0f}",
                result.ok,
            ]
            if has_signal_col:
                sig_str = f"{float(signal_price):.0f}" if (signal_price is not None and float(signal_price) > 0) else ""
                row.append(sig_str)
            w.writerow(row)
    except Exception as e:
        logger.warning(f"寫入稽核紀錄失敗: {e}")
