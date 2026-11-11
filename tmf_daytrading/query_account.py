import os
import config
import shioaji as sj

api_key = os.environ.get("SHIOAJI_API_KEY", "")
secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "")
ca_path = os.environ.get("SHIOAJI_CA_PATH", "")
ca_passwd = os.environ.get("SHIOAJI_CA_PASSWD", "")
person_id = os.environ.get("SHIOAJI_PERSON_ID", "")

api = sj.Shioaji(simulation=False)
api.login(api_key=api_key, secret_key=secret_key)

if ca_path and ca_passwd and person_id and os.path.exists(ca_path):
    try:
        api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd, person_id=person_id)
    except Exception as e:
        print(f"憑證提示: {e}")

acc = api.futopt_account
print("=" * 65)
print("📊 永豐金期貨實體帳戶即時資訊查詢")
print("=" * 65)
print(f"• 交易帳號: {acc.broker_id}-{acc.account_id} ({acc.username})")
print(f"• 身分證字號: {acc.person_id}")
print(f"• API簽署狀態: {'已簽署 (有效)' if acc.signed else '未簽署'}")

# 1. 查詢未平倉部位
print("\n" + "-" * 65)
print("📦 【未平倉部位 (Open Positions)】")
print("-" * 65)
try:
    positions = api.list_positions(acc)
    if not positions:
        print("✅ 目前帳上【無任何部位 (0口 / 空手)】，零隔夜持倉風險。")
    else:
        for p in positions:
            code = getattr(p, "code", "")
            direction = getattr(p, "direction", "")
            qty = getattr(p, "quantity", 0)
            price = getattr(p, "price", 0.0)
            pnl = getattr(p, "pnl", 0.0)
            print(f"• 合約: {code:<10} | 方向: {direction:<4} | 口數: {qty}口 | 成本均價: {price:.1f} | 未實現損益: {pnl:+,.0f}元")
except Exception as e:
    print(f"❌ 查詢部位失敗: {e}")

# 2. 查詢帳戶權益數與保證金
print("\n" + "-" * 65)
print("💰 【帳戶資金餘額與保證金 (Margin & Equity)】")
print("-" * 65)
try:
    m = api.margin(acc)
    equity = getattr(m, "equity", 0.0) or 0.0
    available = getattr(m, "available_margin", 0.0) or 0.0
    initial = getattr(m, "initial_margin", 0.0) or 0.0
    maintenance = getattr(m, "maintenance_margin", 0.0) or 0.0
    unrealized = getattr(m, "unrealized_pnl", 0.0) or 0.0
    realized = getattr(m, "realized_pnl", 0.0) or 0.0

    print(f"• 帳戶權益總值 (Equity)         : NT$ {equity:12,.0f} 元")
    print(f"• 目前可用保證金 (Available)     : NT$ {available:12,.0f} 元")
    print(f"• 已佔用原始保證金 (Initial)      : NT$ {initial:12,.0f} 元")
    print(f"• 維持保證金門檻 (Maintenance)   : NT$ {maintenance:12,.0f} 元")
    print(f"• 當日未實現損益 (Unrealized)    : NT$ {unrealized:12,.0f} 元")
    print(f"• 當日已實現損益 (Realized)      : NT$ {realized:12,.0f} 元")
except Exception as e:
    print(f"❌ 查詢保證金失敗: {e}")

api.logout()
print("=" * 65)
