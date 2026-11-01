"""
永豐金證券 Shioaji 實盤 / 模擬交易執行器 (TMF 微型臺指期當沖專用)
支援：
1. --mode paper  (模擬測試環境 simulation=True，不下真單，驗證即時報價與訊號)
2. --mode live   (正式實盤環境 simulation=False，掛載憑證自動送單)
"""
import time
import datetime
import logging
import pandas as pd
from typing import Optional
from config import InstrumentConfig, ORBStrategyConfig, ShioajiConfig
from strategies.orb_strategy import ORBStrategy

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ShioajiTrader")

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
        
        # 風控狀態
        self.is_kill_switch_triggered = False

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

            return True
        except Exception as e:
            logger.error(f"Shioaji 初始化失敗: {e}")
            return False

    def load_tmf_contract(self, code: str = "202611"):
        """獲取 TMF 微型臺指期近月合約"""
        if self.api is None:
            logger.error("API 尚未登入")
            return None
        try:
            contract_name = f"TMF{code}"
            if hasattr(self.api.Contracts.Futures.TMF, contract_name):
                self.contract = getattr(self.api.Contracts.Futures.TMF, contract_name)
            else:
                # 自動綁定近月連續合約 TMFR1
                self.contract = self.api.Contracts.Futures.TMF.TMFR1
            logger.info(f"已綁定交易合約: {self.contract.code} ({self.contract.symbol}) - {self.contract.name}")
            return self.contract
        except Exception as e:
            logger.error(f"獲取合約失敗: {e}")
            return None

    def place_order(self, action: str, price: float, quantity: int = 1, price_type: str = "MKP", octype: str = "Auto"):
        """
        送出期貨委託單
        :param action: 'BUY' 或 'SELL'
        :param price: 限價價格 (若為 MKP 範圍市價則填 0 或當前價)
        :param quantity: 下單口數
        :param price_type: 'MKP' 範圍市價 (當沖突破建議使用，確保成交且防滑價), 'LMT' 限價
        :param octype: 'Auto' 自動, 'New' 新倉, 'Cover' 平倉, 'DayTrade' 當沖
        """
        if self.is_kill_switch_triggered:
            logger.warning("已觸發當日風控熔斷 (Kill Switch)，拒絕新增任何委託！")
            return None

        if self.api is None or self.contract is None:
            logger.info(f"[乾跑模擬] 送出委託: {action} {quantity}口 TMF @ {price} ({price_type})")
            return {"status": "Simulated", "action": action, "price": price, "qty": quantity}

        import shioaji as sj
        sj_action = sj.Action.Buy if action == "BUY" else sj.Action.Sell
        sj_price_type = (sj.FuturesPriceType.MKP if price_type == "MKP" 
                         else sj.FuturesPriceType.LMT)
        sj_octype = getattr(sj.FuturesOCType, octype, sj.FuturesOCType.Auto)

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
        logger.info(f"✅ 委託單已送出: [{action}] {quantity}口 ({price_type}/IOC) -> {trade.status.status}")
        return trade

    def fetch_today_day_kbars(self) -> pd.DataFrame:
        """從 Shioaji 抓取今日日盤 (08:45 ~ 13:45) 1分K 資料"""
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        kbars = self.api.kbars(self.contract, start=today_str, end=today_str)
        df = pd.DataFrame({**kbars})
        if df.empty:
            return pd.DataFrame()

        df['ts'] = pd.to_datetime(df['ts'])
        df = df.set_index('ts')
        df.columns = [c.lower() for c in df.columns]
        # 篩選今日日盤 08:45 ~ 13:45
        day_df = df[df.index.strftime("%Y-%m-%d") == today_str].between_time("08:45", "13:45")
        return day_df[['open', 'high', 'low', 'close', 'volume']].copy()

    def run_intraday_bot(self, strat_cfg: ORBStrategyConfig = None):
        """
        啟動日盤全自動監控與下單迴圈 (08:45 ~ 13:32)
        每分鐘自動更新 1分K、計算 ORB 高低點與 VWAP/EMA 指標，觸發訊號時自動下單
        """
        strat_cfg = strat_cfg or ORBStrategyConfig()
        strategy = ORBStrategy(config=strat_cfg, inst_config=InstrumentConfig())
        processed_timestamps = set()

        logger.info("=" * 75)
        logger.info("🤖 TMF 微台指 ORB Pro 自動當沖機器人已啟動！")
        logger.info(f"• 下單口數: {self.quantity} 口 | 開盤區間: {strat_cfg.orb_start_time} ~ {strat_cfg.orb_end_time}")
        logger.info(f"• 風控設定: 停損 -{strat_cfg.stop_loss_pts:.0f}點 | 一階鎖利 +{strat_cfg.breakeven_trigger_pts:.0f}->+{strat_cfg.breakeven_lock_pts:.0f}點 | 二階鎖利 +{strat_cfg.tier2_trigger_pts:.0f}->+{strat_cfg.tier2_lock_pts:.0f}點 | {strat_cfg.force_close_time[:5]} 強制安全平倉")
        logger.info("• 按下 Ctrl+C 可隨時安全停止程式。")
        logger.info("=" * 75)

        try:
            while True:
                now = datetime.datetime.now()
                now_str = now.strftime("%H:%M:%S")

                # 保險起見：若超過 13:26:00 帳上仍有部位，不等待 K 棒直接強制平倉
                if now_str >= "13:26:00" and strategy.position != 0:
                    close_action = "SELL" if strategy.position == 1 else "BUY"
                    logger.warning(f"⏰ 已達 {now_str} 收盤前安全底線，強制市價平倉剩餘部位！")
                    self.place_order(close_action, 0, quantity=self.quantity, price_type="MKP", octype="Cover")
                    strategy.position = 0

                if now_str > "13:28:00":
                    logger.info("🏁 已完成收盤前安全平倉檢查，今日自動交易結束。")
                    break

                kbars = self.fetch_today_day_kbars()
                if not kbars.empty and len(kbars) >= 2:
                    # 準備 VWAP 與 EMA9/EMA21 指標
                    kbars = strategy.prepare_indicators(kbars)

                    # 09:15 後計算並鎖定今日 ORB 上下界
                    if now_str >= strat_cfg.orb_end_time and not strategy.orb_calculated:
                        orb_h, orb_l = strategy.calculate_orb_levels(kbars)
                        if orb_h and orb_l:
                            logger.info(f"📌 今日現貨開盤區間 ({strat_cfg.orb_start_time}~{strat_cfg.orb_end_time}) 已鎖定: 上界={orb_h:.0f}, 下界={orb_l:.0f} (振幅={orb_h-orb_l:.0f}點)")

                    # 逐根處理已收完的 K 棒 (排除當前正在跳動的最後一根未收盤 K 棒)
                    completed_bars = kbars.iloc[:-1]
                    prev_bar = None
                    for ts, bar in completed_bars.iterrows():
                        if ts not in processed_timestamps:
                            processed_timestamps.add(ts)
                            prev_pos = strategy.position
                            res = strategy.execute_bar(ts, bar, prev_bar)
                            act = res.get("action", "HOLD")

                            # 根據策略動作執行實際下單
                            if act == "BUY":
                                logger.info(f"🟢 [訊號觸發] {ts.strftime('%H:%M')} 做多進場 @ {bar['close']:.0f} ({res.get('detail')})")
                                self.place_order("BUY", bar['close'], quantity=self.quantity, price_type="MKP")
                            elif act == "SELL":
                                logger.info(f"🔴 [訊號觸發] {ts.strftime('%H:%M')} 做空進場 @ {bar['close']:.0f} ({res.get('detail')})")
                                self.place_order("SELL", bar['close'], quantity=self.quantity, price_type="MKP")
                            elif act == "EXIT":
                                rec = res.get("record", {})
                                close_action = "SELL" if prev_pos == 1 else "BUY"
                                logger.info(f"🏁 [平倉回補] {ts.strftime('%H:%M')} {rec.get('reason')} @ {rec.get('exit_price'):.0f} (損益: {rec.get('pnl_pts'):+.0f}點)")
                                self.place_order(close_action, rec.get('exit_price', 0), quantity=self.quantity, price_type="MKP", octype="Cover")
                            elif act == "SAR_SELL":
                                rec = res.get("record", {})
                                logger.info(f"🔄 [假突破反手做空 SAR] {ts.strftime('%H:%M')} 多單停損並反手做空 @ {rec.get('exit_price'):.0f}")
                                self.place_order("SELL", rec.get('exit_price', 0), quantity=self.quantity * 2, price_type="MKP")
                            elif act == "SAR_BUY":
                                rec = res.get("record", {})
                                logger.info(f"🔄 [假跌破反手做多 SAR] {ts.strftime('%H:%M')} 空單停損並反手做多 @ {rec.get('exit_price'):.0f}")
                                self.place_order("BUY", rec.get('exit_price', 0), quantity=self.quantity * 2, price_type="MKP")

                        prev_bar = bar

                    # 印出最新即時狀態列
                    latest = kbars.iloc[-1]
                    pos_str = "無部位" if strategy.position == 0 else ("🟢多單持倉" if strategy.position == 1 else "🔴空單持倉")
                    orb_info = f"ORB:{strategy.orb_low:.0f}~{strategy.orb_high:.0f}" if strategy.orb_calculated else "等待09:15鎖定ORB"
                    logger.info(f"⏱️ [{now_str}] 報價:{latest['close']:.0f} | VWAP:{latest['vwap']:.0f} | {orb_info} | 狀態:{pos_str} (最高浮盈:{strategy.highest_profit:+.0f}點)")
                else:
                    logger.info(f"⏱️ [{now_str}] 等待日盤 08:45 開盤 K 棒資料...")

                time.sleep(10)

        except KeyboardInterrupt:
            logger.warning("⚠️ 收到使用者中斷指令 (Ctrl+C)，停止自動監控。")
            if strategy.position != 0:
                logger.warning(f"⚠️ 注意：目前帳上仍有部位 ({'多單' if strategy.position==1 else '空單'})，請確認是否需要手動平倉！")

    def emergency_close_all(self):
        """緊急撤銷所有未成交掛單"""
        logger.warning("執行緊急撤單程序...")
        if self.api is not None:
            try:
                self.api.update_status(self.api.futopt_account)
                for trade in self.api.list_trades():
                    if trade.status.status in ["Submitted", "PartFilled", "PendingSubmit"]:
                        self.api.cancel_order(trade)
                logger.info("所有未成交掛單已全數撤銷。")
            except Exception as e:
                logger.error(f"撤單異常: {e}")
