"""
即時推播通知與每日分檔日誌管理模組 (Notifier & Log Setup)
"""
import datetime
import html
import json
import logging
import os
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from config import load_env_file
from gateways.data_repo import get_ssl_context

logger = logging.getLogger("Notifier")
_ssl_ctx = get_ssl_context()

LOG_RETENTION_DAYS = 60
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
VALID_MODES = ("live", "paper")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(_PROJECT_ROOT, "logs"), exist_ok=True)
os.environ.setdefault("SJ_LOG_PATH", os.path.join(_PROJECT_ROOT, "logs", "shioaji.log"))
_PRUNE_PATTERNS = (
    re.compile(r"^trader_(\d{8})\.log$"),
    re.compile(r"^state_(\d{8})\.json$"),
)


def get_mode_log_dir(mode: str, base_dir: Optional[str] = None) -> str:
    """取得指定模式的日誌目錄 (logs/live 或 logs/paper)"""
    if mode not in VALID_MODES:
        raise ValueError(f"mode 必須是 {VALID_MODES} 其中之一，收到: {mode!r}")
    root = base_dir or os.path.join(_PROJECT_ROOT, "logs")
    return os.path.join(root, mode)


def prune_old_files(
    log_dir: str,
    retention_days: int = LOG_RETENTION_DAYS,
    today: Optional[datetime.date] = None,
) -> list:
    """刪除超過保留天數的 trader_*.log 與 state_*.json，回傳已刪除檔名清單"""
    removed = []
    if not os.path.isdir(log_dir):
        return removed
    cutoff = (today or datetime.date.today()) - datetime.timedelta(days=retention_days)
    for name in os.listdir(log_dir):
        for pat in _PRUNE_PATTERNS:
            m = pat.match(name)
            if not m:
                continue
            try:
                file_date = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                break
            if file_date < cutoff:
                try:
                    os.remove(os.path.join(log_dir, name))
                    removed.append(name)
                except OSError:
                    pass
            break
    return removed


class DailyFileHandler(logging.FileHandler):
    """依日誌時間自動切換至 trader_YYYYMMDD.log 的檔案 Handler"""

    def __init__(self, log_dir: str, prefix: str = "trader", retention_days: int = LOG_RETENTION_DAYS):
        self.log_dir = log_dir
        self.prefix = prefix
        self.retention_days = retention_days
        os.makedirs(log_dir, exist_ok=True)
        self._current_date = datetime.date.today()
        super().__init__(self._path_for(self._current_date), mode="a", encoding="utf-8")
        prune_old_files(log_dir, retention_days, self._current_date)

    def _path_for(self, d: datetime.date) -> str:
        return os.path.join(self.log_dir, f"{self.prefix}_{d.strftime('%Y%m%d')}.log")

    def emit(self, record: logging.LogRecord):
        record_date = datetime.date.fromtimestamp(record.created)
        if record_date != self._current_date:
            self._current_date = record_date
            if self.stream:
                try:
                    self.stream.close()
                finally:
                    self.stream = None
            self.baseFilename = os.path.abspath(self._path_for(record_date))
            prune_old_files(self.log_dir, self.retention_days, record_date)
        super().emit(record)


class _StreamToLogger:
    """把 print()/stderr 輸出導入 logger"""

    def __init__(self, target_logger: logging.Logger, level: int, tee=None):
        self._logger = target_logger
        self._level = level
        self._tee = tee
        self._buf = ""
        self._in_write = False

    def write(self, msg):
        if self._tee is not None:
            try:
                self._tee.write(msg)
                self._tee.flush()
            except Exception:
                pass
        if self._in_write:
            if self._tee is None:
                try:
                    sys.__stderr__.write(msg)
                except Exception:
                    pass
            return len(msg)
        self._in_write = True
        try:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        finally:
            self._in_write = False
        return len(msg)

    def flush(self):
        if self._buf.strip():
            self._logger.log(self._level, self._buf.rstrip())
        self._buf = ""
        if self._tee is not None:
            try:
                self._tee.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):
        target = self._tee or sys.__stdout__
        return target.fileno()


