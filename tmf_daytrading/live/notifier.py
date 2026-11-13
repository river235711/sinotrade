"""
即時推播通知模組 (支援 Gmail / Telegram Bot / Discord Webhook / LINE Notify)
只需在 .env 設定對應環境變數即可自動啟用，未設定時靜默略過，絕不影響下單速度與穩定度。

可設定之 .env 變數：
- GMAIL_USER (寄件人 Gmail，預設同 NOTIFY_EMAIL)
- GMAIL_APP_PASSWORD (Google 應用程式 16 碼專用密碼)
- NOTIFY_EMAIL (接收通知之 Email，如 river235711@gmail.com)
- TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID
- DISCORD_WEBHOOK_URL
- LINE_NOTIFY_TOKEN
"""
import os
import time
import html
import json
import ssl
import smtplib
import datetime
import threading
import urllib.request
import urllib.parse
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import load_env_file

logger = logging.getLogger("Notifier")

# macOS / Linux 相容 SSL 上下文 (解決未安裝 root CA 憑證時的握手失敗問題)
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    try:
        _ssl_ctx = ssl.create_default_context()
    except Exception:
        _ssl_ctx = ssl._create_unverified_context()


class TradeNotifier:
    def __init__(self):
        # 共用設定檔載入器，不覆蓋已透過環境注入的測試/執行期變數
        load_env_file(override=False)

        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.line_token = os.environ.get("LINE_NOTIFY_TOKEN", "").strip()

        # LINE Messaging API (LINE 官方帳號)
        self.line_channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        self.line_user_id = os.environ.get("LINE_USER_ID", "").strip()
        self._line_cooldown_until = 0.0  # 額度用盡或認證失敗時的冷卻時間戳 (秒)

        # Gmail 設定
        self.notify_email = os.environ.get("NOTIFY_EMAIL", "").strip() or "river235711@gmail.com"
        self.gmail_user = os.environ.get("GMAIL_USER", "").strip() or self.notify_email
        self.gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    @property
    def email_enabled(self) -> bool:
        return bool(self.gmail_user and self.gmail_app_password and self.notify_email)

    @property
    def line_messaging_enabled(self) -> bool:
        return bool(self.line_channel_access_token and self.line_user_id)

    @property
    def enabled(self) -> bool:
        return bool(
            (self.tg_token and self.tg_chat_id)
            or self.discord_webhook
            or self.line_token
            or self.line_messaging_enabled
            or self.email_enabled
        )

    def send(self, text: str, subject: str = None, async_send: bool = True):
        """
        發送推播通知 (預設背景守護執行緒非阻塞發送，0ms 延遲不卡下單迴圈)
        具備全隔離防護：即使外部推播服務掛掉或超額，也絕不影響交易主程序
        """
        if not self.enabled:
            return
        try:
            if async_send:
                t = threading.Thread(target=self._send_sync, args=(text, subject), daemon=True)
                t.start()
            else:
                self._send_sync(text, subject)
        except Exception as e:
            # 極端防護：即使建立執行緒失敗，也絕不向上拋出例外中斷交易主迴圈
            logger.error(f"⚠️ [通知模組背景調度異常] {e} (已安全略過，交易正常進行)")

    def _send_sync(self, text: str, subject: str = None):
        """同步發送通知 (各通知通道完全獨立隔離，單一通道失敗絕不波及其他通道)"""
        try:
            # 1. Gmail Email 發送
            if self.email_enabled:
                self._send_email_sync(text, subject)
        except Exception as e:
            logger.error(f"Gmail 發送異常 (已隔離): {e}")

        # 2. Telegram Bot
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
                with urllib.request.urlopen(req, timeout=4, context=_ssl_ctx) as _:
                    pass
            except Exception as e:
                logger.debug(f"Telegram 推播失敗: {e}")

        # 3. Discord Webhook
        if self.discord_webhook:
            try:
                payload = json.dumps({"content": text}).encode("utf-8")
                req = urllib.request.Request(
                    self.discord_webhook,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "TMFBot/1.0"},
                )
                with urllib.request.urlopen(req, timeout=4, context=_ssl_ctx) as _:
                    pass
            except Exception as e:
                logger.debug(f"Discord 推播失敗: {e}")

        # 4. LINE Messaging API (LINE 官方帳號 Push Message，具備熔斷降級機制)
        if self.line_messaging_enabled and time.time() >= self._line_cooldown_until:
            try:
                url = "https://api.line.me/v2/bot/message/push"
                payload = json.dumps({
                    "to": self.line_user_id,
                    "messages": [{"type": "text", "text": text}]
                }).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.line_channel_access_token}"
                    }
                )
                with urllib.request.urlopen(req, timeout=4, context=_ssl_ctx) as _:
                    pass
                logger.info("📱 LINE 推播通知已成功送達")
            except urllib.error.HTTPError as http_err:
                err_body = ""
                try:
                    err_body = http_err.read().decode("utf-8")
                except Exception:
                    pass
                # 429 額度用盡或 401 憑證失效：啟動 1 小時自動熔斷冷卻，不頻繁撞牆
                if http_err.code in (429, 403):
                    self._line_cooldown_until = time.time() + 3600.0
                    logger.warning(f"⚠️ [LINE 推播額度已滿或被限制 (HTTP {http_err.code})] 已啟動自動熔斷保護 (冷卻 1 小時)，期間由 Gmail 正常接管通知！")
                elif http_err.code == 401:
                    self._line_cooldown_until = time.time() + 7200.0
                    logger.warning(f"⚠️ [LINE Token 認證失效 (HTTP 401)] 請檢查 LINE_CHANNEL_ACCESS_TOKEN。由 Gmail 正常接管通知。")
                else:
                    logger.error(f"❌ LINE 推播 HTTP 異常 (代碼 {http_err.code}): {err_body}")
            except Exception as e:
                logger.error(f"❌ LINE 推播發送異常 (已安全隔離): {e}")

        # 5. LINE Notify (舊版備援)
        if self.line_token:
            try:
                url = "https://notify-api.line.me/api/notify"
                data = urllib.parse.urlencode({"message": f"\n{text}"}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Authorization": f"Bearer {self.line_token}"},
                )
                with urllib.request.urlopen(req, timeout=5, context=_ssl_ctx) as _:
                    pass
            except Exception as e:
                logger.debug(f"LINE 推播失敗: {e}")

    def _send_email_sync(self, text: str, subject: str = None):
        """透過 Gmail SMTP 安全連線發送郵件 (STARTTLS on port 587)"""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        first_line = text.strip().split("\n")[0] if text else "即時通知"
        mail_subj = subject or f"[TMF期貨當沖] {first_line}"

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = mail_subj
            msg["From"] = f"TMF Trading Bot <{self.gmail_user}>"
            msg["To"] = self.notify_email

            part_plain = MIMEText(text, "plain", "utf-8")
            html_content = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #202124; max-width: 620px; border: 1px solid #dadce0; border-radius: 8px; padding: 24px; margin: 10px auto; background-color: #ffffff;">
                <div style="display: flex; align-items: center; border-bottom: 2px solid #1a73e8; padding-bottom: 12px; margin-bottom: 16px;">
                    <h2 style="color: #1a73e8; margin: 0; font-size: 20px;">🤖 TMF 微型臺指期當沖系統</h2>
                </div>
                <div style="background-color: #f8f9fa; border-left: 4px solid #1a73e8; padding: 16px; border-radius: 4px; margin-bottom: 20px;">
                    <pre style="margin: 0; font-size: 14px; white-space: pre-wrap; word-break: break-all; font-family: Consolas, Monaco, monospace; color: #1f1f1f;">{html.escape(text)}</pre>
                </div>
                <div style="font-size: 12px; color: #5f6368; border-top: 1px solid #eeeeee; padding-top: 12px; display: flex; justify-content: space-between;">
                    <span>⏱️ 發送時間: {now_str}</span>
                    <span>📈 標的: TMF 微台指當沖</span>
                </div>
            </div>
            """
            part_html = MIMEText(html_content, "html", "utf-8")
            msg.attach(part_plain)
            msg.attach(part_html)

            with smtplib.SMTP("smtp.gmail.com", 587, timeout=8) as server:
                server.starttls()
                server.login(self.gmail_user, self.gmail_app_password)
                server.sendmail(self.gmail_user, [self.notify_email], msg.as_string())
            logger.info(f"📧 Gmail 通知已成功發送至 {self.notify_email}: {mail_subj}")
        except Exception as e:
            logger.error(f"❌ Gmail 通知發送異常: {e}")
