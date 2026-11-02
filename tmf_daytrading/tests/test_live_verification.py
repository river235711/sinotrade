"""
成交確認與對帳邏輯測試 (不需要 API Key，使用假的券商 API)
執行: python3 tests/test_live_verification.py
"""
import os
import sys
import tempfile
import warnings
from types import SimpleNamespace

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shioaji as sj
from config import ORBStrategyConfig, InstrumentConfig
from strategies.orb_strategy import ORBStrategy
from live.shioaji_trader import ShioajiTMFTrader

Status = sj.constant.Status


class FakeAPI:
    """模擬券商：mode 佇列決定每張單的結果 (fill / cancel / partial / stuck)"""

    def __init__(self, modes, start_net=0):
        self.modes = list(modes)
        self.net = start_net
        self.futopt_account = object()
        self.sent = []

    def Order(self, **kw):
        return SimpleNamespace(**kw)

    def place_order(self, contract, order):
        mode = self.modes.pop(0) if self.modes else "fill"
        qty = order.quantity
        sign = 1 if order.action == sj.constant.Action.Buy else -1
        self.sent.append((order.action, qty, mode))
        status = SimpleNamespace(status=Status.Submitted, deals=[], deal_quantity=0)
        trade = SimpleNamespace(status=status, _mode=mode, _qty=qty, _sign=sign)
        if mode == "fill":
            self.net += sign * qty
            status.status = Status.Filled
            status.deals = [SimpleNamespace(quantity=qty, price=45000.0 + 5)]
        elif mode == "cancel":
            status.status = Status.Cancelled
        elif mode == "partial":
            part = max(1, qty // 2)
            self.net += sign * part
            status.status = Status.Cancelled
            status.deals = [SimpleNamespace(quantity=part, price=45000.0)]
        # stuck: 維持 Submitted，直到被撤單
        return trade

    def update_status(self, account):
        pass

    def cancel_order(self, trade):
        trade.status.status = Status.Cancelled

    def list_positions(self, account):
        if self.net == 0:
            return []
        d = sj.constant.Action.Buy if self.net > 0 else sj.constant.Action.Sell
        return [SimpleNamespace(code="TMFK6", direction=d, quantity=abs(self.net), price=45000.0)]


def make_trader(modes, start_net=0):
    t = ShioajiTMFTrader(simulation=False, quantity=1)
    t.api = FakeAPI(modes, start_net)
    t.contract = SimpleNamespace(code="TMFR1", symbol="TMFR1", name="fake")
    t.fill_timeout = 0.3
    t.fill_poll_interval = 0.05
    t.retry_pause = 0.0
    t.log_dir = tempfile.mkdtemp()
    return t


def make_strategy(position=0, entry=45000.0, trade_count=1):
    s = ORBStrategy(ORBStrategyConfig(), InstrumentConfig())
    s.position = position
    s.entry_price = entry
    s.trade_count = trade_count
    return s


BAR = pd.Series({"close": 45000.0})
TS = pd.Timestamp("2026-11-02 09:20:00")


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        check.failed += 1


check.failed = 0


# 1. 開倉單全部成交 -> 對帳通過，進場價改用實際成交價
t = make_trader(["fill"])
s = make_strategy(position=1, entry=45000.0)
t._handle_action(s, {"action": "BUY", "detail": "t"}, TS, BAR, 0)
check("開倉成交: 策略維持多單", s.position == 1)
check("開倉成交: 進場價改為實際成交價 45005", s.entry_price == 45005.0)
check("開倉成交: 未停開新倉", not t.entries_halted)
check("開倉成交: 稽核檔已寫入", any(f.startswith("orders_") for f in os.listdir(t.log_dir)))

# 2. 開倉單被取消 (IOC 沒成交) -> 策略退回空手、交易次數退回
t = make_trader(["cancel"])
s = make_strategy(position=1, trade_count=1)
t._handle_action(s, {"action": "BUY", "detail": "t"}, TS, BAR, 0)
check("開倉未成交: 策略退回空手", s.position == 0)
check("開倉未成交: 交易次數退回 0", s.trade_count == 0)
check("開倉未成交: 失敗次數 +1", t.order_failures == 1)
check("開倉未成交: 還不到停開新倉", not t.entries_halted)

# 3. 連續 3 次開倉沒成交 -> 停止開新倉
t = make_trader(["cancel", "cancel", "cancel"])
for _ in range(3):
    s = make_strategy(position=1, trade_count=1)
    t._handle_action(s, {"action": "SELL", "detail": "t"}, TS, BAR, 0)
check("連續 3 次未成交: 停止開新倉", t.entries_halted)

# 4. 平倉單被取消 -> 自動依帳上部位補平
t = make_trader(["cancel", "fill"], start_net=1)
s = make_strategy(position=0)
rec = {"reason": "停損", "exit_price": 44910.0, "pnl_pts": -90.0}
t._handle_action(s, {"action": "EXIT", "record": rec}, TS, BAR, 1)
check("平倉首次未成交: 重試後帳上已平", t.api.net == 0)
check("平倉首次未成交: 策略為空手", s.position == 0)
check("平倉首次未成交: 沒有停開新倉", not t.entries_halted)

# 5. 平倉一直失敗 -> 停開新倉，策略改回帳上真實方向
t = make_trader(["cancel"] * 10, start_net=1)
s = make_strategy(position=0)
t._handle_action(s, {"action": "EXIT", "record": rec}, TS, BAR, 1)
check("平倉全失敗: 停止開新倉", t.entries_halted)
check("平倉全失敗: 策略改回多單以便後續再平倉", s.position == 1)

# 6. 卡單 (一直 Submitted) -> 逾時撤單，判定未成交
t = make_trader(["stuck"])
r = t._send_and_verify("BUY", 1)
check("卡單: 逾時後撤單", r.status == "Cancelled" and not r.ok)

# 7. 部分成交 (SAR 要 2 口只成交 1 口) -> 平掉殘留、停開新倉
t = make_trader(["partial", "fill"], start_net=-1)   # 原本空單 1 口，SAR 反手買 2 口只成交 1 口
s = make_strategy(position=1)
rec2 = {"reason": "停損", "exit_price": 45090.0, "pnl_pts": -90.0}
t._handle_action(s, {"action": "SAR_BUY", "record": rec2}, TS, BAR, -1)
# 預期反手後 +1，實際 -1+1=0 -> real==0 視為開倉未成交
check("SAR 部分成交: 策略與帳上一致(空手)", s.position == 0 and t.api.net == 0)

# 8. 已停開新倉時，開倉訊號被忽略且策略退回
t = make_trader([])
t.entries_halted = True
s = make_strategy(position=1, trade_count=1)
t._handle_action(s, {"action": "BUY", "detail": "t"}, TS, BAR, 0)
check("停開新倉: 不送單", len(t.api.sent) == 0)
check("停開新倉: 策略退回空手", s.position == 0)

# 9. 定期對帳: 使用者手動平倉 -> 策略同步為空手並停開新倉
t = make_trader([], start_net=0)
s = make_strategy(position=1)
t._periodic_reconcile(s)
check("手動平倉偵測: 策略同步為空手", s.position == 0)
check("手動平倉偵測: 停開新倉等待人工確認", t.entries_halted)

# 10. 收盤強制平倉以帳上部位為準 (策略以為空手，但帳上有單)
t = make_trader(["fill"], start_net=-1)
s = make_strategy(position=0)
t._force_flatten_all(s)
check("13:26 強制平倉: 帳上空單被買回", t.api.net == 0)

# 11. 帳上部位解析 (多/空/無)
check("帳上部位: 多單 +1", make_trader([], 1).get_real_position() == 1)
check("帳上部位: 空單 -2", make_trader([], -2).get_real_position() == -2)
check("帳上部位: 無部位 0", make_trader([], 0).get_real_position() == 0)

# 12. 乾跑模式 (沒有 API) 不會出錯
t = ShioajiTMFTrader(simulation=True)
t.log_dir = tempfile.mkdtemp()
s = make_strategy(position=1)
t._handle_action(s, {"action": "BUY", "detail": "t"}, TS, BAR, 0)
check("乾跑模式: 策略維持多單", s.position == 1 and not t.entries_halted)

# 13. 重啟時接管帳上多單部位 (Adopt existing long position on startup)
t = make_trader([], start_net=1)
s = make_strategy(position=0, entry=0.0, trade_count=0)
t._reconcile_and_recover_on_startup(s, pd.DataFrame())
check("重啟接管多單: 策略設為多單", s.position == 1)
check("重啟接管多單: 策略進場價同步為帳上成本 45000", s.entry_price == 45000.0)
check("重啟接管多單: 未停開新倉", not t.entries_halted)
check("重啟接管多單: 狀態檔已自動建立", t._load_state() is not None and t._load_state()["position"] == 1)

# 14. 重啟時接管帳上空單部位 (Adopt existing short position on startup)
t = make_trader([], start_net=-1)
s = make_strategy(position=0, entry=0.0)
t._reconcile_and_recover_on_startup(s, pd.DataFrame())
check("重啟接管空單: 策略設為空單", s.position == -1)
check("重啟接管空單: 策略進場價同步為帳上成本 45000", s.entry_price == 45000.0)

# 15. 本地狀態恢復 (從 logs/state_YYYYMMDD.json 讀取 highest_profit 與 trade_count)
t = make_trader([], start_net=1)
# 先存入上一輪的狀態 (最高浮盈 85 點，交易次數 1)
s_prev = make_strategy(position=1, entry=45000.0, trade_count=1)
s_prev.highest_profit = 85.0
t._save_state(s_prev)

# 模擬程式重開機，以全新空的 strategy 實例接管
s_new = make_strategy(position=0, entry=0.0, trade_count=0)
t._reconcile_and_recover_on_startup(s_new, pd.DataFrame())
check("狀態恢復: 最高浮盈成功恢復為 85點", s_new.highest_profit == 85.0)
check("狀態恢復: 交易次數成功恢復為 1", s_new.trade_count == 1)
check("狀態恢復: 持續維持多單部位", s_new.position == 1)

# 16. 本地 state 曾有持倉，但券商帳上已被平倉 (例如手機 App 手動平倉) -> 策略拉回空手
t = make_trader([], start_net=0)
s_prev = make_strategy(position=1, entry=45000.0, trade_count=1)
t._save_state(s_prev)

s_restart = make_strategy(position=0, entry=0.0)
t._reconcile_and_recover_on_startup(s_restart, pd.DataFrame())
check("外部手動平倉: 策略拉回空手 (0)", s_restart.position == 0)
check("外部手動平倉: 當日交易次數仍維持 1 次", s_restart.trade_count == 1)

# 17. 重啟時歷史 K 棒標記已處理 (避免重啟後重複下單)
t = make_trader([], start_net=1)
dates = pd.date_range("2026-11-02 08:45", periods=5, freq="1min")
df_dummy = pd.DataFrame({"close": [45000]*5, "high": [45010]*5, "low": [44990]*5}, index=dates)
processed = t._reconcile_and_recover_on_startup(s_new, df_dummy)
check("歷史 K 棒過濾: 收盤 K 棒已加入 processed_timestamps", len(processed) == 4)

# 18. 重啟接管後執行收盤強制平倉 (保證當日絕不留倉)
t = make_trader(["fill"], start_net=1)
s = make_strategy(position=0)
t._reconcile_and_recover_on_startup(s, pd.DataFrame())
t._force_flatten_all(s)
check("接管後強制平倉: 帳上多單已成功賣出平倉", t.api.net == 0)
check("接管後強制平倉: 策略維持空手", s.position == 0)

# 19. 20日 ATR 動態波動自適應縮放驗證
from strategies.atr_scaler import compute_atr, scale_strategy_config, load_daily_history
base_cfg = ORBStrategyConfig()
cfg_high, info_high = scale_strategy_config(base_cfg, base_cfg.atr_reference * 1.5)
check("ATR 1.5倍高波動: 停損自動放大為 135點", cfg_high.stop_loss_pts == 135.0 and info_high["applied"])
check("ATR 1.5倍高波動: 二階鎖利門檻放大為 300點", cfg_high.tier2_trigger_pts == 300.0)
cfg_low, info_low = scale_strategy_config(base_cfg, base_cfg.atr_reference * 0.5)
check("ATR 最小倍數保護 (0.6x): 停損不低於 54點", cfg_low.stop_loss_pts == 54.0)

data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
daily_hist = load_daily_history(data_dir)
atr_val, n_days = compute_atr(daily_hist, "2026-11-03", period=20, min_days=20)
check("永久日 K 歷史檔 (daily_bars.csv) 已累積滿 20 日可計算 ATR", atr_val is not None and n_days == 20)

print()
print("全部通過" if check.failed == 0 else f"有 {check.failed} 項失敗")
sys.exit(1 if check.failed else 0)
