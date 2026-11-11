#!/usr/bin/env python3
"""
health_check.py - 開盤前自動健康巡檢模組 (Pre-Market Health Check)
支援獨立執行 (CLI / 批次檔 / 工作排程器) 與整合進主程式 (08:35 定時巡檢)
"""
import os
import sys
import datetime
import logging
from typing import Dict, Any

# 加入專案根目錄
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import InstrumentConfig, ORBStrategyConfig
from strategies.atr_scaler import config_for_date, describe
from live.notifier import TradeNotifier

logger = logging.getLogger("HealthCheck")

def run_health_check(mode: str = "live", qty: int = 1, send_notify: bool = True) -> Dict[str, Any]:
    """
    執行開盤前全系統健康巡檢
    :param mode: 'live' 實盤 或 'paper' 模擬
    :param qty: 預計下單口數
    :param send_notify: 是否發送 LINE 與 Gmail 推播
    :return: 包含巡檢狀態、細節與報告文字的字典
    """
    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    is_live = (mode.lower() == "live")

    notifier = TradeNotifier()
    data_dir = os.path.join(BASE_DIR, "data")
    base_cfg = ORBStrategyConfig()
    strat_cfg, atr_info = config_for_date(base_cfg, data_dir, today_str)

    results = {
        "timestamp": now_str,
        "mode": "實盤 (Live)" if is_live else "模擬 (Paper)",
        "qty": qty,
        "checks": {},
        "overall_ok": True,
        "has_warnings": False,
        "alerts": []
    }

    print("=" * 70)
    print(f"🏥 TMF 微型臺指期當沖系統 - 開盤前系統健康巡檢 ({now_str})")
    print(f"• 交易模式: {'🔴 實盤 (Live)' if is_live else '🟢 模擬 (Paper)'} | 預計口數: {qty} 口")
    print("=" * 70)

    # -------------------------------------------------------------
    # 檢查項目 1: 通知推播通道 (Notification Channels)
    # -------------------------------------------------------------
    line_ok = notifier.line_messaging_enabled
    email_ok = notifier.email_enabled
    if line_ok or email_ok:
        status_str = f"正常 (LINE: {'✅' if line_ok else '❌'}, Gmail: {'✅' if email_ok else '❌'})"
        results["checks"]["通知推播"] = {"status": "PASS", "detail": status_str}
        print(f"1. [通知推播] ✅ {status_str}")
    else:
        results["checks"]["通知推播"] = {"status": "WARN", "detail": "未設定 LINE 或 Gmail"}
        results["has_warnings"] = True
        print(f"1. [通知推播] ⚠️ 未設定通知通道，將無法接收即時推播")

    # -------------------------------------------------------------
    # 檢查項目 2: 歷史數據與今日 ATR 參數 (Data & Parameters)
    # -------------------------------------------------------------
    try:
        param_desc = describe(strat_cfg, atr_info)
        results["checks"]["今日參數"] = {"status": "PASS", "detail": param_desc}
        print(f"2. [今日參數] ✅ 載入成功：\n   {param_desc}")
    except Exception as e:
        results["checks"]["今日參數"] = {"status": "FAIL", "detail": str(e)}
        results["overall_ok"] = False
        results["alerts"].append(f"今日參數載入失敗: {e}")
        print(f"2. [今日參數] ❌ 載入失敗: {e}")

    # -------------------------------------------------------------
    # 檢查項目 3: 永豐金 Shioaji API 連線與帳號登入 (API Login)
    # -------------------------------------------------------------
    api = None
    account = None
    try:
        import shioaji as sj
        api = sj.Shioaji(simulation=not is_live)
        api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
        secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()

        if not api_key or not secret_key:
            raise ValueError(".env 未設定 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY")

        api.login(api_key=api_key, secret_key=secret_key)
        account = api.futopt_account
        if not account:
            raise ValueError("登入成功但查無期貨交易帳號 (futopt_account)")

        acc_desc = f"{account.broker_id}-{account.account_id} ({account.username})"
        results["checks"]["API連線"] = {"status": "PASS", "detail": f"登入成功 ({acc_desc})"}
        print(f"3. [API連線]  ✅ 登入成功 ({acc_desc})")
    except Exception as e:
        results["checks"]["API連線"] = {"status": "FAIL", "detail": str(e)}
        results["overall_ok"] = False
        results["alerts"].append(f"Shioaji API 登入失敗: {e}")
        print(f"3. [API連線]  ❌ 登入失敗: {e}")

    # -------------------------------------------------------------
    # 檢查項目 4: 當月主力合約解析 (Contract Binding)
    # -------------------------------------------------------------
    contract_bound = None
    if api and results["overall_ok"]:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                futs = getattr(api.Contracts.Futures, "TMF", [])
            for c in futs:
                if getattr(c, "code", "") == "TMFR1":
                    contract_bound = c
                    break

            if contract_bound:
                target_code = getattr(contract_bound, "target_code", None)
                if target_code:
                    for c in futs:
                        if getattr(c, "code", "") == target_code:
                            contract_bound = c
                            break

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

    # -------------------------------------------------------------
    # 檢查項目 5: 下單憑證 (CA Certificate - 實盤專用)
    # -------------------------------------------------------------
    if is_live and api:
        ca_path = os.environ.get("SHIOAJI_CA_PATH", "").strip()
        ca_passwd = os.environ.get("SHIOAJI_CA_PASSWD", "").strip()
        person_id = os.environ.get("SHIOAJI_PERSON_ID", "").strip()

        if not ca_path or not os.path.exists(ca_path):
            results["checks"]["下單憑證"] = {"status": "FAIL", "detail": f"憑證檔不存在: {ca_path}"}
            results["overall_ok"] = False
            results["alerts"].append(f"憑證檔不存在: {ca_path}")
            print(f"5. [下單憑證] ❌ 憑證檔不存在: {ca_path}")
        else:
            try:
                api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd, person_id=person_id)
                results["checks"]["下單憑證"] = {"status": "PASS", "detail": "憑證驗證簽署成功"}
                print(f"5. [下單憑證] ✅ 憑證啟用成功 (有效且已簽署)")
            except Exception as e:
                results["checks"]["下單憑證"] = {"status": "FAIL", "detail": str(e)}
                results["overall_ok"] = False
                results["alerts"].append(f"憑證啟用失敗: {e}")
                print(f"5. [下單憑證] ❌ 憑證啟用失敗: {e}")
    else:
        results["checks"]["下單憑證"] = {"status": "PASS", "detail": "模擬模式免憑證"}
        print(f"5. [下單憑證] ℹ️ 模擬模式免下單憑證")

    # -------------------------------------------------------------
    # 檢查項目 6: 帳上部位檢查 (Pre-Market Inventory Audit)
    # -------------------------------------------------------------
    if is_live and api and account:
        try:
            positions = api.list_positions(account)
            tmf_pos = 0
            if positions:
                for p in positions:
                    if str(getattr(p, "code", "")).startswith("TMF"):
                        d = 1 if "Buy" in str(getattr(p, "direction", "")) else -1
                        tmf_pos += int(getattr(p, "quantity", 0)) * d

            if tmf_pos == 0:
                results["checks"]["帳上部位"] = {"status": "PASS", "detail": "0 口 (空手零留倉風險)"}
                print(f"6. [帳上部位] ✅ 帳上確認 0 口 (完全空手，無隔夜留倉)")
            else:
                results["checks"]["帳上部位"] = {"status": "FAIL", "detail": f"帳上有殘留部位: {tmf_pos:+d}口"}
                results["overall_ok"] = False
                results["alerts"].append(f"開盤前帳上有未平倉部位: {tmf_pos:+d} 口！")
                print(f"6. [帳上部位] 🚨 異常！帳上有殘留部位: {tmf_pos:+d} 口！請立即檢查券商軟體！")
        except Exception as e:
            results["checks"]["帳上部位"] = {"status": "WARN", "detail": f"查詢失敗: {e}"}
            results["has_warnings"] = True
            print(f"6. [帳上部位] ⚠️ 查詢失敗: {e}")
    else:
        results["checks"]["帳上部位"] = {"status": "PASS", "detail": "模擬模式 (零部位)"}
        print(f"6. [帳上部位] ℹ️ 模擬模式無實體部位")

    # -------------------------------------------------------------
    # 檢查項目 7: 保證金充足度 (Margin Adequacy - 實盤專用)
    # -------------------------------------------------------------
    if is_live and api and account:
        try:
            margin = api.margin(account)
            avail = float(getattr(margin, "available_margin", 0.0) or 0.0)
            equity = float(getattr(margin, "equity", 0.0) or 0.0)
            statutory_req = qty * 15000.0   # 法定原始保證金門檻
            safe_req = qty * 35000.0        # 量化安全保證金門檻

            margin_info = f"可用保證金 NT$ {avail:,.0f} 元 (權益數: NT$ {equity:,.0f} 元)"
            if avail < statutory_req:
                results["checks"]["保證金"] = {
                    "status": "FAIL",
                    "detail": f"{margin_info} < 法定門檻 NT$ {statutory_req:,.0f} 元"
                }
                results["overall_ok"] = False
                results["alerts"].append(f"保證金不足！可用 NT$ {avail:,.0f} < 法定門檻 NT$ {statutory_req:,.0f}")
                print(f"7. [保證金]   🚨 嚴重不足！{margin_info}，低於法定門檻 NT$ {statutory_req:,.0f} 元！無法下單！")
            elif avail < safe_req:
                results["checks"]["保證金"] = {
                    "status": "WARN",
                    "detail": f"{margin_info} (低於建議安全水位 NT$ {safe_req:,.0f} 元)"
                }
                results["has_warnings"] = True
                print(f"7. [保證金]   ⚠️ 偏低提醒：{margin_info}，建議補足至安全水位 NT$ {safe_req:,.0f} 元。")
            else:
                results["checks"]["保證金"] = {"status": "PASS", "detail": f"{margin_info} (充足)"}
                print(f"7. [保證金]   ✅ 充足：{margin_info} (高於安全水位 NT$ {safe_req:,.0f} 元)")
        except Exception as e:
            results["checks"]["保證金"] = {"status": "WARN", "detail": f"查詢失敗: {e}"}
            results["has_warnings"] = True
            print(f"7. [保證金]   ⚠️ 查詢失敗: {e}")
    else:
        results["checks"]["保證金"] = {"status": "PASS", "detail": "模擬模式免保證金"}
        print(f"7. [保證金]   ℹ️ 模擬模式免真實保證金")

    # 登出 API
    if api:
        try:
            api.logout()
        except Exception:
            pass

    # -------------------------------------------------------------
    # 產出總結報告與發送通知
    # -------------------------------------------------------------
    print("=" * 70)
    if results["overall_ok"] and not results["has_warnings"]:
        summary_title = "🟢 系統全項健檢通過 (System All Clear)"
        badge = "✅ PASS"
    elif results["overall_ok"] and results["has_warnings"]:
        summary_title = "🟡 系統檢查通過 (含維運提醒)"
        badge = "⚠️ WARNING"
    else:
        summary_title = "🔴 系統檢測發現異常 (Critical Alerts)"
        badge = "🚨 FAIL"

    print(f"總結結果: {badge} - {summary_title}")
    print("=" * 70)

    # 組合推播文字
    report_lines = [
        f"🌅 [開盤前系統健康巡檢報告]",
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
        print("📨 巡檢報告已成功推播至 LINE 與 Gmail！")

    return results

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TMF 微台開盤前系統健康巡檢")
    parser.add_argument("--mode", choices=["live", "paper"], default="live", help="交易模式 (預設 live)")
    parser.add_argument("--qty", type=int, default=1, help="預計下單口數 (預設 1)")
    parser.add_argument("--no-notify", action="store_true", help="不發送推播通知")
    args = parser.parse_args()

    run_health_check(mode=args.mode, qty=args.qty, send_notify=not args.no_notify)
