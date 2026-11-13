# -*- coding: utf-8 -*-
"""
持倉與策略狀態持久化管理器 (State Manager)
職責：
1. 將當日策略執行即時狀態 (持倉方向、進場價、最高浮盈、交易次數、ORB上下界) 安全持久化至磁碟 logs/state_YYYYMMDD.json。
2. 採用原子化寫入機制 (先寫入 .tmp 再 atomic replace)，防止突然斷電或行程中斷造成 JSON 損壞。
3. 程式重啟或開盤初始化時，自動讀取狀態檔，支援策略狀態無縫接管與風控保護。
4. 磁碟 I/O 異常隔離防禦，確保硬碟或檔案系統異常絕不影響實盤交易主迴圈。
"""

import os
import json
import logging
import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger("TMFTrader.StateManager")


class StateManager:
    """
    策略狀態持久化管理器
    """
    def __init__(self, log_dir: Optional[str] = None):
        if log_dir is None:
            # 預設為專案 root 下的 logs 目錄
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.log_dir = os.path.join(base_dir, "logs")
        else:
            self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

    def get_state_path(self, date_str: Optional[str] = None) -> str:
        """
        取得指定交易日的狀態檔路徑 (logs/state_YYYYMMDD.json)
        :param date_str: YYYYMMDD 或 YYYY-MM-DD，預設為今日
        """
        if not date_str:
            d_str = datetime.date.today().strftime("%Y%m%d")
        else:
            d_str = date_str.replace("-", "")
        return os.path.join(self.log_dir, f"state_{d_str}.json")

    def save_state(
        self,
        strategy: Any,
        entries_halted: bool = False,
        custom_fields: Optional[Dict[str, Any]] = None,
        date_str: Optional[str] = None
    ) -> bool:
        """
        將當日策略即時狀態以原子化方式保存至 logs/state_YYYYMMDD.json
        :param strategy: ORBStrategy 實例
        :param entries_halted: 今日是否已停止新倉委託
        :param custom_fields: 額外需要儲存的自訂欄位字典
        :param date_str: 日期字串，預設今日
        :return: 儲存成功與否 (bool)
        """
        tmp_path = None
        try:
            path = self.get_state_path(date_str)
            if date_str:
                clean_d = date_str.replace("-", "")
                today_fmt = f"{clean_d[:4]}-{clean_d[4:6]}-{clean_d[6:]}" if len(clean_d) == 8 else date_str
            else:
                today_fmt = datetime.date.today().strftime("%Y-%m-%d")

            state = {
                "date": today_fmt,
                "updated_at": datetime.datetime.now().strftime("%H:%M:%S"),
                "position": int(getattr(strategy, "position", 0)),
                "entry_price": float(getattr(strategy, "entry_price", 0.0)),
                "entry_time": str(strategy.entry_time) if getattr(strategy, "entry_time", None) is not None else None,
                "highest_profit": float(getattr(strategy, "highest_profit", 0.0)),
                "trade_count": int(getattr(strategy, "trade_count", 0)),
                "orb_high": float(strategy.orb_high) if getattr(strategy, "orb_high", None) is not None else None,
                "orb_low": float(strategy.orb_low) if getattr(strategy, "orb_low", None) is not None else None,
                "orb_calculated": bool(getattr(strategy, "orb_calculated", False)),
                "entries_halted": bool(entries_halted),
            }

            if custom_fields:
                state.update(custom_fields)

            tmp_path = f"{path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return True
        except Exception as e:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            logger.warning(f"保存策略狀態失敗: {e}")
            return False

    def load_state(self, date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        讀取當日策略狀態檔 logs/state_YYYYMMDD.json
        :param date_str: 日期字串，預設今日
        :return: 狀態字典，若檔案不存在或讀取異常則回傳 None
        """
        try:
            path = self.get_state_path(date_str)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"讀取策略狀態失敗: {e}")
        return None

    def restore_strategy_state(self, strategy: Any, saved_state: Optional[Dict[str, Any]] = None) -> bool:
        """
        從狀態字典將欄位注入 ORBStrategy 物件中
        :param strategy: ORBStrategy 實例
        :param saved_state: 狀態字典 (若為 None 則自動嘗試讀取今日狀態)
        :return: 是否成功復原
        """
        state = saved_state if saved_state is not None else self.load_state()
        if not state:
            return False

        try:
            strategy.position = int(state.get("position", 0))
            strategy.entry_price = float(state.get("entry_price", 0.0))
            raw_et = state.get("entry_time")
            if raw_et:
                import pandas as pd
                et_str = str(raw_et).strip()
                if len(et_str) <= 8 and state.get("date"):
                    et_str = f"{state['date']} {et_str}"
                try:
                    strategy.entry_time = pd.Timestamp(et_str)
                except Exception:
                    strategy.entry_time = raw_et
            else:
                strategy.entry_time = None
            strategy.trade_count = int(state.get("trade_count", 0))
            strategy.highest_profit = float(state.get("highest_profit", 0.0))
            strategy._hp_before_bar_val = float(strategy.highest_profit)
            if state.get("orb_calculated", False) and state.get("orb_high") is not None:
                strategy.orb_high = float(state["orb_high"])
                strategy.orb_low = float(state["orb_low"])
                strategy.orb_calculated = True
            return True
        except Exception as e:
            logger.warning(f"復原策略狀態至實例時異常: {e}")
            return False
