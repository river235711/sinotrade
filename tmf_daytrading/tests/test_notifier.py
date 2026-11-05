import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from live.notifier import TradeNotifier

n = TradeNotifier()
print("Email enabled:", n.email_enabled)
print("LINE messaging enabled:", n.line_messaging_enabled)
n.send("🧪 [TMF期貨當沖] 雙通道推播測試成功！\n\n• LINE 官方帳號即時通知：已連通 🚀\n• Gmail 備援郵件通知：已連通 📧\n• 後續交易訊號將零秒同步彈窗！", async_send=False)
print("All Done!")
