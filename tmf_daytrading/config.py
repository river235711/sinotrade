"""
TMF (微型臺指期) 當沖交易設定與常數 (Pro 翻修獲利版)
"""
import os
from dataclasses import dataclass

def _load_env_file():
    """自動載入同目錄下的 .env 設定檔"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = val

_load_env_file()

@dataclass
class ShioajiConfig:
    api_key: str = os.environ.get("SHIOAJI_API_KEY", "")
    secret_key: str = os.environ.get("SHIOAJI_SECRET_KEY", "")
    ca_path: str = os.environ.get("SHIOAJI_CA_PATH", "")
    ca_passwd: str = os.environ.get("SHIOAJI_CA_PASSWD", "")
    person_id: str = os.environ.get("SHIOAJI_PERSON_ID", "")

@dataclass
class InstrumentConfig:
    symbol: str = "TMF"                      # 微型臺指期代碼
    name: str = "微型臺指期貨"
    multiplier: float = 10.0                 # 1點 = 新台幣 10 元
    tick_size: float = 1.0                   # 最小跳動點數 = 1 點
    commission_per_order: float = 12.0       # 單邊手續費估計 (新台幣 12 元/口)
    tax_rate: float = 0.00001                # 期貨交易稅率 (十萬分之一)
    slippage_points: float = 1.0             # 估計單邊滑價 (1 點)

@dataclass
class ORBStrategyConfig:
    pre_orb_start_time: str = "08:45:00"     # 期貨早盤區間開始時間
    pre_orb_end_time: str = "09:00:00"       # 期貨早盤區間結束時間
    early_super_breakout_pts: float = 85.0   # 09:01~09:15 強勢爆發突破門檻 (超越早盤高低點達 85 點立即順勢進場，不漏掉早盤急殺/急拉行情)
    orb_start_time: str = "09:00:00"         # 現貨開盤區間開始時間 (09:00 台股現貨開盤)
    orb_end_time: str = "09:15:00"           # 現貨開盤區間結束時間 (09:00~09:15 關鍵區間)
    entry_start_time: str = "09:16:00"       # 允許突破進場開始時間
    entry_end_time: str = "11:30:00"         # 停止首次進場時間
    sar_end_time: str = "11:00:00"           # 允許假突破反手 (Stop & Reverse) 截止時間
    force_close_time: str = "13:20:00"       # 第一道：當沖策略強制平倉時間 (提前至 13:20 避開現貨 13:25 集合競價、尾盤甩尾與週三結算風險)
    failsafe_close_time: str = "13:22:00"   # 第二道：安全底線強制補平時間 (若帳上仍有殘留部位，直接 MKP IOC 強制補平)
    shutdown_time: str = "13:24:00"         # 第三道：收盤確認零部位並安全退出時間 (在 13:24:00 前完全結束今日交易)
    stop_loss_pts: float = 90.0              # 初始停損點數 (90 點，適應高基期波動)
    breakeven_trigger_pts: float = 80.0      # 一階保本鎖利門檻 (獲利達 80 點啟動)
    breakeven_lock_pts: float = 30.0         # 一階保本鎖定點數 (拉回時至少鎖住 +30 點)
    tier1_5_trigger_pts: float = 160.0       # 中階(1.5階)鎖利門檻 (獲利達 160 點啟動，補齊 80~200 點真空區且不卡死主升段洗盤)
    tier1_5_lock_pts: float = 100.0          # 中階(1.5階)鎖定點數 (拉回時鎖住 +100 點)
    tier2_trigger_pts: float = 200.0         # 二階波段鎖利門檻 (獲利達 200 點啟動)
    tier2_lock_pts: float = 120.0            # 二階波段鎖定點數 (拉回時鎖住 +120 點)
    trailing_activation_pts: float = 340.0   # 三階大波段移動停利門檻 (+340 點啟動，否則抱到收盤)
    trailing_distance_pts: float = 60.0      # 三階大波段移動停利回撤距離 (回撤 60 點出場)
    max_breakout_chase_pts: float = 60.0     # 突破後等待 VWAP/EMA 均線共振允許的最大追價距離 (60 點，防漏單同時不追高)
    max_orb_range_pts: float = 320.0         # 09:00~09:15 ORB 區間最大允許寬度 (超過 320 點代表早盤空間耗盡，09:16 後不追突破)
    use_vwap_ema_filter: bool = True         # 啟用 VWAP + EMA9/EMA21 順勢濾網
    enable_sar: bool = True                  # 啟用早盤假突破停損反手 (Stop & Reverse)
    max_initial_trades: int = 1              # 每日首次突破進場上限 (1 次，若假突破可反手 1 次)

    # ATR 動態尺度調整 (依前 N 日日盤平均真實振幅，自動放大/縮小上方所有「點數」參數)
    # 今日參數 = 上方固定點數 × (今日 ATR / atr_reference)，比例限制在 atr_scale_min ~ atr_scale_max
    use_atr_scaling: bool = True             # 啟用 ATR 動態縮放 (False = 完全使用上方固定點數)
    atr_period: int = 20                     # ATR 計算天數 (前 20 個交易日)
    atr_min_days: int = 20                   # 需滿 20 個交易日歷史日 K 才啟動動態縮放，避免短期樣本失真
    atr_reference: float = 880.0             # 基準 ATR：今日 ATR 等於此值時，參數 = 上方固定點數 (對應 45,000~50,000 點區間 20 日均 ATR ~880 點)
    atr_scale_min: float = 0.6               # 最小縮放倍數 (避免極低波動時停損過窄被雜訊掃出)
    atr_scale_max: float = 1.8               # 最大縮放倍數 (避免極端行情時單筆風險過大)