def setup_mode_logging(
    mode: str,
    base_dir: Optional[str] = None,
    retention_days: int = LOG_RETENTION_DAYS,
    redirect_std: Optional[bool] = None,
) -> str:
    """設定指定模式的日誌輸出，回傳該模式日誌目錄"""
    log_dir = get_mode_log_dir(mode, base_dir)
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    for h in list(root.handlers):
        if isinstance(h, DailyFileHandler):
            root.removeHandler(h)
            h.close()

    file_handler = DailyFileHandler(log_dir, retention_days=retention_days)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)

    if redirect_std is None:
        try:
            redirect_std = not sys.stdout.isatty()
        except Exception:
            redirect_std = True

    if redirect_std:
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                root.removeHandler(h)
        sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
        sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.WARNING, tee=sys.__stderr__)
    elif not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    return log_dir


class TradeNotifier:
    """多通道推播通知器 (Gmail / Telegram / Discord / LINE Messaging API + 熔斷)"""

    def __init__(self):
        load_env_file(override=False)

        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.line_token = os.environ.get("LINE_NOTIFY_TOKEN", "").strip()

        self.line_channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        self.line_user_id = os.environ.get("LINE_USER_ID", "").strip()
        self._line_cooldown_until = 0.0

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
        """發送推播通知 (背景非阻塞且單一通道異常完全隔離)"""
        if not self.enabled:
            return
        try:
            if async_send:
                t = threading.Thread(target=self._send_sync, args=(text, subject), daemon=True)
                t.start()
            else:
                self._send_sync(text, subject)
        except Exception as e:
            logger.error(f"⚠️ [通知模組背景調度異常] {e} (已安全略過，交易正常進行)")

    def _send_sync(self, text: str, subject: str = None):
        try:
            if self.email_enabled:
                self._send_email_sync(text, subject)
        except Exception as e:
            logger.error(f"Gmail 發送異常 (已隔離): {e}")

        if self.tg_token and self.tg_chat_id:
            try:
                url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
                payload = json.dumps({"chat_id": self.tg_chat_id, "text": text}).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=4, context=_ssl_ctx) as _:
                    pass
            except Exception as e:
                logger.debug(f"Telegram 推播失敗: {e}")

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

        if self.line_messaging_enabled and time.time() >= self._line_cooldown_until:
            try:
                url = "https://api.line.me/v2/bot/message/push"
                payload = json.dumps({
                    "to": self.line_user_id,
                    "messages": [{"type": "text", "text": text}],
                }).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.line_channel_access_token}",
                    },
                )
                with urllib.request.urlopen(req, timeout=4, context=_ssl_ctx) as _:
                    pass
                logger.info("📱 LINE 推播通知已成功送達")
            except urllib.error.HTTPError as http_err:
                if http_err.code in (429, 403):
                    self._line_cooldown_until = time.time() + 3600.0
                    logger.warning(f"⚠️ [LINE 推播額度已滿或被限制 (HTTP {http_err.code})] 已啟動 1 小時熔斷。")
                elif http_err.code == 401:
                    self._line_cooldown_until = time.time() + 7200.0
                    logger.warning("⚠️ [LINE Token 認證失效 (HTTP 401)] 請檢查 LINE_CHANNEL_ACCESS_TOKEN。")
                else:
                    logger.error(f"❌ LINE 推播 HTTP 異常 (代碼 {http_err.code})")
            except Exception as e:
                logger.error(f"❌ LINE 推播發送異常 (已安全隔離): {e}")

        if self.line_token:
            try:
                url = "https://notify-api.line.me/api/notify"
                data = urllib.parse.urlencode({"message": f"\n{text}"}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data, headers={"Authorization": f"Bearer {self.line_token}"}
                )
                with urllib.request.urlopen(req, timeout=5, context=_ssl_ctx) as _:
                    pass
            except Exception as e:
                logger.debug(f"LINE Notify 推播失敗: {e}")

    def _send_email_sync(self, text: str, subject: str = None):
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
