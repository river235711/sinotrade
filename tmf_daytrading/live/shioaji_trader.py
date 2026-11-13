"""
永豐金證券 Shioaji 實盤 / 模擬交易執行器 (TMF 微型臺指期當沖專用)
支援：
1. --mode paper  (模擬測試環境 simulation=True，不下真單，驗證即時報價與訊號)
2. --mode live   (正式實盤環境 simulation=False，掛載憑證自動送單)

成交確認與對帳機制：
- 每張單送出後都會輪詢委託狀態，確認真的 Filled，並取得實際成交價與口數。
- 送單後再用 list_positions 查帳上真實部位，與策略預期部位對帳。
- 沒成交、部分成交、卡單：自動撤單、重新對齊策略狀態，必要時強制平倉並停止開新倉。
- 所有委託與成交結果寫入 logs/<live|paper>/orders_YYYYMMDD.csv 供事後核對。
"""
import os
import csv
import json
import time
import signal
import warnings
import datetime
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import pandas as pd
from config import InstrumentConfig, ORBStrategyConfig, ShioajiConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date, describe
from live.notifier import TradeNotifier
from live.bar_aggregator import BarAggregator
from live.state_manager import StateManager
from live.log_setup import get_mode_log_dir

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ShioajiTrader")

FINAL_STATUSES = {"Filled", "Cancelled", "Failed", "Inactive"}
PENDING_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}


def _status_name(s) -> str:
    """把 Shioaji 的列舉值統一轉成純文字，如 Status.Filled -> 'Filled'"""
    v = getattr(s, "value", s)
    return str(v).split(".")[-1]


@dataclass
class FillResult:
    ok: bool                 # 是否「全部成交」
    filled_qty: int          # 實際成交口數
    avg_price: float         # 實際成交均價 (未成交為 0)
    status: str              # 最終委託狀態


