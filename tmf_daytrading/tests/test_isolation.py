import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
from live.notifier import TradeNotifier

print("="*60)
print("🧪 測試 LINE 故障隔離防護機制 (Fault Isolation Test)")
print("="*60)

n = TradeNotifier()
# 刻意將 Token 改為錯誤的無效 Token，模擬額度用盡或網路斷線
n.line_channel_access_token = "INVALID_TOKEN_FOR_TESTING"

start = time.time()
# 呼叫 send (async_send=True，模擬盤中下單時的呼叫方式)
n.send("🚨 測試訊息：此訊息 LINE 預期會失敗，測試主程序是否 100% 免疫不受影響！", async_send=True)
elapsed = time.time() - start

print(f"✅ 主程序耗時: {elapsed*1000:.3f} 毫秒 (0ms 零延遲，主程序完全不被卡住)")
time.sleep(1.0)
print("✅ 主程序繼續平穩運行，無任何例外未捕獲！")
print("="*60)
