"""
TMF (微型臺指期) 當沖交易設定與常數 (V2 Hexagonal Architecture)
"""
import json
import os
import sys
import warnings
from dataclasses import dataclass

sys.dont_write_bytecode = True
warnings.filterwarnings("ignore", message=".*api\\.Contracts is deprecated.*", category=DeprecationWarning)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(_BASE_DIR, "logs"), exist_ok=True)
os.environ.setdefault("SJ_LOG_PATH", os.path.join(_BASE_DIR, "logs", "shioaji.log"))


def load_env_file(env_path: str = None, override: bool = False):
    """自動載入 .env 設定檔 (預設僅填入尚未定義的環境變數)"""
    path = env_path or os.path.join(_BASE_DIR, ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if override or key not in os.environ:
                    os.environ[key] = val


_load_env_file = load_env_file
load_env_file()


def atomic_write_csv(df, target_path: str, **to_csv_kwargs) -> None:
    """以暫存檔搭配 os.replace 原子寫入 CSV，防止寫檔中斷導致原檔毀損"""
    abs_path = os.path.abspath(target_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{abs_path}.tmp.{os.getpid()}"
    try:
        df.to_csv(tmp_path, **to_csv_kwargs)
        os.replace(tmp_path, abs_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def atomic_write_json(target_path: str, data: dict) -> None:
    """以暫存檔搭配 os.replace 原子寫入 JSON"""
    abs_path = os.path.abspath(target_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{abs_path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, abs_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


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
    enable_early_super_breakout: bool = True # 是否啟用 09:01~09:15 早盤強勢爆發突破進場
    early_super_breakout_pts: float = 85.0   # 09:01~09:15 強勢爆發突破門檻
    early_breakout_max_range_ratio: float = 1.25 # 09:15 鎖定時允許之 ORB 空間倍率上限
    orb_start_time: str = "09:00:00"         # 現貨開盤區間開始時間
    orb_end_time: str = "09:15:00"           # 現貨開盤區間結束時間
    entry_start_time: str = "09:16:00"       # 允許突破進場開始時間
    entry_end_time: str = "11:30:00"         # 停止首次進場時間
    sar_end_time: str = "11:00:00"           # 允許假突破反手 (Stop & Reverse) 截止時間
    force_close_time: str = "13:08:00"       # 第一道：當沖策略強制平倉時間
    failsafe_close_time: str = "13:09:00"    # 第二道：安全底線強制補平時間
    shutdown_time: str = "13:10:00"          # 第三道：收盤確認零部位並安全退出時間
    stop_loss_pts: float = 90.0              # 初始停損點數
    breakeven_trigger_pts: float = 80.0      # 一階保本鎖利門檻
    breakeven_lock_pts: float = 30.0         # 一階保本鎖定點數
    tier1_5_trigger_pts: float = 160.0       # 中階(1.5階)鎖利門檻
    tier1_5_lock_pts: float = 100.0          # 中階(1.5階)鎖定點數
    tier2_trigger_pts: float = 200.0         # 二階波段鎖利門檻
    tier2_lock_pts: float = 120.0            # 二階波段鎖定點數
    trailing_activation_pts: float = 280.0   # 三階大波段移動停利門檻
    trailing_distance_pts: float = 60.0      # 三階大波段移動停利回撤距離
    max_breakout_chase_pts: float = 60.0     # 突破後等待均線共振允許的最大追價距離
    max_orb_range_pts: float = 320.0         # 09:00~09:15 ORB 區間最大允許寬度
    use_vwap_ema_filter: bool = True         # 啟用 VWAP + EMA9/EMA21 順勢濾網
    use_overnight_wall_filter: bool = True   # 啟用夜盤高低點防撞牆保護濾網
    overnight_wall_threshold_pts: float = 40.0 # 防撞牆保護門檻
    enable_sar: bool = True                  # 啟用早盤假突破停損反手 (Stop & Reverse)
    max_initial_trades: int = 1              # 每日首次突破進場上限

    # ATR 動態尺度調整
    use_atr_scaling: bool = True
    atr_period: int = 20
    atr_ref_period: int = 60
    atr_min_days: int = 20
    atr_reference: float = 880.0
    atr_scale_min: float = 0.6
    atr_scale_max: float = 1.8
