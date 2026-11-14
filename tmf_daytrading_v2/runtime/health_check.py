#!/usr/bin/env python3
"""
開盤前自動健康巡檢模組 (Pre-Market Health Check - V2)
"""
import datetime
import logging
import os
import sys
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import ORBStrategyConfig
from core.atr_scaler import describe
from gateways.data_repo import config_for_date
from gateways.shioaji_live import resolve_tmf_contract
from runtime.notifier import TradeNotifier

logger = logging.getLogger("HealthCheck")


def run_health_check(
    mode: str = "live",
    qty: int = 1,
    send_notify: bool = True,
    api: Any = None,
    contract: Any = None,
    ca_activated: Optional[bool] = None,
) -> Dict[str, Any]:
    """執行開盤前 7 大系統健康巡檢"""
    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    is_live = mode.lower() == "live"

    notifier = TradeNotifier()
    data_dir = os.path.join(BASE_DIR, "data")
    base_cfg = ORBStrategyConfig()
    strat_cfg, atr_info = config_for_date(base_cfg, data_dir, today_str)

    results: Dict[str, Any] = {
        "timestamp": now_str,
        "mode": "實盤 (Live)" if is_live else "模擬 (Paper)",
        "qty": qty,
        "checks": {},
        "overall_ok": True,
        "has_warnings": False,
        "alerts": [],
    }

    print("=" * 70)
    print(f"🏥 TMF 微型臺指期當沖系統 (V2) - 開盤前系統健康巡檢 ({now_str})")
    print(f"• 交易模式: {'🔴 實盤 (Live)' if is_live else '🟢 模擬 (Paper)'} | 預計口數: {qty} 口")
    print("=" * 70)

    # 1. 通知推播通道
    line_ok = notifier.line_messaging_enabled
    email_ok = notifier.email_enabled
    if line_ok or email_ok:
        status_str = f"正常 (LINE: {'✅' if line_ok else '❌'}, Gmail: {'✅' if email_ok else '❌'})"
        results["checks"]["通知推播"] = {"status": "PASS", "detail": status_str}
        print(f"1. [通知推播] ✅ {status_str}")
    else:
        results["checks"]["通知推播"] = {"status": "WARN", "detail": "未設定 LINE 或 Gmail"}
        results["has_warnings"] = True
        print("1. [通知推播] ⚠️ 未設定通知通道，將無法接收即時推播")

    # 2. 今日 ATR 參數
    try:
        param_desc = describe(strat_cfg, atr_info)
        results["checks"]["今日參數"] = {"status": "PASS", "detail": param_desc}
        print(f"2. [今日參數] ✅ 載入成功：\n   {param_desc}")
    except Exception as e:
        results["checks"]["今日參數"] = {"status": "FAIL", "detail": str(e)}
        results["overall_ok"] = False
        results["alerts"].append(f"今日參數載入失敗: {e}")
        print(f"2. [今日參數] ❌ 載入失敗: {e}")

    # 3. Shioaji API 連線
    own_session = api is None
    account = None
    try:
        if own_session:
            import shioaji as sj
            api = sj.Shioaji(simulation=not is_live)
            api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
            secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
            if not api_key or not secret_key:
                raise ValueError(".env 未設定 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY")
            api.login(api_key=api_key, secret_key=secret_key)
        account = getattr(api, "futopt_account", None)
        if not account:
            raise ValueError("登入成功但查無期貨交易帳號 (futopt_account)")
        acc_desc = f"{account.broker_id}-{account.account_id} ({account.username})"
        reuse_tag = "複用既有連線" if not own_session else "登入成功"
        results["checks"]["API連線"] = {"status": "PASS", "detail": f"{reuse_tag} ({acc_desc})"}
        print(f"3. [API連線]  ✅ {reuse_tag} ({acc_desc})")
    except Exception as e:
        results["checks"]["API連線"] = {"status": "FAIL", "detail": str(e)}
        results["overall_ok"] = False
        results["alerts"].append(f"Shioaji API 登入失敗: {e}")
        print(f"3. [API連線]  ❌ 登入失敗: {e}")

    # 4. 合約解析
    contract_bound = contract
    if api and results["overall_ok"]:
        try:
            if contract_bound is None:
                contract_bound = resolve_tmf_contract(api, code="TMFR1")
            if contract_bound:
                c_code = getattr(contract_bound, "code", "")
                c_name = getattr(contract_bound, "name", "")
                results["checks"]["交易合約"] = {"status": "PASS", "detail": f"{c_code} - {c_name}"}
                print(f"4. [交易合約] ✅ 解析主力成功: {c_code} ({c_name})")
            else:
                raise ValueError("無法解析當月主力微台合約 (TMFR1)")
        except Exception as e:
            results["checks"]["交易合約"] = {"status": "FAIL", "detail": str(e)}
            results["overall_ok"] = False
            results["alerts"].append(f"合約解析失敗: {e}")
            print(f"4. [交易合約] ❌ 合約解析失敗: {e}")

    # 5. 下單憑證
    if is_live and api:
        ca_path = os.path.expanduser(os.environ.get("SHIOAJI_CA_PATH", "").strip())
        ca_passwd = os.environ.get("SHIOAJI_CA_PASSWD", "").strip()
        person_id = os.environ.get("SHIOAJI_PERSON_ID", "").strip()
        if not ca_path or not os.path.exists(ca_path):
            results["checks"]["下單憑證"] = {"status": "FAIL", "detail": f"憑證檔不存在: {ca_path}"}
            results["overall_ok"] = False
            results["alerts"].append(f"憑證檔不存在: {ca_path}")
            print(f"5. [下單憑證] ❌ 憑證檔不存在: {ca_path}")
        elif not own_session:
            if ca_activated is False:
                results["checks"]["下單憑證"] = {"status": "FAIL", "detail": "主連線尚未啟用憑證"}
                results["overall_ok"] = False
                results["alerts"].append("主連線尚未成功啟用實盤下單憑證！")
            else:
                results["checks"]["下單憑證"] = {"status": "PASS", "detail": "憑證檔存在 (已隨主連線啟用)"}
        else:
            try:
                api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd, person_id=person_id)
                results["checks"]["下單憑證"] = {"status": "PASS", "detail": "憑證驗證簽署成功"}
                print("5. [下單憑證] ✅ 憑證啟用成功")
            except Exception as e:
                results["checks"]["下單憑證"] = {"status": "FAIL", "detail": str(e)}
                results["overall_ok"] = False
                results["alerts"].append(f"憑證啟用失敗: {e}")
    else:
        results["checks"]["下單憑證"] = {"status": "PASS", "detail": "模擬模式免憑證"}
        print("5. [下單憑證] ℹ️ 模擬模式免下單憑證")

    # 6. 帳上部位檢查
    if is_live and api and account:
        try:
            positions = api.list_positions(account)
            by_code: Dict[str, int] = {}
            for p in positions or []:
                code = str(getattr(p, "code", ""))
                if code.startswith("TMF"):
                    d = 1 if "Buy" in str(getattr(p, "direction", "")) else -1
                    by_code[code] = by_code.get(code, 0) + int(getattr(p, "quantity", 0)) * d
            non_zero = {k: v for k, v in by_code.items() if v != 0}
            if not non_zero:
                results["checks"]["帳上部位"] = {"status": "PASS", "detail": "0 口 (空手零留倉風險)"}
                print("6. [帳上部位] ✅ 帳上確認 0 口")
            else:
                pos_desc = ", ".join(f"{k}:{v:+d}口" for k, v in sorted(non_zero.items()))
                results["checks"]["帳上部位"] = {"status": "FAIL", "detail": f"帳上有殘留部位 ({pos_desc})"}
                results["overall_ok"] = False
                results["alerts"].append(f"開盤前帳上有未平倉部位: {pos_desc}！")
        except Exception as e:
            results["checks"]["帳上部位"] = {"status": "WARN", "detail": f"查詢失敗: {e}"}
            results["has_warnings"] = True
    else:
        results["checks"]["帳上部位"] = {"status": "PASS", "detail": "模擬模式 (零部位)"}

    # 7. 保證金檢查
    if is_live and api and account:
        try:
            margin = api.margin(account)
            avail = float(getattr(margin, "available_margin", 0.0) or 0.0)
            equity = float(getattr(margin, "equity", 0.0) or 0.0)
            statutory_req = qty * 15000.0
            safe_req = qty * 35000.0
            margin_info = f"可用保證金 NT$ {avail:,.0f} 元 (權益數: NT$ {equity:,.0f} 元)"
            if avail < statutory_req:
                results["checks"]["保證金"] = {"status": "FAIL", "detail": f"{margin_info} < 法定門檻 NT$ {statutory_req:,.0f} 元"}
                results["overall_ok"] = False
                results["alerts"].append(f"保證金不足！可用 NT$ {avail:,.0f} < NT$ {statutory_req:,.0f}")
            elif avail < safe_req:
                results["checks"]["保證金"] = {"status": "WARN", "detail": f"{margin_info} (低於建議水位 NT$ {safe_req:,.0f} 元)"}
                results["has_warnings"] = True
            else:
                results["checks"]["保證金"] = {"status": "PASS", "detail": f"{margin_info} (充足)"}
        except Exception as e:
            results["checks"]["保證金"] = {"status": "WARN", "detail": f"查詢失敗: {e}"}
            results["has_warnings"] = True
    else:
        results["checks"]["保證金"] = {"status": "PASS", "detail": "模擬模式免保證金"}

    if own_session and api:
        try:
            api.logout()
        except Exception:
            pass

    if results["overall_ok"] and not results["has_warnings"]:
        summary_title = "🟢 系統全項健檢通過 (System All Clear)"
    elif results["overall_ok"] and results["has_warnings"]:
        summary_title = "🟡 系統檢查通過 (含維運提醒)"
    else:
        summary_title = "🔴 系統檢測發現異常 (Critical Alerts)"

    report_lines = [
        "🌅 [開盤前系統健康巡檢報告]",
        f"時間: {now_str}",
        f"模式: {'🔴 實盤 (Live)' if is_live else '🟢 模擬 (Paper)'} | 預計口數: {qty}口",
        f"狀態: {summary_title}\n",
        f"• [API連線]: {results['checks'].get('API連線', {}).get('status', 'N/A')}",
        f"• [交易合約]: {results['checks'].get('交易合約', {}).get('detail', 'N/A')}",
        f"• [下單憑證]: {results['checks'].get('下單憑證', {}).get('detail', 'N/A')}",
        f"• [帳上部位]: {results['checks'].get('帳上部位', {}).get('detail', 'N/A')}",
        f"• [保證金]: {results['checks'].get('保證金', {}).get('detail', 'N/A')}",
        f"• [今日參數]: {results['checks'].get('今日參數', {}).get('detail', 'N/A')}",
    ]
    if results["alerts"]:
        report_lines.append("\n⚠️ 【異常警報提醒】:")
        for a in results["alerts"]:
            report_lines.append(f"  🚨 {a}")
    report_lines.append("\n🚀 系統已準備就緒，等待 08:45:00 日盤正式開盤！")
    report_msg = "\n".join(report_lines)
    results["report_message"] = report_msg

    if send_notify:
        notifier.send(report_msg, async_send=False)
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TMF 微台開盤前系統健康巡檢 (V2)")
    parser.add_argument("--mode", choices=["live", "paper"], default="live")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    run_health_check(mode=args.mode, qty=args.qty, send_notify=not args.no_notify)
