# -*- coding: utf-8 -*-
"""
日誌目錄與每日分檔管理 (Log Setup)

目錄結構 (paper / live 完全分開，同一時間只會有一支機器人在跑)：
    logs/
    ├── live/
    │   ├── trader_YYYYMMDD.log   ← Python logger 主日誌，每日自動換檔 (跨日待機不需重啟)
    │   ├── stdout.log            ← nohup 輸出：SDK 底層訊息、子程序輸出、未捕捉例外 (由 start_bg.sh 追加寫入)
    │   ├── state_YYYYMMDD.json   ← 策略斷點狀態
    │   ├── orders_YYYYMMDD.csv   ← 委託成交稽核 (永久保留)
    │   └── command.json          ← 外部即時指令
    └── paper/                    ← 結構相同

保留政策：trader_*.log 與 state_*.json 保留最近 LOG_RETENTION_DAYS 天；orders_*.csv 為稽核紀錄永久保留。

不使用 TimedRotatingFileHandler 的原因：它在午夜需要「重新命名」正在寫入的檔案，
若 Windows 端 (PowerShell Get-Content -Wait / 編輯器) 正開著該檔，rename 會失敗並每筆日誌重試報錯。
改為直接以日期命名新檔，不需任何 rename。
"""
import os
import re
import sys
import logging
import datetime
from typing import Optional

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


def prune_old_files(log_dir: str, retention_days: int = LOG_RETENTION_DAYS,
                    today: Optional[datetime.date] = None) -> list:
    """刪除超過保留天數的 trader_*.log 與 state_*.json (依檔名日期判斷)，回傳已刪除檔名清單"""
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
    """依日誌時間自動切換至 trader_YYYYMMDD.log 的檔案 Handler (換日時順便清理過期檔)"""

    def __init__(self, log_dir: str, prefix: str = "trader",
                 retention_days: int = LOG_RETENTION_DAYS):
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
            # handle() 已持有 lock，這裡直接切換檔案即可
            self._current_date = record_date
            if self.stream:
                try:
                    self.stream.close()
                finally:
                    self.stream = None
            self.baseFilename = os.path.abspath(self._path_for(record_date))
            prune_old_files(self.log_dir, self.retention_days, record_date)
        super().emit(record)  # stream 為 None 時 FileHandler 會自動以 append 開啟新檔


class _StreamToLogger:
    """把 print() 等 Python 層 stdout/stderr 輸出導入 logger，確保盤後總結等訊息也寫進當日 trader 日誌"""

    def __init__(self, logger: logging.Logger, level: int, tee=None):
        self._logger = logger
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
            # 防止 logging 內部錯誤 (handleError 寫 stderr) 造成無限遞迴
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
        # 子程序 (subprocess) 仍可繼承原始 fd，其輸出會進入 nohup 的 stdout.log
        target = self._tee or sys.__stdout__
        return target.fileno()


def setup_mode_logging(mode: str, base_dir: Optional[str] = None,
                       retention_days: int = LOG_RETENTION_DAYS,
                       redirect_std: Optional[bool] = None) -> str:
    """
    設定指定模式的日誌輸出，回傳該模式日誌目錄。
    :param redirect_std: 是否把 print()/stderr 導入 logger；None 時自動判斷 (背景 nohup 執行 = 非 TTY 才導入)
    """
    log_dir = get_mode_log_dir(mode, base_dir)
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    # 避免重複掛載
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
        # 背景執行：移除主控台 Handler，避免同一行同時寫進 trader 日誌與 stdout.log
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                root.removeHandler(h)
        sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
        # stderr (警告、未捕捉例外) 同時保留一份在 stdout.log，程式當掉時也查得到
        sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.WARNING, tee=sys.__stderr__)
    elif not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                 for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    return log_dir