class ShioajiTMFTrader:
    def __init__(self, simulation: bool = True, api_key: str = "", secret_key: str = "", quantity: int = 1):
        sj_cfg = ShioajiConfig()
        self.simulation = simulation
        self.api_key = api_key or sj_cfg.api_key
        self.secret_key = secret_key or sj_cfg.secret_key
        self.ca_path = sj_cfg.ca_path
        self.ca_passwd = sj_cfg.ca_passwd
        self.person_id = sj_cfg.person_id
        self.quantity = quantity
        self.api = None
        self.contract = None
        self._contract_code = "TMFR1"
        self._is_session_active = False
        self.notifier = TradeNotifier()
        self._last_reconnect_time = 0.0

        # 成交確認參數
        self.fill_timeout = 10.0                # 等待成交的秒數
        self.fill_poll_interval = 0.15          # 輪詢委託狀態的間隔秒數 (150ms 極速確認 IOC 成交)
        self.retry_pause = 1.0                  # 平倉重試之間的等待秒數
        self.reconcile_interval = 30.0          # 平時對帳間隔秒數
        # paper / live 日誌、狀態檔、稽核表與外部指令檔完全分開 (logs/paper 或 logs/live)
        self.log_dir = get_mode_log_dir("paper" if simulation else "live")

        # 核心解耦模組：K 棒聚合器與狀態持久化管理器
        self.bar_aggregator = BarAggregator(kbars_disabled=bool(simulation))
        self.state_manager = StateManager(log_dir=self.log_dir)

        # 事件驅動喚醒與安全退出控制
        self._wake_event = threading.Event()
        self._stop_requested = False
        self._active_strategy: Optional[ORBStrategy] = None
        self._last_wake_price: float = 0.0
        self._eod_close_notified: bool = False
        self._eod_done_date: Optional[str] = None
        self._holiday_detected: bool = False

        # 風控狀態
        self.is_kill_switch_triggered = False   # True 時拒絕所有委託 (含平倉)，僅供緊急手動使用
        self.entries_halted = False             # True 時只允許平倉，不再開新倉
        self.pending_flatten = False            # True 時代表已觸發平倉但帳上尚未歸零，每秒持續以 MKT+IOC 追殺直到 0 口
        self.order_failures = 0                 # 開倉單連續未成交次數
        self.max_order_failures = 3

    def request_stop(self, signum=None, frame=None):
        """接收 SIGINT / SIGTERM 信號或外部呼叫，0 毫秒立即喚醒主迴圈並安全停止"""
        self._stop_requested = True
        self._wake_event.set()

    def close(self):
        """安全登出並釋放 Shioaji 連線資源"""
        if self.api is not None:
            try:
                self.api.logout()
            except Exception:
                pass
            finally:
                self.api = None
                self.contract = None
                self._is_session_active = False

    # ------------------------------------------------------------------
    # 向後相容屬性映射 (保持對既有私有屬性之存取相容)
    # ------------------------------------------------------------------
    @property
    def _realtime_bars(self):
        return self.bar_aggregator._bars

    @_realtime_bars.setter
    def _realtime_bars(self, val):
        self.bar_aggregator._bars = val

    @property
    def _history_loaded(self) -> bool:
        return self.bar_aggregator.history_loaded

    @_history_loaded.setter
    def _history_loaded(self, val: bool):
        self.bar_aggregator.history_loaded = val

    @property
    def _kbars_disabled(self) -> bool:
        return self.bar_aggregator.kbars_disabled

    @_kbars_disabled.setter
    def _kbars_disabled(self, val: bool):
        self.bar_aggregator.kbars_disabled = val

    @property
    def _last_tick_time(self) -> float:
        return self.bar_aggregator.last_tick_time

    @_last_tick_time.setter
    def _last_tick_time(self, val: float):
        self.bar_aggregator.last_tick_time = val

    @property
    def _last_snap_time(self) -> float:
        return self.bar_aggregator.last_snap_time

    @_last_snap_time.setter
    def _last_snap_time(self, val: float):
        self.bar_aggregator.last_snap_time = val

    # ------------------------------------------------------------------
    # 連線與合約
    # ------------------------------------------------------------------
    def initialize(self) -> bool:
        """初始化 Shioaji API、登入並啟用憑證"""
        try:
            import shioaji as sj
            mode_str = "模擬環境 (Simulation)" if self.simulation else "正式實盤環境 (Live)"
            logger.info(f"正在連線至永豐金 Shioaji [{mode_str}]...")
            self.api = sj.Shioaji(simulation=self.simulation)

            if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
                logger.error("尚未設定有效的 SHIOAJI_API_KEY！請先編輯專案目錄下的 .env 檔案。")
                return False

            accounts = self.api.login(
                api_key=self.api_key,
                secret_key=self.secret_key
            )
            logger.info(f"Shioaji 登入成功！帳號列表: {accounts}")

            # 確保底層 Solace session 完整建立 (等待 2 秒)
            time.sleep(2.0)

            # 若為正式實盤環境，必須啟用下單憑證 (.pfx)
            if not self.simulation:
                if self.ca_path and self.ca_passwd:
                    logger.info(f"正在載入實盤下單憑證: {self.ca_path} ...")
                    self.api.activate_ca(
                        ca_path=self.ca_path,
                        ca_passwd=self.ca_passwd,
                        person_id=self.person_id
                    )
                    logger.info("實盤下單憑證啟用成功！")
                else:
                    logger.warning("注意：實盤模式尚未設定 SHIOAJI_CA_PATH / SHIOAJI_CA_PASSWD，將僅能看盤無法送出實單。")

            self._is_session_active = True
            return True
        except Exception as e:
            self._is_session_active = False
            logger.error(f"Shioaji 初始化失敗: {e}")
            return False

    def load_tmf_contract(self, code: str = "TMFR1"):
        """獲取 TMF 微型臺指期近月合約"""
        self._contract_code = code or "TMFR1"
        if self.api is None:
            logger.error("API 尚未登入")
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tmf_contracts = list(self.api.Contracts.Futures.TMF)

            # 優先搜尋 code 相符的合約 (如 TMFR1 或 TMFK6)
            for c in tmf_contracts:
                if c.code == code or (hasattr(c, "name") and code in c.name):
                    self.contract = c
                    break
            
            if not self.contract:
                # 預設綁定近月連續合約 TMFR1
                for c in tmf_contracts:
                    if c.code == "TMFR1":
                        self.contract = c
                        break

            # 若選到連續合約 (如 TMFR1，其 target_code 為實際交易月份 TMFK6)，自動解析為實際可下單之月份合約
            target_code = getattr(self.contract, "target_code", None)
            if target_code:
                for c in tmf_contracts:
                    if c.code == target_code:
                        logger.info(f"自動將連續合約 {self.contract.code} 解析為當月主力可下單合約: {c.code} ({c.name})")
                        self.contract = c
                        break

            logger.info(f"已綁定交易合約: {self.contract.code} - {self.contract.name}")

            # 訂閱即時逐筆 Tick 報價，委由 BarAggregator 零延遲即時合成 1分K
            try:
                import shioaji as sj
                @self.api.on_tick_fop_v1()
                def _on_tick(*args):
                    try:
                        tick = args[-1]
                        price = float(tick.close)
                        vol = int(tick.volume)
                        if price <= 0:
                            return
                        dt = datetime.datetime.combine(tick.date, tick.time)
                        self.bar_aggregator.update_tick(price, vol, dt)
                        strat = self._active_strategy
                        if strat is not None and strat.position != 0 and price != self._last_wake_price:
                            self._last_wake_price = price
                            self._wake_event.set()
                    except Exception:
                        pass

                self.api.subscribe(self.contract, quote_type=sj.QuoteType.Tick)
                logger.info(f"📡 已啟用即時 Tick 串流訂閱 ({self.contract.code})，自動合成即時 1分K")
            except Exception as sub_err:
                logger.warning(f"訂閱即時 Tick 略過: {sub_err}")

            return self.contract
        except Exception as e:
            logger.error(f"獲取合約失敗: {e}")
            return None

    def reconnect(self) -> bool:
        """盤中斷線自動重連機制 (含 15 秒冷卻保護，非交易時段且零部位時不重連)"""
        now = datetime.datetime.now()
        now_str = now.strftime("%H:%M:%S")
        is_trading_window = (now.weekday() < 5) and ("08:30:00" <= now_str <= "13:45:00") and not getattr(self, "_holiday_detected", False)
        strat = getattr(self, "_active_strategy", None)
        has_pos = (strat is not None and strat.position != 0) or getattr(self, "pending_flatten", False)

        # 非開盤交易時段且無持倉時，拒絕夜間不必要的重登報警
        if not is_trading_window and not has_pos:
            logger.debug(f"目前為非交易時段 ({now_str}) 且無部位，略過自動重連。")
            return False

        if time.time() - self._last_reconnect_time < 15.0:
            return False
        self._last_reconnect_time = time.time()
        logger.warning("🔄 [自動重連] 偵測到 Shioaji 連線中斷，正在重新登入並恢復即時行情訂閱...")
        try:
            if self.api is not None:
                try:
                    self.api.logout()
                except Exception:
                    pass
            if self.initialize() and self.load_tmf_contract(self._contract_code):
                msg = f"✅ [自動重連成功] Shioaji 已恢復連線與 Tick 訂閱 ({self._contract_code})"
                logger.info(msg)
                self.notifier.send(msg)
                return True
        except Exception as e:
            logger.error(f"自動重連失敗: {e}")
        return False

    def logout_and_sleep(self, reason: str = "盤後休眠"):
        """盤後或非交易時段主動斷開連線，進入離線深度休眠模式 (防夜間中斷與不必要網路存取)"""
        if self.api is not None:
            try:
                logger.info(f"🌙 [{reason}] 正在安全登出永豐金 Shioaji API，進入離線待命模式...")
                self.api.logout()
            except Exception as e:
                logger.debug(f"登出異常 (可安全忽略): {e}")
            finally:
                self.api = None
                self.contract = None
                self._is_session_active = False
                logger.info("💤 永豐 API 連線已安全斷開，進入深度省電防打擾睡眠 (零網路存取、不受夜間維護中斷干擾)。")

    def wake_and_login(self, reason: str = "開盤前喚醒") -> bool:
        """開盤前自動重新登入永豐金 Shioaji、啟用憑證並重新訂閱當日主力合約"""
        logger.info(f"🌅 [{reason}] 正在連線登入永豐金 Shioaji 並掛載憑證與主力合約...")
        ok = self.initialize() and (self.load_tmf_contract(self._contract_code) is not None)
        if ok:
            self._is_session_active = True
            logger.info(f"✅ [{reason}] 永豐金 Shioaji 登入與即時行情串流已就緒！")
        else:
            logger.error(f"❌ [{reason}] 永豐金 Shioaji 登入失敗，將於稍後重試。")
        return ok

    def keep_alive_heartbeat(self):
        """主動向券商主機發送輕量保活查詢 (Ping)，維持 TCP Session 熱連線，防閒置逾時 (僅在盤中在線時運作)"""
        if self.api is None or self.simulation or getattr(self, "_holiday_detected", False):
            return
        now = datetime.datetime.now()
        now_str = now.strftime("%H:%M:%S")
        if not ((now.weekday() < 5) and ("08:30:00" <= now_str <= "13:45:00")):
            return
        try:
            acc = getattr(self.api, "futopt_account", None)
            if acc:
                m = self.api.margin(acc)
                eq = float(getattr(m, "equity", 0.0) or 0.0)
                logger.debug(f"💓 [心跳保活] Shioaji 熱連線正常 (帳戶權益數: {eq:.0f}元)")
        except Exception as e:
            err_str = str(e).lower()
            logger.warning(f"⚠️ [心跳保活發現異常] {e}，立即啟動自動重連...")
            if any(k in err_str for k in ("authenticated", "session", "token", "disconnect", "connect", "timeout")):
                self.reconnect()

    # ------------------------------------------------------------------
    # 送單
    # ------------------------------------------------------------------
    def place_order(self, action: str, price: float, quantity: int = 1, price_type: str = "MKP", octype: str = "Auto"):
        """
        送出期貨委託單 (只負責送出，是否成交請用 _send_and_verify 確認)
        :param action: 'BUY' 或 'SELL'
        :param price: 限價價格 (若為 MKP 範圍市價則填 0 或當前價)
        :param quantity: 下單口數
        :param price_type: 'MKP' 範圍市價 (當沖突破建議使用，確保成交且防滑價), 'LMT' 限價
        :param octype: 'Auto' 自動, 'New' 新倉, 'Cover' 平倉, 'DayTrade' 當沖
        """
        if self.is_kill_switch_triggered:
            logger.warning("已觸發當日風控熔斷 (Kill Switch)，拒絕新增任何委託！")
            return None

        if self.api is None or self.contract is None or self.simulation:
            logger.info(f"[模擬下單] 送出委託: {action} {quantity}口 TMF @ {price} ({price_type}/{octype})")
            return {"status": "Simulated", "action": action, "price": price, "qty": quantity}

        import shioaji as sj
        sj_action = sj.Action.Buy if action == "BUY" else sj.Action.Sell
        if price_type == "MKT":
            sj_price_type = sj.FuturesPriceType.MKT
        elif price_type == "MKP":
            sj_price_type = sj.FuturesPriceType.MKP
        else:
            sj_price_type = sj.FuturesPriceType.LMT
        sj_octype = getattr(sj.FuturesOCType, octype, sj.FuturesOCType.Auto)

        try:
            order = self.api.Order(
                action=sj_action,
                price=price if price_type == "LMT" else 0,
                quantity=quantity,
                price_type=sj_price_type,
                order_type=sj.OrderType.IOC,
                octype=sj_octype,
                account=self.api.futopt_account
            )
            trade = self.api.place_order(self.contract, order)
        except Exception as e:
            logger.error(f"❌ 送單失敗 [{action}] {quantity}口: {e}")
            return None

        logger.info(f"📤 委託單已送出: [{action}] {quantity}口 ({price_type}/IOC/{octype}) -> {_status_name(trade.status.status)} (尚未確認成交)")
        return trade

    def wait_for_fill(self, trade, qty: int) -> FillResult:
        """輪詢委託狀態直到終態；逾時仍未成交就撤單，避免單子之後才偷偷成交"""
        deadline = time.time() + self.fill_timeout
        status = _status_name(trade.status.status)

        while True:
            try:
                self.api.update_status(self.api.futopt_account)
            except Exception as e:
                logger.warning(f"更新委託狀態失敗: {e}")
            status = _status_name(trade.status.status)
            if status in FINAL_STATUSES or time.time() >= deadline:
                break
            time.sleep(self.fill_poll_interval)

        # 逾時仍掛在市場上：撤單，再確認最終狀態
        if status in PENDING_STATUSES:
            logger.warning(f"⚠️ 委託 {self.fill_timeout:.0f} 秒仍為 {status}，嘗試撤單...")
            try:
                self.api.cancel_order(trade)
                time.sleep(self.fill_poll_interval)
                self.api.update_status(self.api.futopt_account)
            except Exception as e:
                logger.error(f"撤單失敗: {e}")
            status = _status_name(trade.status.status)

        deals = list(getattr(trade.status, "deals", None) or [])
        filled = sum(int(d.quantity) for d in deals)
        avg_price = (sum(float(d.price) * int(d.quantity) for d in deals) / filled) if filled else 0.0
        if filled == 0:
            filled = int(getattr(trade.status, "deal_quantity", 0) or 0)
        if filled == 0 and status == "Filled":
            filled = qty    # 狀態已是 Filled 但成交明細尚未更新
        return FillResult(ok=filled >= qty, filled_qty=filled, avg_price=avg_price, status=status)

    def _send_and_verify(self, action: str, qty: int, octype: str = "Auto", price: float = 0.0, price_type: str = "MKP") -> FillResult:
        """送單並確認成交結果，同時寫入稽核紀錄"""
        trade = self.place_order(action, price, qty, price_type, octype)
        if trade is None:
            result = FillResult(False, 0, 0.0, "Rejected")
        elif isinstance(trade, dict):      # 乾跑模式：沒有真正的券商回報
            result = FillResult(True, qty, price, "Simulated")
        else:
            result = self.wait_for_fill(trade, qty)

        if result.ok:
            logger.info(f"✅ 成交確認: [{action}] {qty}口 全部成交 均價 {result.avg_price:.0f} ({result.status})")
        else:
            logger.error(f"❌ 未完全成交: [{action}] 要求 {qty}口，實際成交 {result.filled_qty}口，狀態 {result.status}")
        self._audit(action, qty, octype, result)
        return result

    def _audit(self, action: str, qty: int, octype: str, result: FillResult):
        """把每次委託結果寫入 logs/<live|paper>/orders_YYYYMMDD.csv"""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            now = datetime.datetime.now()
            path = os.path.join(self.log_dir, f"orders_{now.strftime('%Y%m%d')}.csv")
            is_new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(["time", "action", "qty", "octype", "status", "filled_qty", "avg_price", "ok"])
                w.writerow([now.strftime("%H:%M:%S"), action, qty, octype, result.status,
                            result.filled_qty, f"{result.avg_price:.0f}", result.ok])
        except Exception as e:
            logger.warning(f"寫入稽核紀錄失敗: {e}")

    # ------------------------------------------------------------------
    # 帳上部位對帳與狀態持久化 (Position Reconciliation & State Recovery)
    # ------------------------------------------------------------------
    def get_real_position_details(self) -> dict:
        """
        查詢券商帳上 TMF 詳細部位資訊 (以券商帳上為唯一真實依據 Single Source of Truth)
        回傳: {"net": int, "avg_price": float, "direction": str, "positions": list}
        若無 API 或查詢失敗，回傳 {"net": None, ...}
        """
        if self.api is None or self.simulation:
            return {"net": None, "avg_price": 0.0, "direction": "None", "positions": []}
        try:
            positions = self.api.list_positions(self.api.futopt_account)
        except Exception as e:
            logger.error(f"查詢帳上部位詳細資訊失敗: {e}")
            err_str = str(e).lower()
            if any(k in err_str for k in ("authenticated", "session", "token", "disconnect", "connect")):
                self.reconnect()
            return {"net": None, "avg_price": 0.0, "direction": "None", "positions": []}

        net = 0
        tmf_positions = []
        total_cost = 0.0
        total_qty = 0

        for p in positions:
            code = str(getattr(p, "code", ""))
            if not code.startswith("TMF"):
                continue
            qty = int(getattr(p, "quantity", 0))
            d = _status_name(getattr(p, "direction", ""))
            price = float(getattr(p, "price", 0.0))
            tmf_positions.append({"code": code, "direction": d, "qty": qty, "price": price})
            if d == "Buy":
                net += qty
            else:
                net -= qty
            total_cost += price * qty
            total_qty += qty

        avg_price = (total_cost / total_qty) if total_qty > 0 else 0.0
        direction = "Buy" if net > 0 else ("Sell" if net < 0 else "Flat")
        return {"net": net, "avg_price": avg_price, "direction": direction, "positions": tmf_positions}

    def get_real_position(self) -> Optional[int]:
        """查詢券商帳上 TMF 淨部位：多單為正、空單為負、無部位為 0；查詢失敗回傳 None"""
        return self.get_real_position_details()["net"]

    def _get_state_path(self) -> str:
        """取得當日策略狀態檔路徑 (委由 StateManager 管理)"""
        return self.state_manager.get_state_path()

    def _save_state(self, strategy: ORBStrategy):
        """將當日策略即時狀態保存至 logs/state_YYYYMMDD.json (委由 StateManager 管理)"""
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        custom = {
            "eod_close_notified": bool(getattr(self, "_eod_close_notified", False)),
            "eod_done": bool(getattr(self, "_eod_done_date", None) == today_str),
        }
        return self.state_manager.save_state(strategy, entries_halted=self.entries_halted, custom_fields=custom)

    def _load_state(self) -> Optional[dict]:
        """讀取當日策略狀態檔 logs/state_YYYYMMDD.json (委由 StateManager 管理)"""
        return self.state_manager.load_state()

    def _reconcile_and_recover_on_startup(self, strategy: ORBStrategy, kbars: pd.DataFrame) -> set:
        """
        啟動或中斷重啟時，對帳並接管帳上真實部位：
        以券商帳上部位 (Single Source of Truth) 為最高依據。
        回傳已處理的時間戳記集合 processed_timestamps，防止重播歷史 K 棒重複送單。
        """
        processed_timestamps = set()
        if not kbars.empty:
            # 將目前收盤的歷史 K 棒全部標記為已處理，避免重播重複開倉
            processed_timestamps.update(kbars.index[:-1])

        pos_details = self.get_real_position_details()
        real_net = pos_details["net"]
        broker_avg = pos_details["avg_price"]
        saved_state = self._load_state()

        if real_net is None:
            # 模擬模式、乾跑模式或無法連線券商
            if (self.api is None or self.simulation):
                if saved_state:
                    logger.info("ℹ️ [模擬模式] 讀取本地策略狀態檔恢復進度...")
                    strategy.position = int(saved_state.get("position", 0))
                    strategy.entry_price = float(saved_state.get("entry_price", 0.0))
                    strategy.trade_count = int(saved_state.get("trade_count", 0))
                    strategy.highest_profit = float(saved_state.get("highest_profit", 0.0))
                    if saved_state.get("orb_calculated", False) and saved_state.get("orb_high") is not None:
                        strategy.orb_high = float(saved_state["orb_high"])
                        strategy.orb_low = float(saved_state["orb_low"])
                        strategy.orb_calculated = True
                    max_trades = getattr(strategy.config, "max_trades_per_day", None)
                    if max_trades is None:
                        max_trades = getattr(strategy.config, "max_initial_trades", 1) + (1 if getattr(strategy.config, "enable_sar", False) else 0)
                    if strategy.trade_count >= max_trades or saved_state.get("entries_halted", False):
                        self._halt_entries(f"今日交易次數已達上限 ({strategy.trade_count}次)")
                else:
                    logger.info("✅ [模擬模式] 啟動檢查完成，使用本地策略狀態追蹤部位 (略過券商帳務查詢)。")
            elif self.api is not None:
                logger.warning("⚠️ 啟動檢查: 無法查詢帳上部位，將無法進行帳戶對帳，請特別留意。")
            return processed_timestamps

        # 情況一：券商帳上有 TMF 部位 (實體持倉) -> 機器人必須無縫接管！
        if real_net != 0:
            target_pos = 1 if real_net > 0 else -1
            pos_label = "多單" if target_pos == 1 else "空單"
            
            strategy.position = target_pos
            strategy.trade_count = max(1, saved_state.get("trade_count", 1) if saved_state else 1)
            
            # 優先使用券商真實平均成本
            if broker_avg > 0:
                strategy.entry_price = broker_avg
            elif saved_state and saved_state.get("entry_price", 0) > 0:
                strategy.entry_price = float(saved_state["entry_price"])
            elif not kbars.empty:
                strategy.entry_price = float(kbars.iloc[-1]['close'])
                
            # 恢復最高浮盈
            if saved_state and "highest_profit" in saved_state:
                strategy.highest_profit = float(saved_state["highest_profit"])
            else:
                strategy.highest_profit = 0.0

            # 若有當日 K 棒，檢查持倉期間是否創出更高浮盈
            if not kbars.empty and strategy.entry_price > 0:
                if target_pos == 1:
                    max_gain = float(kbars['high'].max() - strategy.entry_price)
                else:
                    max_gain = float(strategy.entry_price - kbars['low'].min())
                if max_gain > strategy.highest_profit:
                    strategy.highest_profit = max(0.0, max_gain)

            logger.warning("=" * 75)
            logger.warning(f"🔄 [重啟接管部位] 帳上發現未平倉 TMF {pos_label} {abs(real_net)}口 (均價 {strategy.entry_price:.0f})！")
            logger.warning(f"🛡️ 機器人已成功接管該部位！持續執行停損、保本鎖利與 {strategy.config.force_close_time[:5]} 強制平倉風控。")
            logger.warning(f"📊 當前追蹤最高浮盈: {strategy.highest_profit:+.0f} 點")
            logger.warning("=" * 75)
            self._save_state(strategy)
            return processed_timestamps

        # 情況二：券商帳上無部位 (空手)
        logger.info("✅ 啟動檢查: 券商帳上目前無 TMF 部位 (淨部位 0)")
        strategy.position = 0
        strategy.highest_profit = 0.0

        if saved_state:
            # 恢復今日已交易次數與紀錄，避免重啟後重複突破開倉超過當日上限
            strategy.trade_count = saved_state.get("trade_count", 0)
            if saved_state.get("position", 0) != 0:
                logger.warning("⚠️ 本地紀錄原為持倉，但券商帳上已無部位 (可能已由手機App手動平倉或由券商停損)，策略拉回空手。")
            max_trades = getattr(strategy.config, "max_trades_per_day", None)
            if max_trades is None:
                max_trades = getattr(strategy.config, "max_initial_trades", 1) + (1 if getattr(strategy.config, "enable_sar", False) else 0)
            if strategy.trade_count >= max_trades:
                self._halt_entries(f"今日交易次數已達上限 ({strategy.trade_count}次)")
                logger.info(f"🛑 今日已完成 {strategy.trade_count} 次交易，不再開新倉，維持空手監控至收盤。")
        self._save_state(strategy)
        return processed_timestamps

    def flatten_real_position(self, retries: int = 3) -> bool:
        """依帳上真實部位反向平倉，前幾次用 MKP 防滑價，最後一次保底自動升級為 MKT 純市價必平；成功回傳 True"""
        for i in range(retries):
            real = self.get_real_position()
            if real is None:
                logger.error("無法查詢帳上部位，無法確認是否已平倉！請立即手動檢查券商軟體。")
                return False
            if real == 0:
                return True
            action = "SELL" if real > 0 else "BUY"
            ptype = "MKT" if (i == retries - 1 and retries > 1) else "MKP"
            logger.warning(f"🧹 平倉嘗試 {i + 1}/{retries} ({ptype}+IOC): 帳上 {real:+d} 口，送出 {action} {abs(real)}口")
            self._send_and_verify(action, abs(real), octype="Cover", price_type=ptype)
            time.sleep(self.retry_pause)
        return self.get_real_position() == 0

    def _halt_entries(self, reason: str):
        if not self.entries_halted:
            logger.error(f"🛑 停止開新倉: {reason}（仍允許平倉）")
        self.entries_halted = True

    def _post_trade_check(self, strategy: ORBStrategy, act: str, result: FillResult):
        """送單後以帳上真實部位對帳，並把策略狀態拉回與現實一致 (含券商帳務非同步延遲重試保護)"""
        expected = 0 if act == "EXIT" else (strategy.position * self.quantity)
        real = self.get_real_position()

        # 若成交回報已確認成交 (Filled/部分成交)，但 list_positions 短暫回傳舊值 (實盤常見 100~1000ms 帳本同步延遲)，
        # 最多重試 5 次 (每次間隔 0.4 秒，總計 2 秒) 確認最新帳務，防止誤判退回空手或重複平倉！
        if real is not None and real != expected and (result.ok or result.filled_qty > 0):
            for _ in range(5):
                time.sleep(0.4)
                real_retry = self.get_real_position()
                if real_retry is not None:
                    real = real_retry
                    if real == expected:
                        break

        if real is None:
            # 沒有券商資料可對帳 (乾跑模式，或查詢失敗)
            if self.api is not None and not result.ok:
                self._halt_entries("委託未成交且無法查詢帳上部位")
            return

        if real == expected:
            logger.info(f"📒 對帳通過: 帳上部位 {real:+d} 口 = 策略預期 {expected:+d} 口")
            if real == 0:
                self.pending_flatten = False
            self.order_failures = 0
            return

        logger.error(f"📒 對帳不符: 帳上 {real:+d} 口，策略預期 {expected:+d} 口")

        if expected == 0:
            # 策略認為已平倉，但帳上還有部位 -> 一定要平掉
            if not self.flatten_real_position():
                strategy.position = 1 if real > 0 else -1
                self.pending_flatten = True
                self._halt_entries("平倉失敗，帳上仍有部位")
                logger.critical("🚨 帳上仍有未平倉部位，啟動【不死鳥追平機制】每秒持續以 MKT+IOC 強制追殺直到 0 口！")
            else:
                self.pending_flatten = False
                logger.info("✅ 已補平帳上殘留部位")
            return

        if real == 0:
            # 若交易所已明確回報 Filled 全部成交，代表僅是券商 list_positions 帳務更新較慢，絕不將真實持倉洗回空手！
            if result.ok and result.status == "Filled" and result.filled_qty >= self.quantity:
                logger.warning(f"⚠️ 成交回報已確認 Filled ({result.filled_qty}口 @ {result.avg_price:.0f})，但帳務查詢仍顯示 0 口，保留策略持倉並等待定期對帳同步！")
                self.order_failures = 0
                return
            # 開倉單根本沒成交：策略退回空手，讓之後可以重新判斷
            strategy.position = 0
            strategy.highest_profit = 0.0
            strategy.trade_count = max(0, strategy.trade_count - 1)
            self.order_failures += 1
            logger.warning(f"開倉單未成交，策略退回空手 (連續失敗 {self.order_failures}/{self.max_order_failures})")
            if self.order_failures >= self.max_order_failures:
                self._halt_entries("開倉單連續未成交")
            return

        # 其他不一致 (方向相反、口數不對、部分成交)：為安全起見全部平掉
        logger.error("部位方向或口數與預期不符，為安全起見全部平倉")
        if not self.flatten_real_position():
            self.pending_flatten = True
            strategy.position = 1 if real > 0 else -1
        else:
            self.pending_flatten = False
            strategy.position = 0
            strategy.highest_profit = 0.0
        self._halt_entries("成交結果與預期不符")
        self._save_state(strategy)

    def _periodic_reconcile(self, strategy: ORBStrategy):
        """沒有交易動作時的定期對帳 (例如你在券商軟體手動平倉或加碼)"""
        real = self.get_real_position()
        if real is None:
            return
        expected = strategy.position * self.quantity
        if real == expected:
            return
        logger.warning(f"⚠️ 定期對帳發現不一致: 帳上 {real:+d} 口，策略預期 {expected:+d} 口（可能有手動操作）")
        if real == 0:
            strategy.position = 0
            strategy.highest_profit = 0.0
            self.pending_flatten = False
        elif strategy.position != 0:
            strategy.position = 1 if real > 0 else -1
        self._halt_entries("帳上部位與策略不一致，需人工確認")
        self._save_state(strategy)

    def _force_flatten_all(self, strategy: ORBStrategy, cur_price: float = 0.0):
        """強制平倉：以帳上真實部位為準，查不到才退回用策略部位"""
        real = self.get_real_position()
        if real is None:
            if strategy.position == 0 and not self.pending_flatten:
                return
            action = "SELL" if strategy.position == 1 else "BUY"
            logger.warning("依策略部位執行平倉回補")
            self._send_and_verify(action, self.quantity, octype="Cover", price=cur_price)
        elif real != 0:
            logger.warning(f"⏰ 帳上有 {real:+d} 口，執行強制平倉！")
            if not self.flatten_real_position():
                self.pending_flatten = True
                logger.critical("🚨 平倉失敗，帳上仍有部位，啟動【不死鳥追平機制】持續以 MKT+IOC 追殺直到 0 口！")
                return
            self.pending_flatten = False
        strategy.position = 0
        strategy.highest_profit = 0.0
        self._save_state(strategy)

    def _relentless_flatten_step(self, strategy: ORBStrategy) -> bool:
        """
        不死鳥追平單步執行：
        當盤中平倉未果 (pending_flatten=True) 或已過收盤時間仍有殘留部位時，
        不需等待下一根 1 分 K，每隔 1~2 秒自動檢測連線並以 MKT+IOC 持續追殺直到帳上歸零 (0 口)。
        """
        real = self.get_real_position()
        if real is None:
            if self.api is not None and not self.simulation:
                self.reconnect()
                real = self.get_real_position()
            if real is None:
                return False

        if real == 0:
            if self.pending_flatten or strategy.position != 0:
                logger.info("🦅 [不死鳥追平完成] 確認券商帳上部位已歸零 (0 口)！")
                self.pending_flatten = False
                strategy.position = 0
                strategy.highest_profit = 0.0
                self._save_state(strategy)
            return True

        action = "SELL" if real > 0 else "BUY"
        logger.warning(f"🦅 [不死鳥持續追平] 帳上仍有 {real:+d} 口，立即補送 MKT+IOC 強制平倉 ({action} {abs(real)}口)...")
        res = self._send_and_verify(action, abs(real), octype="Cover", price_type="MKT")
        if res.status == "Rejected" and self.api is not None and not self.simulation:
            self.reconnect()

        after_real = self.get_real_position()
        if after_real == 0:
            logger.info("🦅 [不死鳥追平成功] 帳上殘留部位已全數清零 (0 口)！")
            self.pending_flatten = False
            strategy.position = 0
            strategy.highest_profit = 0.0
            self._save_state(strategy)
            return True
        return False

    def _check_external_command(self, strategy: ORBStrategy, cur_price: float = 0.0):
        """每秒檢查 logs/<live|paper>/command.json 外部即時指令 (支援對話框或 CLI 隨時下令秒級平倉)"""
        cmd_path = os.path.join(self.log_dir, "command.json")
        if not os.path.exists(cmd_path):
            return
        try:
            with open(cmd_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(cmd_path)
        except Exception as e:
            logger.warning(f"讀取外部指令檔失敗: {e}")
            return

        cmd = str(data.get("cmd", "")).lower().strip()
        if cmd in ("close", "close_and_halt", "flatten"):
            had_pos = (strategy.position != 0) or bool(self.get_real_position())
            pnl_str = ""
            if strategy.position != 0 and strategy.entry_price > 0 and cur_price > 0:
                pnl = (cur_price - strategy.entry_price) if strategy.position == 1 else (strategy.entry_price - cur_price)
                pnl_str = f" @ {cur_price:.0f} (約 {pnl:+.0f}點)"
            self._force_flatten_all(strategy, cur_price=cur_price)
            if cmd == "close_and_halt":
                self._halt_entries("收到手動平倉並停開新倉指令")
            if had_pos:
                msg = f"⚡ [手動指令平倉完成] 已立即平倉回補部位{pnl_str}！"
            else:
                msg = "ℹ️ [手動指令平倉] 收到平倉指令，目前帳上與策略皆無部位 (空手)。"
            logger.info(msg)
            self.notifier.send(msg)
        elif cmd == "resume":
            self.entries_halted = False
            logger.info("▶️ [外部指令] 已恢復允許開新倉。")

    # ------------------------------------------------------------------
    # K 棒與主迴圈
    # ------------------------------------------------------------------
    def _fetch_cnyes_history(self, timeout: int = 5) -> pd.DataFrame:
        """從 CNYES 公開行情介面抓取近月台指期 1分K 資料 (使用跨平台 SSL 上下文)"""
        import urllib.request
        from live.notifier import _ssl_ctx

        url = "https://ws.api.cnyes.com/ws/api/v1/charting/history?symbol=TWF:TXF:FUTURES&resolution=1&quote=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
            payload = json.loads(r.read().decode("utf-8"))
        data = payload.get("data", {})
        t_list = data.get("t", [])
        if not t_list:
            return pd.DataFrame()
        return pd.DataFrame({
            "ts": [pd.Timestamp(datetime.datetime.fromtimestamp(ts)) for ts in t_list],
            "open": data.get("o", []),
            "high": data.get("h", []),
            "low": data.get("l", []),
            "close": data.get("c", []),
            "volume": data.get("v", []),
        })

    def _fetch_backup_intraday_kbars(self, today_str: str) -> pd.DataFrame:
        """當永豐模擬環境 api.kbars 壅塞逾時時，自動從高速台指期 1分K 備援通道回補今日已收盤 K 棒"""
        try:
            df = self._fetch_cnyes_history(timeout=4)
            if df.empty:
                return pd.DataFrame()
            df = df.set_index("ts").sort_index()
            day_df = df[df.index.strftime("%Y-%m-%d") == today_str].between_time("08:45", "13:45")
            return day_df[["open", "high", "low", "close", "volume"]].copy()
        except Exception as e:
            logger.debug(f"備援 K 棒回補失敗: {e}")
            return pd.DataFrame()

    @staticmethod
    def _is_today_day_session_snapshot(snap, today_str: str) -> bool:
        """
        驗證 Shioaji 快照是否屬於「今日日盤 (08:45:00 ~ 13:46:00)」。
        可防止颱風天、國定假日或開盤前讀取到前一日收盤或昨夜盤 05:00 殘留價格。
        """
        snap_ts_raw = getattr(snap, "ts", None)
        if snap_ts_raw is None or not isinstance(snap_ts_raw, (int, float, str, datetime.datetime, pd.Timestamp)):
            return True
        try:
            if isinstance(snap_ts_raw, (int, float)):
                if snap_ts_raw <= 0:
                    return False
                snap_dt = pd.to_datetime(int(snap_ts_raw)).to_pydatetime()
            else:
                snap_dt = pd.to_datetime(snap_ts_raw).to_pydatetime()
            return (
                snap_dt.strftime("%Y-%m-%d") == today_str
                and datetime.time(8, 45, 0) <= snap_dt.time() <= datetime.time(13, 46, 0)
            )
        except Exception:
            return False

    def fetch_today_day_kbars(self) -> pd.DataFrame:
        """從 Shioaji 抓取今日日盤 (08:45 ~ 13:45) 1分K 資料 (委由 BarAggregator 管理即時 Tick 串流合成 + Snapshot 快照防逾時)"""
        if self.api is None or self.contract is None:
            return pd.DataFrame()
        today_str = datetime.date.today().strftime("%Y-%m-%d")

        # 1. 若尚未載入早盤歷史 K 棒，先嘗試 Shioaji kbars (限時 5 秒避免卡死)
        if not self.bar_aggregator.history_loaded and not self.bar_aggregator.kbars_disabled:
            try:
                kbars = self.api.kbars(self.contract, start=today_str, end=today_str, timeout=5000)
                df = pd.DataFrame({**kbars})
                if not df.empty:
                    df["ts"] = pd.to_datetime(df["ts"])
                    df = df.set_index("ts")
                    df.columns = [c.lower() for c in df.columns]
                    self.bar_aggregator.load_history_kbars(df, today_str=today_str)
            except Exception:
                self.bar_aggregator.kbars_disabled = True
                logger.info("⚡ [高速模式] 永豐模擬環境 kbars 歷史查詢壅塞，已自動切換為「Shioaji 即時 Tick 串流 + Snapshot 快照 + 高速歷史回補」模式！")

        # 2. 若模擬環境 kbars 壅塞且尚未載入今日早盤歷史 K 棒，從備援通道一次性補齊
        if self.bar_aggregator.kbars_disabled and not self.bar_aggregator.history_loaded:
            backup_df = self._fetch_backup_intraday_kbars(today_str)
            if not backup_df.empty:
                now_min = pd.Timestamp(datetime.datetime.now().replace(second=0, microsecond=0))
                self.bar_aggregator.load_backup_bars(backup_df, now_min=now_min)
                logger.info(f"⚡ [高速回補完成] 已載入今日 08:45 至今共 {len(backup_df)} 根 1分K，後續由 Shioaji 即時 Tick 串流與 Snapshot 零延遲更新！")

        # 3. 若超過 3 秒未收到新 Tick (或尚未有當分鐘 Bar)，才透過 Shioaji Snapshot 同步報價，平時直接由記憶體 Tick 0ms 合成
        now_dt = datetime.datetime.now()
        now_ts = pd.Timestamp(now_dt.replace(second=0, microsecond=0))
        if self.bar_aggregator.needs_snapshot_sync(now_ts):
            try:
                snaps = self.api.snapshots([self.contract])
                if snaps:
                    s = snaps[0]
                    price = float(getattr(s, "close", 0.0) or 0.0)
                    vol = int(getattr(s, "volume", 1) or 1)
                    if price > 0 and self._is_today_day_session_snapshot(s, today_str):
                        self.bar_aggregator.update_snapshot(price, volume=vol, dt=now_dt)
            except Exception as snap_err:
                err_str = str(snap_err).lower()
                if any(k in err_str for k in ("authenticated", "session", "token", "disconnect", "connect")):
                    self.reconnect()

        day_df = self.bar_aggregator.to_dataframe(today_str=today_str, start_time="08:45", end_time="13:45")
        if day_df.empty:
            return pd.DataFrame()
        return day_df[["open", "high", "low", "close", "volume"]]

    def _handle_action(self, strategy: ORBStrategy, res: dict, ts: pd.Timestamp, bar: pd.Series, prev_pos: int):
        """依策略動作送單、確認成交，並對帳"""
        act = res.get("action", "HOLD")
        if act == "HOLD":
            return
        rec = res.get("record", {})
        q = self.quantity
        is_entry = act in ("BUY", "SELL", "SAR_BUY", "SAR_SELL")

        if is_entry and self.entries_halted:
            # 不開新倉：把策略狀態退回，SAR 的反手單則只做前半段(平掉原部位)
            logger.warning(f"🛑 已停止開新倉，忽略訊號 {act}")
            if act in ("SAR_BUY", "SAR_SELL"):
                close_action = "SELL" if prev_pos == 1 else "BUY"
                self._send_and_verify(close_action, q, octype="Cover", price=rec.get('exit_price', 0))
            strategy.position = 0
            strategy.highest_profit = 0.0
            if act in ("BUY", "SELL"):
                strategy.trade_count = max(0, strategy.trade_count - 1)
            self._post_trade_check(strategy, act, FillResult(True, q, 0.0, "Skipped"))
            return

        if act == "BUY":
            msg = f"🟢 [訊號觸發] {ts.strftime('%H:%M')} 做多進場 @ {bar['close']:.0f} ({res.get('detail')})"
            logger.info(msg)
            result = self._send_and_verify("BUY", q, price=bar['close'])
            self.notifier.send(f"{msg}\n成交均價: {result.avg_price:.0f} ({result.status})")
        elif act == "SELL":
            msg = f"🔴 [訊號觸發] {ts.strftime('%H:%M')} 做空進場 @ {bar['close']:.0f} ({res.get('detail')})"
            logger.info(msg)
            result = self._send_and_verify("SELL", q, price=bar['close'])
            self.notifier.send(f"{msg}\n成交均價: {result.avg_price:.0f} ({result.status})")
        elif act == "EXIT":
            strategy.position = 0
            close_action = "SELL" if prev_pos == 1 else "BUY"
            msg = f"🏁 [平倉回補] {ts.strftime('%H:%M')} {rec.get('reason')} @ {rec.get('exit_price'):.0f} (損益: {rec.get('pnl_pts'):+.0f}點)"
            logger.info(msg)
            result = self._send_and_verify(close_action, q, octype="Cover", price=rec.get('exit_price', 0))
            self.notifier.send(msg)
        elif act == "SAR_SELL":
            msg = f"🔄 [假突破反手做空 SAR] {ts.strftime('%H:%M')} 多單停損並反手做空 @ {rec.get('exit_price'):.0f}"
            logger.info(msg)
            result = self._send_and_verify("SELL", q * 2, price=rec.get('exit_price', 0))
            self.notifier.send(msg)
        elif act == "SAR_BUY":
            msg = f"🔄 [假跌破反手做多 SAR] {ts.strftime('%H:%M')} 空單停損並反手做多 @ {rec.get('exit_price'):.0f}"
            logger.info(msg)
            result = self._send_and_verify("BUY", q * 2, price=rec.get('exit_price', 0))
            self.notifier.send(msg)
        else:
            return

        # 開倉成功後，用實際成交價取代回測用的收盤價，停損與鎖利才會以真實成本計算
        if is_entry and result.ok and result.avg_price > 0:
            if abs(result.avg_price - strategy.entry_price) >= 1:
                logger.info(f"📌 進場價以實際成交價為準: {strategy.entry_price:.0f} -> {result.avg_price:.0f}")
            strategy.entry_price = result.avg_price

        self._post_trade_check(strategy, act, result)
        self._save_state(strategy)

    def _save_overnight_level_to_csv(self, data_dir: str, target_date_str: str, night_bars: pd.DataFrame):
        """將抓到的前一日夜盤極值自動寫入 overnight_levels_summary.csv 永久保存"""
        try:
            os.makedirs(data_dir, exist_ok=True)
            o_high = float(night_bars["high"].max())
            o_low = float(night_bars["low"].min())
            o_open = float(night_bars.iloc[0]["open"])
            o_close = float(night_bars.iloc[-1]["close"])
            new_row = pd.DataFrame([{
                "trading_date": target_date_str,
                "night_start": str(night_bars["ts"].min()),
                "night_end": str(night_bars["ts"].max()),
                "night_bars": len(night_bars),
                "night_open": o_open,
                "night_high": o_high,
                "night_low": o_low,
                "night_close": o_close,
                "night_range": o_high - o_low,
            }])
            lvl_path = os.path.join(data_dir, "overnight_levels_summary.csv")
            if os.path.exists(lvl_path):
                old_df = pd.read_csv(lvl_path)
                combined = pd.concat([old_df[old_df["trading_date"] != target_date_str], new_row], ignore_index=True)
                combined = combined.sort_values("trading_date").reset_index(drop=True)
            else:
                combined = new_row
            combined.to_csv(lvl_path, index=False)
        except Exception as e:
            logger.debug(f"自動儲存夜盤極值至 CSV 失敗: {e}")

    def load_overnight_levels_for_strategy(self, strategy: ORBStrategy, target_date_str: str):
        """讀取或查詢指定交易日所對應的前一日夜盤 (15:00~05:00) 極值並注入策略 (防撞牆保護 Wall Filter)"""
        if not getattr(strategy.config, "use_overnight_wall_filter", False):
            return

        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        # 1. 優先從本地夜盤高低點彙整表 (overnight_levels_summary.csv) 讀取
        for fn in ["overnight_levels_summary.csv", "overnight_levels_30days.csv"]:
            lvl_path = os.path.join(data_dir, fn)
            if os.path.exists(lvl_path):
                try:
                    df_lvl = pd.read_csv(lvl_path)
                    match_row = df_lvl[df_lvl["trading_date"] == target_date_str]
                    if not match_row.empty:
                        o_h = float(match_row.iloc[0]["night_high"])
                        o_l = float(match_row.iloc[0]["night_low"])
                        strategy.set_overnight_levels(o_h, o_l)
                        logger.info(f"🌙 [夜盤防撞牆] 已載入 {target_date_str} 夜盤極值: 夜高={o_h:.0f}, 夜低={o_l:.0f} (門檻={getattr(strategy.config, 'overnight_wall_threshold_pts', 40.0):.0f}點)")
                        return
                except Exception as e:
                    logger.debug(f"讀取 {fn} 失敗: {e}")

        cur_d = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
        end_night = datetime.datetime.combine(cur_d, datetime.time(5, 1, 0))
        start_night = datetime.datetime.combine(cur_d - datetime.timedelta(days=4), datetime.time(15, 0, 0))

        # 2. 若本地無紀錄且 API 可用 (含 live 與 paper 各自已登入之 self.api)，即時從 Shioaji 抓取前一日夜盤 15:00~05:00
        if self.api is not None and self.contract is not None:
            try:
                prev_4d = (cur_d - datetime.timedelta(days=4)).strftime("%Y-%m-%d")
                kbs = self.api.kbars(self.contract, start=prev_4d, end=target_date_str, timeout=5000)
                df_k = pd.DataFrame({**kbs})
                if not df_k.empty:
                    df_k.columns = [c.lower() for c in df_k.columns]
                    df_k["ts"] = pd.to_datetime(df_k["ts"])
                    subset = df_k[(df_k["ts"] >= start_night) & (df_k["ts"] <= end_night)]
                    evening_bars = subset[subset["ts"].dt.hour >= 15]
                    if not evening_bars.empty:
                        last_ev_date = evening_bars["ts"].dt.date.max()
                        night_start_exact = datetime.datetime.combine(last_ev_date, datetime.time(15, 0, 0))
                        night_bars = df_k[(df_k["ts"] >= night_start_exact) & (df_k["ts"] <= end_night)]
                        if not night_bars.empty:
                            o_h = float(night_bars["high"].max())
                            o_l = float(night_bars["low"].min())
                            strategy.set_overnight_levels(o_h, o_l)
                            self._save_overnight_level_to_csv(data_dir, target_date_str, night_bars)
                            logger.info(f"🌙 [夜盤防撞牆] 從 API 即時取得 {target_date_str} 夜盤極值並寫入 CSV: 夜高={o_h:.0f}, 夜低={o_l:.0f}")
                            return
            except Exception as api_err:
                logger.warning(f"從 Shioaji API 抓取夜盤極值逾時或略過，嘗試公開行情備援: {api_err}")

        # 3. 免登入公開高速行情備援 (CNYES，完全不觸碰 simulation=False，防模擬主機 kbars 塞車逾時)
        try:
            df_raw = self._fetch_cnyes_history(timeout=5)
            if not df_raw.empty:
                df_c = df_raw[["ts", "open", "high", "low", "close"]].sort_values("ts").reset_index(drop=True)
                subset = df_c[(df_c["ts"] >= start_night) & (df_c["ts"] <= end_night)]
                evening_bars = subset[subset["ts"].dt.hour >= 15]
                if not evening_bars.empty:
                    last_ev_date = evening_bars["ts"].dt.date.max()
                    night_start_exact = datetime.datetime.combine(last_ev_date, datetime.time(15, 0, 0))
                    night_bars = df_c[(df_c["ts"] >= night_start_exact) & (df_c["ts"] <= end_night)]
                    if not night_bars.empty:
                        o_h = float(night_bars["high"].max())
                        o_l = float(night_bars["low"].min())
                        strategy.set_overnight_levels(o_h, o_l)
                        self._save_overnight_level_to_csv(data_dir, target_date_str, night_bars)
                        logger.info(f"🌙 [夜盤防撞牆] 從公開行情備援取得 {target_date_str} 夜盤極值並寫入 CSV: 夜高={o_h:.0f}, 夜低={o_l:.0f}")
                        return
        except Exception as cnyes_err:
            logger.debug(f"從公開備援抓取夜盤極值失敗: {cnyes_err}")

        logger.info(f"ℹ️ {target_date_str} 未取得前一日夜盤極值 (或前日休市無夜盤)，維持標準 ORB 模式運作")

    def run_intraday_bot(self, strat_cfg: ORBStrategyConfig = None):
        """
        啟動日盤全自動監控與下單迴圈 (08:45 ~ 13:24)
        每分鐘自動更新 1分K、計算 ORB 高低點與 VWAP/EMA 指標，觸發訊號時自動下單並確認成交
        支援中途斷線 / 重啟無縫接管帳上真實部位與狀態恢復
        """
        base_cfg = strat_cfg or ORBStrategyConfig()
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        strat_cfg, atr_info = config_for_date(base_cfg, data_dir, today_str)
        strategy = ORBStrategy(config=strat_cfg, inst_config=InstrumentConfig())
        self._active_strategy = strategy
        self._stop_requested = False
        try:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
        except Exception:
            pass

        self.load_overnight_levels_for_strategy(strategy, today_str)
        last_reconcile = time.time()
        last_state_save = time.time()
        last_heartbeat_time = time.time()
        last_log_time = 0.0
        failsafe_time = getattr(strat_cfg, "failsafe_close_time", "13:09:00")
        shutdown_time = getattr(strat_cfg, "shutdown_time", "13:10:00")

        # 預先讀取今日狀態檔之收盤通知旗標 (避免盤後重啟重複發送 13:10 / 13:45 通知與重跑滾動更新)
        eod_done_date = None
        saved_init = self._load_state()
        if saved_init and saved_init.get("date") == today_str:
            self._eod_close_notified = bool(saved_init.get("eod_close_notified", False))
            if saved_init.get("eod_done", False):
                self._eod_done_date = today_str
                eod_done_date = today_str

        logger.info("=" * 75)
        logger.info("🤖 TMF 微台指 ORB Pro 自動當沖機器人 (雙速極速版 + ATR 動態波動自適應) 已啟動！")
        logger.info(f"• 下單口數: {self.quantity} 口 | 開盤區間: {strat_cfg.orb_start_time} ~ {strat_cfg.orb_end_time}")
        logger.info(f"• 波動自適應: {describe(strat_cfg, atr_info)}")
        logger.info("• 極速引擎: 換分 0.15 秒確認突破 | 持倉 Tick 觸價 <5ms 立即喚醒停損/鎖利 | 150ms 成交確認")
        logger.info(f"• 收盤安全時程: {strat_cfg.force_close_time[:5]} 策略平倉 | {failsafe_time[:5]} 底線強制補平 | {shutdown_time[:5]} 零部位安全退出")
        logger.info("• 重啟保護: 自動偵測帳上未平倉部位並無縫接管風控；歷史 K 棒不重播下單")
        logger.info(f"• 每張單都會確認成交並與帳上部位對帳，稽核紀錄寫入 {os.path.relpath(self.log_dir, os.path.dirname(os.path.dirname(self.log_dir)))}/orders_YYYYMMDD.csv")
        logger.info("• 按下 Ctrl+C 或執行 ./stop.sh 可隨時安全秒停程式。")
        logger.info("=" * 75)
        self.notifier.send(f"🤖 TMF 自動當沖機器人已啟動 ({today_str})\n口數: {self.quantity}口 | {describe(strat_cfg, atr_info)}")

        # 1. 抓取今日日盤 K 棒，計算指標與 ORB 區間 (若已過 09:15)
        _last_prepared_ver = -1
        _cached_kbars = pd.DataFrame()
        kbars = self.fetch_today_day_kbars()
        if not kbars.empty and len(kbars) >= 2:
            kbars = strategy.prepare_indicators(kbars)
            _last_prepared_ver = self.bar_aggregator.version
            _cached_kbars = kbars
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            if now_str >= strat_cfg.orb_end_time:
                orb_h, orb_l = strategy.calculate_orb_levels(kbars)
                if orb_h and orb_l:
                    logger.info(f"📌 今日現貨開盤區間 ({strat_cfg.orb_start_time}~{strat_cfg.orb_end_time}) 已鎖定: 上界={orb_h:.0f}, 下界={orb_l:.0f} (振幅={orb_h-orb_l:.0f}點)")

        # 2. 帳上部位對帳與狀態恢復 / 部位接管
        processed_timestamps = self._reconcile_and_recover_on_startup(strategy, kbars)
        active_date_str = today_str
        last_standby_log = 0.0
        self._holiday_detected = False
        holiday_empty_since = 0.0

        # 若啟動時即處於非交易時段 (週末、平日 13:50 以後、或清晨 08:30 之前)，且帳上確定 0 部位，
        # 在完成初始檢查與對帳後，立即登出 API 進入離線省電深度休眠，避免夜間券商維護中斷與不必要連線
        now_init = datetime.datetime.now()
        now_init_str = now_init.strftime("%H:%M:%S")
        is_init_off_hours = (now_init.weekday() >= 5) or (now_init_str >= "13:50:00") or (now_init_str < "08:30:00")
        if is_init_off_hours and strategy.position == 0 and not self.pending_flatten:
            self.logout_and_sleep(reason="啟動時處於非交易時段")

        try:
            while not self._stop_requested:
                now = datetime.datetime.now()
                cur_date_str = now.strftime("%Y-%m-%d")
                now_str = now.strftime("%H:%M:%S")

                # 跨日自動換日重置 (00:00 過後自動載入新交易日 ATR 參數並清空前日快取)
                if cur_date_str != active_date_str:
                    active_date_str = cur_date_str
                    self.bar_aggregator.clear()
                    _last_prepared_ver = -1
                    _cached_kbars = pd.DataFrame()
                    processed_timestamps.clear()
                    self.entries_halted = False
                    self.pending_flatten = False
                    self._morning_wake_logged = False
                    self._noon_cutoff_sent = False
                    self._eod_close_notified = False
                    self._eod_done_date = None
                    self._holiday_detected = False
                    holiday_empty_since = 0.0
                    self.order_failures = 0
                    strat_cfg, atr_info = config_for_date(base_cfg, data_dir, active_date_str)
                    strategy = ORBStrategy(config=strat_cfg, inst_config=InstrumentConfig())
                    self._active_strategy = strategy
                    
                    # 若 API 仍在線才載入合約與夜盤；若已離線休眠，留至 08:30 喚醒時完整載入 (夜盤05:00才完全收盤)
                    if self.api is not None:
                        self.load_overnight_levels_for_strategy(strategy, active_date_str)
                        self.load_tmf_contract(self._contract_code)

                    logger.info("=" * 75)
                    logger.info(f"🌅 [跨日自動換日] 進入新交易日 {active_date_str}，已重新載入今日 20日 ATR 動態參數：")
                    logger.info(f"• 當前交易合約: {self._contract_code}")
                    logger.info(f"• 波動自適應: {describe(strat_cfg, atr_info)}")
                    if self.api is None:
                        logger.info("• 連線狀態: 💤 離線省電深度睡眠中 (將於今日 08:30:00 自動喚醒登入)")
                    logger.info("=" * 75)

                # 盤中不死鳥追平：若前一秒平倉未果 (pending_flatten=True)，不等待下一根 K 棒，每秒持續以 MKT+IOC 追殺直到 0 口
                if self.pending_flatten:
                    self._relentless_flatten_step(strategy)

                # 若今日已自動判定為休市日 (國定假日/颱風休市)，且帳上無部位，維持深度離線休眠至跨日，不存取任何 API 也不誤發盤後通知
                if self._holiday_detected and strategy.position == 0 and not self.pending_flatten:
                    if time.time() - last_standby_log >= 1800.0:
                        last_standby_log = time.time()
                        logger.info(f"🏖️ [{now_str}] 今日為休市日，深度離線省電休眠中 (帳上零部位)，等待下一交易日 08:30 自動喚醒...")
                    self._wake_event.clear()
                    self._wake_event.wait(timeout=10.0)
                    continue

                # 第二道防線：13:09:00 後帳上仍有部位，不等待 K 棒直接強制平倉
                if now_str >= failsafe_time:
                    self._force_flatten_all(strategy)

                # 第三道防線：13:10:00 後完成當日收盤檢查；若帳上仍有殘留部位，絕不進入待機，每隔 2 秒持續自動重連並以 MKT+IOC 追殺直到 0 口！
                if now_str >= shutdown_time:
                    # 若已離線休眠 (api is None) 且無待平倉，說明之前已確認 0 部位並正式休眠，不再呼叫券商查詢
                    final_pos = 0 if (self.api is None and not self.pending_flatten) else self.get_real_position()
                    if final_pos is None and self.api is not None and not self.simulation:
                        self.reconnect()
                        final_pos = self.get_real_position()
                    if final_pos or self.pending_flatten:
                        logger.critical(f"🚨 [收盤後不死鳥追平] 已過 {shutdown_time} 帳上仍有殘留部位 ({final_pos})，拒絕進入待機，每 2 秒持續以 MKT+IOC 強制追平直到 0 口！")
                        self._relentless_flatten_step(strategy)
                        self._wake_event.clear()
                        self._wake_event.wait(timeout=2.0)
                        continue

                    # 13:10:00 嚴格當沖平倉已完成 (帳上保證 0 口部位)
                    if not getattr(self, "_eod_close_notified", False):
                        self._eod_close_notified = True
                        self._save_state(strategy)
                        self.notifier.send(
                            f"🛡️ [13:10 當沖平倉完成]\n"
                            f"收盤風控稽核通過，帳上部位確認為 0 口！\n"
                            f"• 今日無留倉、零過夜風險。\n"
                            f"• 等待 13:45 台指日盤收盤數據結算..."
                        )

                    # 檢查是否到達 13:45:15 (台指日盤正式收盤)，抓取今日完整 300 根 K 棒執行滾動回測與日總結
                    if now_str >= "13:45:15" and eod_done_date != cur_date_str:
                        eod_done_date = cur_date_str
                        self._eod_done_date = cur_date_str
                        self._save_state(strategy)
                        logger.info(f"🏁 日盤正式收盤 (13:45)！帳上確認 0 部位，開始執行今日 ({cur_date_str}) 盤後總結與數據滾動...")
                        try:
                            from live.update_20days import roll_20days
                            roll_20days(push_git=True)
                        except Exception as roll_err:
                            logger.warning(f"自動滾動更新 20 日資料略過: {roll_err}")
                        next_cfg, next_info = config_for_date(base_cfg, data_dir, "9999-12-31")
                        logger.info("=" * 75)
                        logger.info("🌙 [跨日待機模式] 今日日報表與 20 日回測已更新，進入盤後低耗待命守護！")
                        logger.info(f"📐 下一交易日預載參數: {describe(next_cfg, next_info)}")
                        logger.info("=" * 75)
                        self.notifier.send(
                            f"🏁 [13:45 日盤正式收盤總結]\n"
                            f"今日 ({cur_date_str}) 日盤正式收盤！\n"
                            f"• 完整 300 根 1分K 已歸檔\n"
                            f"• 20 日滾動資料集已更新並推送到 GitHub\n"
                            f"• 下一交易日預載 ATR 參數已生效：\n  {describe(next_cfg, next_info)}"
                        )
                        # 盤後安全登出：正式進入深度省電防打擾睡眠，免除夜間維護 503 警報與網路消耗
                        if self.api is not None and strategy.position == 0 and not self.pending_flatten:
                            self.logout_and_sleep(reason="13:45 盤後結算完成 (明日 08:30 自動喚醒)")
                    elif now_str < "13:45:15" and eod_done_date != cur_date_str:
                        if time.time() - last_standby_log >= 300.0:
                            last_standby_log = time.time()
                            logger.info(f"☕ [{now_str}] 今日當沖交易已完成 (0部位)，等待 13:45:15 台指日盤正式收盤後執行滾動回測與日總結...")
                        self._wake_event.clear()
                        self._wake_event.wait(timeout=5.0)
                        continue

                    if time.time() - last_standby_log >= 600.0:
                        last_standby_log = time.time()
                        logger.info(f"🌙 [{now_str}] 盤後待機守護中 (帳上零部位)，等待下一交易日 08:45:00 日盤開盤...")
                    self._wake_event.clear()
                    self._wake_event.wait(timeout=5.0)
                    continue

                # 週末休市待機 (週六、週日)
                if now.weekday() >= 5:
                    if self.api is not None and strategy.position == 0 and not self.pending_flatten:
                        self.logout_and_sleep(reason="週末休市")
                    if time.time() - last_standby_log >= 1800.0:
                        last_standby_log = time.time()
                        logger.info(f"☕ [{now_str}] 週末休市待命守護中 (週六/週日)，等待週一 08:45:00 日盤開盤...")
                    self._wake_event.clear()
                    self._wake_event.wait(timeout=10.0)
                    continue

                # 交易日清晨開盤前待機 (00:00:00 ~ 08:44:59)
                if now_str < "08:45:00":
                    # 08:30:00 開盤前定時自動喚醒登入並載入夜盤數據
                    if now_str >= "08:30:00" and self.api is None:
                        logger.info(f"⏰ [{now_str}] 到達 08:30 預排定時，啟動開盤前自動登入與連線...")
                        if self.wake_and_login(reason="08:30 開盤前自動喚醒"):
                            self.load_overnight_levels_for_strategy(strategy, active_date_str)

                    if now_str >= "08:35:00" and not getattr(self, "_morning_wake_logged", False):
                        self._morning_wake_logged = True
                        logger.info(f"🌅 [{now_str}] 執行開盤前全系統自動健康巡檢 (Pre-Market Health Check)...")
                        try:
                            from live.health_check import run_health_check
                            mode_str = "paper" if self.simulation else "live"
                            run_health_check(mode=mode_str, qty=self.quantity, send_notify=True)
                        except Exception as hc_err:
                            logger.error(f"健康巡檢執行異常: {hc_err}")
                            self.notifier.send(f"⚠️ [08:35 開盤前健檢異常] {hc_err}")
                    elif time.time() - last_standby_log >= 1800.0:
                        last_standby_log = time.time()
                        logger.info(f"🌙 [{now_str}] 開盤前待命守護中 (帳上零部位)，等待今日 08:45:00 日盤開盤...")
                    self._wake_event.clear()
                    self._wake_event.wait(timeout=5.0)
                    continue

                # 平時定期對帳
                if time.time() - last_reconcile >= self.reconcile_interval:
                    self._periodic_reconcile(strategy)
                    last_reconcile = time.time()

                # Item 5: 主動心跳保活 (每 15 分鐘向券商發送一次輕量保活 Ping，維持熱連線防逾時)
                if time.time() - last_heartbeat_time >= 900.0:
                    self.keep_alive_heartbeat()
                    last_heartbeat_time = time.time()

                # 定期更新策略狀態存檔
                if time.time() - last_state_save >= 30.0:
                    self._save_state(strategy)
                    last_state_save = time.time()

                kbars = self.fetch_today_day_kbars()
                cur_quote = float(kbars.iloc[-1]["close"]) if not kbars.empty else 0.0
                self._check_external_command(strategy, cur_price=cur_quote)
                if not kbars.empty and len(kbars) >= 2:
                    holiday_empty_since = 0.0
                    # 準備 VWAP 與 EMA9/EMA21 指標 (若 K 棒版號未變動則直接使用快取，省去每秒重算全表)
                    cur_ver = self.bar_aggregator.version
                    if cur_ver > 0 and cur_ver == _last_prepared_ver and not _cached_kbars.empty:
                        kbars = _cached_kbars
                    else:
                        kbars = strategy.prepare_indicators(kbars)
                        _last_prepared_ver = cur_ver
                        _cached_kbars = kbars

                    # 09:15 後計算並鎖定今日 ORB 上下界
                    if now_str >= strat_cfg.orb_end_time and not strategy.orb_calculated:
                        orb_h, orb_l = strategy.calculate_orb_levels(kbars)
                        if orb_h and orb_l:
                            logger.info(f"📌 今日現貨開盤區間 ({strat_cfg.orb_start_time}~{strat_cfg.orb_end_time}) 已鎖定: 上界={orb_h:.0f}, 下界={orb_l:.0f} (振幅={orb_h-orb_l:.0f}點)")
                            
                            # 檢查夜盤防撞牆狀態
                            wall_msg = ""
                            if getattr(strategy.config, "use_overnight_wall_filter", False):
                                thresh_pts = getattr(strategy.config, "overnight_wall_threshold_pts", 40.0)
                                if strategy.overnight_high is not None and strategy.orb_high < strategy.overnight_high and (strategy.overnight_high - strategy.orb_high) <= thresh_pts:
                                    wall_msg += f"\n• 🛡️ 啟動做多防撞牆: ORB上界距夜高僅 {strategy.overnight_high - strategy.orb_high:.0f} 點，需突破夜高 {strategy.overnight_high:.0f} 才開多！"
                                if strategy.overnight_low is not None and strategy.orb_low > strategy.overnight_low and (strategy.orb_low - strategy.overnight_low) <= thresh_pts:
                                    wall_msg += f"\n• 🛡️ 啟動做空防撞地: ORB下界距夜低僅 {strategy.orb_low - strategy.overnight_low:.0f} 點，需跌破夜低 {strategy.overnight_low:.0f} 才開空！"
                                if not wall_msg and strategy.overnight_high is not None and strategy.overnight_low is not None:
                                    wall_msg += f"\n• 🌙 夜盤參考水位: 夜高 {strategy.overnight_high:.0f} / 夜低 {strategy.overnight_low:.0f} (距離安全)"

                            self.notifier.send(
                                f"📌 [09:15 ORB 區間確立]\n"
                                f"現貨開盤 15 分鐘關鍵區間已鎖定！\n"
                                f"• 突破做多界線: {orb_h:.0f}\n"
                                f"• 跌破做空界線: {orb_l:.0f}\n"
                                f"• 今日開盤振幅: {orb_h - orb_l:.0f} 點{wall_msg}\n"
                                f"• 09:16 起啟動順勢突破監控！"
                            )

                    # 11:30 首次進場截止通知
                    if now_str >= "11:30:00" and not getattr(self, "_noon_cutoff_sent", False):
                        self._noon_cutoff_sent = True
                        pos_txt = "空手 (無部位)" if strategy.position == 0 else f"{'🟢多單' if strategy.position == 1 else '🔴空單'} (抱單守護中)"
                        self.notifier.send(
                            f"🛑 [11:30 首次進場截止]\n"
                            f"午盤關門點已過，今日不再開任何新單！\n"
                            f"• 當前持倉狀態: {pos_txt}\n"
                            f"• 今日累計交易: {strategy.trade_count} 次"
                        )

                    # (A) 逐根處理已收完的 K 棒 (換分 0.2~1 秒內立即確認突破進場)
                    completed_bars = kbars.iloc[:-1]
                    prev_bar = None
                    for ts, bar in completed_bars.iterrows():
                        if ts not in processed_timestamps:
                            processed_timestamps.add(ts)
                            prev_pos = strategy.position
                            res = strategy.execute_bar(ts, bar, prev_bar)
                            self._handle_action(strategy, res, ts, bar, prev_pos)
                        prev_bar = bar

                    # (B) 持倉期間：不等待 1 分 K 收完，對當前最新 Tick K 棒即時檢查停損、保本鎖利與反手 (SAR)
                    latest_ts = kbars.index[-1]
                    latest = kbars.iloc[-1]
                    if strategy.position != 0 and latest_ts not in processed_timestamps:
                        prev_pos = strategy.position
                        intrabar_res = strategy.execute_bar(latest_ts, latest, prev_bar)
                        if intrabar_res.get("action", "HOLD") != "HOLD":
                            processed_timestamps.add(latest_ts)
                            self._handle_action(strategy, intrabar_res, latest_ts, latest, prev_pos)

                    # (C) 每 3 秒印出最新即時狀態列
                    if time.time() - last_log_time >= 3.0:
                        last_log_time = time.time()
                        orb_info = f"ORB:{strategy.orb_low:.0f}~{strategy.orb_high:.0f}" if strategy.orb_calculated else "等待09:15鎖定ORB"
                        halt_info = " | 🛑停開新倉" if self.entries_halted else ""
                        if strategy.position == 0:
                            pos_str = f"無部位 (最高浮盈:{strategy.highest_profit:+.0f}點)"
                        else:
                            cur_pnl = (latest['close'] - strategy.entry_price) if strategy.position == 1 else (strategy.entry_price - latest['close'])
                            dir_str = "🟢多單" if strategy.position == 1 else "🔴空單"
                            pos_str = f"{dir_str}@{strategy.entry_price:.0f} (損益:{cur_pnl:+.0f}點 / 最高:{strategy.highest_profit:+.0f}點)"
                        logger.info(f"⏱️ [{now_str}] 報價:{latest['close']:.0f} | VWAP:{latest['vwap']:.0f} | {orb_info} | 狀態:{pos_str}{halt_info}")
                else:
                    if kbars.empty and now_str >= "09:05:00" and strategy.position == 0 and not self.pending_flatten:
                        if holiday_empty_since == 0.0:
                            holiday_empty_since = time.time()
                        elif time.time() - holiday_empty_since >= 30.0:
                            self._holiday_detected = True
                            logger.info(f"🏖️ [{now_str}] 今日 ({cur_date_str}) 已過 09:05 且連續 30 秒無任何日盤開盤行情，判定今日為國定假日或颱風休市！")
                            self.notifier.send(
                                f"🏖️ [休市自動偵測] {cur_date_str}\n"
                                f"已過 09:05 仍無任何日盤成交行情 (國定假日或颱風休市)，今日不執行任何交易。\n"
                                f"• 系統已安全登出永豐金 API，提前進入離線深度省電休眠，將於明日 08:30 自動喚醒。"
                            )
                            self.logout_and_sleep(reason=f"{cur_date_str} 國定假日/颱風休市提前休眠")
                            continue
                    else:
                        holiday_empty_since = 0.0

                    if time.time() - last_log_time >= 3.0:
                        last_log_time = time.time()
                        logger.info(f"⏱️ [{now_str}] 等待日盤 08:45 開盤 K 棒資料...")

                # 雙速睡眠與事件喚醒控制：
                # 1. 持倉期間若收到新 Tick 價位變動，_on_tick 會立即觸發 _wake_event (<5ms 喚醒檢查停損/鎖利)
                # 2. 接近整分收線 (59秒)，精準對齊至下一分鐘 :00.15 秒喚醒
                # 3. 收到 SIGINT / SIGTERM 時立即喚醒退出
                now_after = datetime.datetime.now()
                rem_sec = 60.0 - (now_after.second + now_after.microsecond / 1e6)
                wait_sec = max(0.05, rem_sec + 0.15) if rem_sec <= 1.2 else 1.0
                self._wake_event.clear()
                if not self._stop_requested:
                    self._wake_event.wait(timeout=wait_sec)

        except KeyboardInterrupt:
            self._stop_requested = True
        finally:
            logger.warning("⚠️ 收到停止指令，正在儲存狀態並安全停止監控...")
            self._save_state(strategy)
            real = self.get_real_position()
            if real:
                logger.warning(f"⚠️ 注意：目前帳上仍有 TMF 部位 {real:+d} 口，請確認是否需要手動平倉！")
            elif real is None and strategy.position != 0:
                logger.warning(f"⚠️ 注意：策略記錄仍有部位 ({'多單' if strategy.position==1 else '空單'})，請確認是否需要手動平倉！")
            self.close()

    def emergency_close_all(self):
        """緊急撤銷所有未成交掛單"""
        logger.warning("執行緊急撤單程序...")
        if self.api is not None:
            try:
                self.api.update_status(self.api.futopt_account)
                for trade in self.api.list_trades():
                    if _status_name(trade.status.status) in PENDING_STATUSES:
                        self.api.cancel_order(trade)
                logger.info("所有未成交掛單已全數撤銷。")
            except Exception as e:
                logger.error(f"撤單異常: {e}")
