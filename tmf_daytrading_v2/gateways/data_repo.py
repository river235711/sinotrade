"""
資料倉儲與外部行情備援配接層 (Data Repository & Persistence Adapter)
負責所有磁碟 CSV/JSON 原子讀寫、稽核寫入、日 K 歷史保存與 CNYES 公開行情備援抓取。
"""
import csv
import dataclasses
import datetime
import json
import logging
import os
import ssl
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple

import pandas as pd

from config import atomic_write_csv, atomic_write_json
from core.atr_scaler import build_daily_bars, merge_daily_history, scale_config_from_daily
from core.models import TradeRecord, TradingDayState
from gateways.base import FillResult

logger = logging.getLogger("TMFTrader.DataRepo")

DAILY_HISTORY_FILE = "daily_bars.csv"
ROLLING_KBARS_FILE = "tmf_20days_1m_kbars.csv"
CNYES_TXF_URL = "https://ws.api.cnyes.com/ws/api/v1/charting/history?symbol=TWF:TXF:FUTURES&resolution=1&quote=1"

_AUDIT_HEADER_CACHE: Dict[str, Tuple[bool, int]] = {}


def get_ssl_context() -> ssl.SSLContext:
    """建立跨平台相容 SSL 上下文"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()


def load_daily_history(data_dir: str) -> pd.DataFrame:
    """讀取永久日 K 歷史，並與滾動 20 日 1分K 資料合併 (同日期以 1分K 合成結果為準)"""
    hist_df = None
    roll_df = None
    hist_path = os.path.join(data_dir, DAILY_HISTORY_FILE)
    if os.path.exists(hist_path):
        hist_df = pd.read_csv(hist_path, index_col="date")
    roll_path = os.path.join(data_dir, ROLLING_KBARS_FILE)
    if os.path.exists(roll_path):
        roll_df = build_daily_bars(pd.read_csv(roll_path))
    return merge_daily_history(hist_df, roll_df)


def save_daily_history(data_dir: str, daily: pd.DataFrame) -> None:
    """原子覆寫永久日 K 歷史檔"""
    path = os.path.join(data_dir, DAILY_HISTORY_FILE)
    out = daily[["open", "high", "low", "close"]].sort_index().round(1)
    out.index.name = "date"
    atomic_write_csv(out, path)


def update_daily_history(data_dir: str, df_1m_new: pd.DataFrame) -> pd.DataFrame:
    """把新的 1分K 合成日 K 併入永久歷史"""
    daily = merge_daily_history(load_daily_history(data_dir), build_daily_bars(df_1m_new))
    save_daily_history(data_dir, daily)
    return daily


def config_for_date(
    base_cfg, data_dir: str, target_date: str, daily: Optional[pd.DataFrame] = None
) -> Tuple[object, Dict]:
    """讀取日 K 歷史並計算 target_date 的 ATR 縮放設定"""
    if daily is None:
        daily = load_daily_history(data_dir)
    return scale_config_from_daily(base_cfg, daily, target_date)


def fetch_cnyes_history(timeout: int = 5) -> pd.DataFrame:
    """從 CNYES 公開行情介面抓取近月大台指期 (TWF:TXF:FUTURES) 1分K 備援資料"""
    req = urllib.request.Request(CNYES_TXF_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
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


def fetch_cnyes_day_kbars(today_str: str, timeout: int = 5) -> pd.DataFrame:
    """取得指定交易日日盤 (08:45~13:44 Start Timestamp) 之 CNYES 備援 1分K"""
    df = fetch_cnyes_history(timeout=timeout)
    if df.empty:
        return pd.DataFrame()
    df = df.set_index("ts").sort_index()
    day_df = df[df.index.strftime("%Y-%m-%d") == today_str].between_time("08:45", "13:44")
    if day_df.empty:
        return pd.DataFrame()
    return day_df[["open", "high", "low", "close", "volume"]].copy()


def is_overnight_row_complete(
    row: Mapping[str, Any],
    target_date_str: str,
    min_bars: int = 800,
    min_end_hhmm: datetime.time = datetime.time(4, 55, 0),
) -> bool:
    """檢查 overnight_levels_summary.csv 單日紀錄是否包含完整夜盤"""
    raw_bars = row.get("night_bars")
    if raw_bars is not None and pd.notna(raw_bars):
        try:
            if int(raw_bars) < min_bars:
                return False
        except (TypeError, ValueError):
            return False

    raw_end = row.get("night_end")
    if raw_end is not None and pd.notna(raw_end):
        try:
            end_dt = pd.to_datetime(raw_end).to_pydatetime()
            target_d = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
            days_gap = (target_d - end_dt.date()).days
            if days_gap < 0 or days_gap > 14:
                return False
            if not (min_end_hhmm <= end_dt.time() <= datetime.time(6, 0, 0)):
                return False
            raw_start = row.get("night_start")
            if raw_start is not None and pd.notna(raw_start):
                start_dt = pd.to_datetime(raw_start).to_pydatetime()
                min_session_end = datetime.datetime.combine(
                    start_dt.date() + datetime.timedelta(days=1),
                    min_end_hhmm,
                )
                if end_dt < min_session_end:
                    return False
        except Exception:
            return False

    return True


def extract_overnight_bars(df_kbars: pd.DataFrame, cur_date: datetime.date) -> pd.DataFrame:
    """萃取對應 cur_date 的前一夜盤 (15:00~05:00) K 棒"""
    if df_kbars is None or df_kbars.empty:
        return pd.DataFrame()
    df_k = df_kbars.copy()
    if "ts" not in df_k.columns:
        df_k["ts"] = pd.to_datetime(df_k.index)
    else:
        df_k["ts"] = pd.to_datetime(df_k["ts"])
    df_k = df_k.reset_index(drop=True).sort_values("ts").reset_index(drop=True)

    end_night = datetime.datetime.combine(cur_date, datetime.time(5, 1, 0))
    start_night = datetime.datetime.combine(cur_date - datetime.timedelta(days=4), datetime.time(15, 0, 0))
    subset = df_k[(df_k["ts"] >= start_night) & (df_k["ts"] <= end_night)]
    evening_bars = subset[subset["ts"].dt.hour >= 15]
    if evening_bars.empty:
        return pd.DataFrame()

    last_ev_date = evening_bars["ts"].dt.date.max()
    night_start_exact = datetime.datetime.combine(last_ev_date, datetime.time(15, 0, 0))
    return df_k[(df_k["ts"] >= night_start_exact) & (df_k["ts"] <= end_night)].copy()


def should_persist_overnight(
    night_bars: pd.DataFrame,
    target_date_str: str,
    now_dt: Optional[datetime.datetime] = None,
) -> bool:
    """僅在夜盤資料完整或已過清晨 05:05 收盤後才持久化至 CSV"""
    if night_bars is None or night_bars.empty:
        return False
    cur_now = now_dt or datetime.datetime.now()
    target_d = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
    ts_series = pd.to_datetime(night_bars["ts"])
    summary_stub = {
        "night_bars": len(night_bars),
        "night_start": str(ts_series.min()),
        "night_end": str(ts_series.max()),
    }
    if is_overnight_row_complete(summary_stub, target_date_str):
        return True
    return cur_now >= datetime.datetime.combine(target_d, datetime.time(5, 5, 0))


def save_overnight_level_to_csv(data_dir: str, target_date_str: str, night_bars: pd.DataFrame) -> bool:
    """將抓到的前一日夜盤極值寫入 overnight_levels_summary.csv"""
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
        atomic_write_csv(combined, lvl_path, index=False)
        return True
    except Exception as e:
        logger.debug(f"自動儲存夜盤極值至 CSV 失敗: {e}")
        return False


def load_or_fetch_overnight_levels(
    data_dir: str,
    target_date_str: str,
    gateway: Any = None,
    api: Any = None,
    contract: Any = None,
    allow_cnyes_fallback: bool = True,
    now_dt: Optional[datetime.datetime] = None,
) -> Tuple[Optional[float], Optional[float], str]:
    """
    取得指定交易日對應的前一日夜盤 (15:00~05:00) 高低點。
    回傳: (night_high, night_low, source_tag) in ('csv', 'shioaji', 'cnyes', 'none')
    """
    cur_d = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()

    # 1. 本地 CSV 快取
    for fn in ["overnight_levels_summary.csv", "overnight_levels_30days.csv"]:
        lvl_path = os.path.join(data_dir, fn)
        if not os.path.exists(lvl_path):
            continue
        try:
            df_lvl = pd.read_csv(lvl_path)
            match_row = df_lvl[df_lvl["trading_date"] == target_date_str]
            if not match_row.empty:
                row_dict = match_row.iloc[0].to_dict()
                if is_overnight_row_complete(row_dict, target_date_str):
                    return float(row_dict["night_high"]), float(row_dict["night_low"]), "csv"
                logger.warning(
                    f"⚠️ [夜盤防撞牆] 本地 {fn} 中 {target_date_str} 紀錄不完整 "
                    f"(night_bars={row_dict.get('night_bars')}, night_end={row_dict.get('night_end')})，重新抓取補齊..."
                )
        except Exception as e:
            logger.debug(f"讀取 {fn} 失敗: {e}")

    # 2. Gateway / Shioaji API 查詢
    prev_4d = (cur_d - datetime.timedelta(days=4)).strftime("%Y-%m-%d")
    if gateway is not None:
        try:
            df_k = gateway.fetch_kbars(prev_4d, target_date_str, timeout=5000)
            night_bars = extract_overnight_bars(df_k, cur_d)
            if not night_bars.empty:
                o_h = float(night_bars["high"].max())
                o_l = float(night_bars["low"].min())
                if should_persist_overnight(night_bars, target_date_str, now_dt=now_dt):
                    save_overnight_level_to_csv(data_dir, target_date_str, night_bars)
                return o_h, o_l, "shioaji"
        except Exception as gw_err:
            logger.warning(f"透過 Gateway 抓取夜盤極值逾時或略過: {gw_err}")
    elif api is not None and contract is not None:
        try:
            from gateways.shioaji_live import normalize_shioaji_kbars
            kbs = api.kbars(contract, start=prev_4d, end=target_date_str, timeout=5000)
            df_k = normalize_shioaji_kbars(kbs, subtract_minute=True)
            night_bars = extract_overnight_bars(df_k, cur_d)
            if not night_bars.empty:
                o_h = float(night_bars["high"].max())
                o_l = float(night_bars["low"].min())
                if should_persist_overnight(night_bars, target_date_str, now_dt=now_dt):
                    save_overnight_level_to_csv(data_dir, target_date_str, night_bars)
                return o_h, o_l, "shioaji"
        except Exception as api_err:
            logger.warning(f"從 Shioaji API 抓取夜盤極值逾時或略過: {api_err}")

    # 3. CNYES 公開備援
    if allow_cnyes_fallback:
        try:
            df_raw = fetch_cnyes_history(timeout=5)
            night_bars = extract_overnight_bars(df_raw, cur_date=cur_d)
            if not night_bars.empty:
                o_h = float(night_bars["high"].max())
                o_l = float(night_bars["low"].min())
                if should_persist_overnight(night_bars, target_date_str, now_dt=now_dt):
                    save_overnight_level_to_csv(data_dir, target_date_str, night_bars)
                return o_h, o_l, "cnyes"
        except Exception as cnyes_err:
            logger.debug(f"從公開備援抓取夜盤極值失敗: {cnyes_err}")

    return None, None, "none"


class StateRepo:
    """TradingDayState 原子 JSON 持久化倉儲"""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

    def get_state_path(self, date_str: Optional[str] = None) -> str:
        d_str = datetime.date.today().strftime("%Y%m%d") if not date_str else date_str.replace("-", "")
        return os.path.join(self.log_dir, f"state_{d_str}.json")

    def save_state(
        self,
        state: TradingDayState,
        custom_fields: Optional[Dict[str, Any]] = None,
        date_str: Optional[str] = None,
    ) -> bool:
        try:
            d_raw = date_str or state.trading_date or datetime.date.today().strftime("%Y-%m-%d")
            clean_d = d_raw.replace("-", "")
            today_fmt = f"{clean_d[:4]}-{clean_d[4:6]}-{clean_d[6:]}" if len(clean_d) == 8 else d_raw
            payload = {
                "date": today_fmt,
                "updated_at": datetime.datetime.now().strftime("%H:%M:%S"),
                "contract_code": state.contract_code,
                "position": int(state.position),
                "entry_price": float(state.entry_price),
                "entry_time": str(state.entry_time) if state.entry_time is not None else None,
                "highest_profit": float(state.highest_profit),
                "trade_count": int(state.trade_count),
                "pre_orb_high": float(state.pre_orb_high) if state.pre_orb_high is not None else None,
                "pre_orb_low": float(state.pre_orb_low) if state.pre_orb_low is not None else None,
                "orb_high": float(state.orb_high) if state.orb_high is not None else None,
                "orb_low": float(state.orb_low) if state.orb_low is not None else None,
                "orb_calculated": bool(state.orb_calculated),
                "entries_halted": bool(state.entries_halted),
                "overnight_high": float(state.overnight_high) if state.overnight_high is not None else None,
                "overnight_low": float(state.overnight_low) if state.overnight_low is not None else None,
            }
            if custom_fields:
                payload.update(custom_fields)
            atomic_write_json(self.get_state_path(today_fmt), payload)
            return True
        except Exception as e:
            logger.warning(f"保存策略狀態失敗: {e}")
            return False

    def load_state(self, date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
        try:
            path = self.get_state_path(date_str)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"讀取策略狀態失敗: {e}")
        return None

    def restore_into_state(
        self,
        state: TradingDayState,
        saved_state: Optional[Dict[str, Any]] = None,
    ) -> TradingDayState:
        data = saved_state if saved_state is not None else self.load_state(state.trading_date or None)
        if not data:
            return state
        try:
            raw_et = data.get("entry_time")
            parsed_et = None
            if raw_et:
                et_str = str(raw_et).strip()
                if len(et_str) <= 8 and data.get("date"):
                    et_str = f"{data['date']} {et_str}"
                try:
                    parsed_et = pd.Timestamp(et_str)
                except Exception:
                    parsed_et = None

            hp = float(data.get("highest_profit", 0.0))
            return dataclasses.replace(
                state,
                trading_date=str(data.get("date", state.trading_date)),
                contract_code=str(data.get("contract_code", state.contract_code) or state.contract_code),
                position=int(data.get("position", 0)),
                entry_price=float(data.get("entry_price", 0.0)),
                entry_time=parsed_et,
                highest_profit=hp,
                hp_before_bar_val=hp,
                trade_count=int(data.get("trade_count", 0)),
                entries_halted=bool(data.get("entries_halted", state.entries_halted)),
                pre_orb_high=float(data["pre_orb_high"]) if data.get("pre_orb_high") is not None else state.pre_orb_high,
                pre_orb_low=float(data["pre_orb_low"]) if data.get("pre_orb_low") is not None else state.pre_orb_low,
                orb_high=float(data["orb_high"]) if data.get("orb_high") is not None else state.orb_high,
                orb_low=float(data["orb_low"]) if data.get("orb_low") is not None else state.orb_low,
                orb_calculated=bool(data.get("orb_calculated", state.orb_calculated)),
            )
        except Exception as e:
            logger.warning(f"復原策略狀態失敗: {e}")
            return state


def append_order_audit(
    log_dir: str,
    action: str,
    qty: int,
    octype: str,
    result: FillResult,
    signal_price: Optional[float] = None,
    now_dt: Optional[datetime.datetime] = None,
) -> None:
    """將每次委託結果附加至 logs/<live|paper>/orders_YYYYMMDD.csv"""
    try:
        os.makedirs(log_dir, exist_ok=True)
        now = now_dt or datetime.datetime.now()
        path = os.path.join(log_dir, f"orders_{now.strftime('%Y%m%d')}.csv")
        is_new = not os.path.exists(path)
        has_signal_col = True
        if not is_new:
            cur_size = os.path.getsize(path)
            cached = _AUDIT_HEADER_CACHE.get(path)
            if cached is not None and cur_size >= cached[1] > 0:
                has_signal_col = cached[0]
            else:
                try:
                    with open(path, "r", encoding="utf-8") as rf:
                        first_line = rf.readline().strip()
                    has_signal_col = "signal_price" in first_line.split(",")
                    if cur_size > 0:
                        _AUDIT_HEADER_CACHE[path] = (has_signal_col, cur_size)
                except Exception:
                    has_signal_col = False

        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["time", "action", "qty", "octype", "status", "filled_qty", "avg_price", "ok", "signal_price"])
                has_signal_col = True
            row = [
                now.strftime("%H:%M:%S"),
                action,
                qty,
                octype,
                result.status,
                result.filled_qty,
                f"{result.avg_price:.0f}",
                result.ok,
            ]
            if has_signal_col:
                sig_str = f"{float(signal_price):.0f}" if (signal_price is not None and float(signal_price) > 0) else ""
                row.append(sig_str)
            w.writerow(row)
        try:
            _AUDIT_HEADER_CACHE[path] = (has_signal_col, os.path.getsize(path))
        except OSError:
            pass
    except Exception as e:
        logger.warning(f"寫入稽核紀錄失敗: {e}")


def load_command(command_path: str) -> Optional[Dict[str, Any]]:
    """讀取並刪除外部指令檔 logs/<mode>/command.json"""
    if not os.path.exists(command_path):
        return None
    try:
        with open(command_path, "r", encoding="utf-8") as f:
            cmd = json.load(f)
        clear_command(command_path)
        return cmd
    except Exception:
        return None


def clear_command(command_path: str) -> None:
    try:
        if os.path.exists(command_path):
            os.remove(command_path)
    except OSError:
        pass


def write_command(command_path: str, cmd_dict: Dict[str, Any]) -> None:
    atomic_write_json(command_path, cmd_dict)
