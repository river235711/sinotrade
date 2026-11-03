"""
永豐金 Shioaji 官方 API 條款簽署後之「模擬環境登入 + 證券/期貨下單測試」腳本
嚴格依照官方文件 (https://sinotrade.github.io/zh/tutor/prepare/terms/#api) 實作：
1. 檢查版本 >= 1.2
2. simulation=True 模擬模式登入 (login)
3. 證券下單測試 (2890 永豐金，使用當日參考價避免超過漲跌幅 Failed)
4. 間隔 > 1 秒以上 (官方規定：證券/期貨下單測試需間隔1秒以上，以利系統留存測試紀錄)
5. 期貨下單測試 (TXFR1 臺指期近月，使用當日參考價)
6. 切換 simulation=False 正式模式檢查 signed 狀態
"""
import os
import time
import shioaji as sj
import config  # 自動載入 .env

API_KEY = os.environ.get("SHIOAJI_API_KEY", "")
SECRET_KEY = os.environ.get("SHIOAJI_SECRET_KEY", "")

print("=" * 75)
print(f"1️⃣ 查詢 Shioaji 版本: {sj.__version__} (需 >= 1.2)")
print("=" * 75)

print("\n2️⃣ 啟動模擬模式 (simulation=True) 進行登入測試...")
api = sj.Shioaji(simulation=True)
accounts = api.login(api_key=API_KEY, secret_key=SECRET_KEY)
print("📋 模擬模式帳戶列表:", accounts)
time.sleep(2.0)

# 3. 證券下單測試 (2890)
print("\n3️⃣ 執行【證券下單測試】 (2890 永豐金)...")
try:
    stk_contract = api.Contracts.Stocks["2890"]
    stk_price = float(getattr(stk_contract, "reference", 28.0) or 28.0)
    print(f"   使用 2890 當日參考價: {stk_price}")
    stk_order = sj.StockOrder(
        action=sj.Action.Buy,
        price=stk_price,
        quantity=1,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        order_lot=sj.StockOrderLot.Common,
        order_cond=sj.StockOrderCond.Cash,
        account=api.stock_account,
    )
    stk_trade = api.place_order(stk_contract, stk_order)
    print(f"✅ 證券測試單成功送出! 狀態: {stk_trade.status.status}")
    print("   詳細回報:", stk_trade)
except Exception as e:
    print("⚠️ 證券測試單回應:", e)

# 官方規定：證券/期貨下單測試，需間隔 1 秒以上，以利系統留存測試紀錄
print("\n⏳ 依官方規定等待 3 秒，讓系統留存測試紀錄...")
time.sleep(3.0)

# 4. 期貨下單測試 (官方範例 TXFR1)
print("\n4️⃣ 執行【期貨下單測試】 (TXFR1 臺指期近月)...")
try:
    fut_contract = api.Contracts.Futures.TXF.TXFR1
    fut_price = float(getattr(fut_contract, "reference", 44300.0) or 44300.0)
    print(f"   使用 TXFR1 當日參考價: {fut_price:.0f}")
    fut_order = sj.FuturesOrder(
        action=sj.Action.Buy,
        price=int(fut_price),
        quantity=1,
        price_type=sj.FuturesPriceType.LMT,
        order_type=sj.OrderType.ROD,
        octype=sj.FuturesOCType.Auto,
        account=api.futopt_account,
    )
    fut_trade = api.place_order(fut_contract, fut_order)
    print(f"✅ 期貨測試單成功送出! 狀態: {fut_trade.status.status}")
    print("   詳細回報:", fut_trade)
except Exception as e:
    print("⚠️ 期貨測試單回應:", e)

time.sleep(2.0)
api.logout()

# 5. 正式模式登入檢查 signed 狀態
print("\n5️⃣ 切換正式模式 (simulation=False) 檢視 signed 狀態...")
api_prod = sj.Shioaji(simulation=False)
prod_accounts = api_prod.login(api_key=API_KEY, secret_key=SECRET_KEY)
print("📋 正式模式帳戶列表:")
for acc in prod_accounts:
    print("  ->", acc)
api_prod.logout()
print("=" * 75)
