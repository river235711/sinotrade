"""
即時推播通知模組 (支援 Telegram Bot / Discord Webhook / LINE Notify)
只需在 .env 設定對應環境變數即可自動啟用，未設定時靜默略過，絕不影響下單速度與穩定度。

可設定之 .env 變數：
- TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID
- DISCORD_WEBHOOK_URL
- LINE_NOTIFY_TOKEN
"""
import os
import json
import threading
import urllib.request
import urllib.parse
import logging

logger = logging.getLogger("Notifier")


class TradeNotifier:
    def __init__(self):
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.line_token = os.environ.get("LINE_NOTIFY_TOKEN", "").strip()

    @property
    def enabled(self) -> bool:
        return bool((self.tg_token and self.tg_chat_id) or self.discord_webhook or self.line_token)

    def send(self, text: str, async_send: bool = True):
        """發送推播通知 (預設背景執行緒非阻塞發送，0ms 延遲不卡下單迴圈)"""
        if not self.enabled:
            return
        if async_send:
            t = threading.Thread(target=self._send_sync, args=(text,), daemon=True)
            t.start()
        else:
            self._send_sync(text)

    def _send_sync(self, text: str):
        # 1. Telegram Bot
        if self.tg_token and self.tg_chat_id:
            try:
                url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
                payload = json.dumps({
                    "chat_id": self.tg_chat_id,
                    "text": text,
                }).encode("utf-8")
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=5) as _:
                    pass
            except Exception as e:
                logger.debug(f"Telegram 推播失敗: {e}")

        # 2. Discord Webhook
        if self.discord_webhook:
            try:
                payload = json.dumps({"content": text}).encode("utf-8")
                req = urllib.request.Request(
                    self.discord_webhook,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "TMFBot/1.0"},
                )
                with urllib.request.urlopen(req, timeout=5) as _:
                    pass
            except Exception as e:
                logger.debug(f"Discord 推播失敗: {e}")

        # 3. LINE Notify
        if self.line_token:
            try:
                url = "https://notify-api.line.me/api/notify"
                data = urllib.parse.urlencode({"message": f"\n{text}"}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Authorization": f"Bearer {self.line_token}"},
                )
                with urllib.request.urlopen(req, timeout=5) as _:
                    pass
            except Exception as e:
                logger.debug(f"LINE 推播失敗: {e}")
