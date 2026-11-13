# -*- coding: utf-8 -*-
"""
即時行情、合約解析、夜盤極值與 ORB 盤中狀態報告共用模組

職責：
1. 統一解析 Shioaji TMF 當月主力合約 (TMFR1 -> 實際交易月合約)
2. 統一對齊 Shioaji api.kbars 收盤時間戳為策略起始時間戳 (-1 分鐘)
3. 共用 CNYES 大台 (TWF:TXF:FUTURES) 備援行情抓取與來源隔離警示
4. 驗證並存取完整夜盤極值 (overnight_levels_summary.csv)，防止半夜不完整紀錄汙染隔日防撞牆
5. 產出 09:15 ORB 鎖定時之「可進場 / 不進場 + 停損與各階停利點」即時提示訊息
"""

import os
import json
import logging
import warnings
import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

import pandas as pd

from config import atomic_write_csv

logger = logging.getLogger("TMFTrader.MarketData")

CNYES_TXF_URL = "https://ws.api.cnyes.com/ws/api/v1/charting/history?symbol=TWF:TXF:FUTURES&resolution=1&quote=1"


def resolve_tmf_contract(api: Any, code: str = "TMFR1") -> Any:
    """
    從 Shioaji API 解析 TMF 主力或指定合約；若為連續月合約 (如 TMFR1) 自動解出實際可下單月份合約
    """
    if api is None:
        return None

    tmf_contracts = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            tmf_contracts = list(api.contracts.futures("TMF"))
        except Exception:
            tmf_contracts = []
        if not tmf_contracts:
            futs = getattr(getattr(getattr(api, "Contracts", None), "Futures", None), "TMF", None)
            if futs is not None:
                tmf_contracts = list(futs)

    if not tmf_contracts:
        return None

    def _unwrap(c: Any) -> Any:
        return getattr(c, "contract", c)

    selected = None
    target_code_req = code or "TMFR1"
    for c in tmf_contracts:
        raw_c = _unwrap(c)
        c_code = getattr(raw_c, "code", getattr(c, "code", ""))
        c_name = getattr(raw_c, "name", getattr(c, "name", "")) or ""
        if c_code == target_code_req or (c_name and target_code_req in c_name):
            selected = raw_c
            break

    if selected is None:
        for c in tmf_contracts:
            raw_c = _unwrap(c)
            if getattr(raw_c, "code", getattr(c, "code", "")) == "TMFR1":
                selected = raw_c
                break

    if selected is None:
        return None

    resolved_target = getattr(selected, "target_code", None)
    if resolved_target:
        for c in tmf_contracts:
            raw_c = _unwrap(c)
            if getattr(raw_c, "code", getattr(c, "code", "")) == resolved_target:
                logger.info(
                    f"自動將連續合約 {getattr(selected, 'code', target_code_req)} "
                    f"解析為當月主力可下單合約: {resolved_target} ({getattr(raw_c, 'name', '')})"
                )
                return raw_c

    return selected


def normalize_shioaji_kbars(raw_kbars: Any, subtract_minute: bool = True) -> pd.DataFrame:
    """
    將 Shioaji api.kbars 回傳物件標準化為以 ts (Start Timestamp) 為索引的 OHLCV DataFrame。
    Shioaji 原生時間戳為收盤時間 (如 08:46:00 代表 08:45~08:46)，subtract_minute=True 時減去 1 分鐘對齊回測與策略核心。
    """
    if raw_kbars is None:
        return pd.DataFrame()
    if isinstance(raw_kbars, pd.DataFrame):
        df = raw_kbars.copy()
    else:
        df = pd.DataFrame({**raw_kbars})
    if df.empty:
        return pd.DataFrame()

    df.columns = [str(c).lower() for c in df.columns]
    if "ts" not in df.columns:
        return pd.DataFrame()

    df["ts"] = pd.to_datetime(df["ts"])
    if subtract_minute:
        df["ts"] = df["ts"] - pd.Timedelta(minutes=1)
    df = df.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").set_index("ts")
    return df


def fetch_cnyes_history(timeout: int = 5) -> pd.DataFrame:
    """
    從 CNYES 公開行情介面抓取近月大台指期 (TWF:TXF:FUTURES) 1分K 備援資料。
    注意：CNYES 時間戳已為起始時間 (Start Timestamp)，無需再減 1 分鐘，但成交量屬大台量能 (TXF)。
    """
    import urllib.request
    from live.notifier import _ssl_ctx

    req = urllib.request.Request(CNYES_TXF_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
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
    """取得指定交易日日盤 (08:45~13:44 Start Timestamp) 之 CNYES 備援 1分K (已索引 ts)"""
    df = fetch_cnyes_history(timeout=timeout)
    if df.empty:
        return pd.DataFrame()
    df = df.set_index("ts").sort_index()
    day_df = df[df.index.strftime("%Y-%m-%d") == today_str].between_time("08:45", "13:44")
    if day_df.empty:
        return pd.DataFrame()
    return day_df[["open", "high", "low", "close", "volume"]].copy()


def is_today_day_session_snapshot(snap: Any, today_str: str) -> bool:
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


def is_overnight_row_complete(
    row: Mapping[str, Any],
    target_date_str: str,
    min_bars: int = 800,
    min_end_hhmm: datetime.time = datetime.time(4, 55, 0),
) -> bool:
    """
    檢查 overnight_levels_summary.csv 單日紀錄是否包含完整夜盤 (約 15:00 ~ 05:00 共 ~840 根)。
    若中途 (如 00:01) 曾誤寫入半截夜盤 (如 night_bars=541)，回傳 False 促使程式重抓補齊。
    """
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
            min_end_dt = datetime.datetime.combine(target_d, min_end_hhmm)
            if end_dt < min_end_dt:
                return False
        except Exception:
            return False

    return True


def extract_overnight_bars(df_kbars: pd.DataFrame, cur_date: datetime.date) -> pd.DataFrame:
    """從含 ts 欄位或 ts 索引的 K 棒 DataFrame 萃取對應 cur_date 的前一夜盤 (15:00~05:00) K 棒"""
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
    """
    僅在夜盤資料已完整 (或已過該日清晨 05:05 夜盤收盤後) 才將紀錄寫入 CSV，
    防止跨日 00:00 瞬間重啟把未走完的半截夜盤鎖死進快取。
    """
    if night_bars is None or night_bars.empty:
        return False
    cur_now = now_dt or datetime.datetime.now()
    target_d = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
    summary_stub = {
        "night_bars": len(night_bars),
        "night_end": str(pd.to_datetime(night_bars["ts"]).max()),
    }
    if is_overnight_row_complete(summary_stub, target_date_str):
        return True
    # 若已過當日 05:05 (例如假日/縮短盤確實未達 800 根)，仍允許寫入
    return cur_now >= datetime.datetime.combine(target_d, datetime.time(5, 5, 0))


def save_overnight_level_to_csv(data_dir: str, target_date_str: str, night_bars: pd.DataFrame) -> bool:
    """將抓到的前一日夜盤極值寫入 overnight_levels_summary.csv 保存"""
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
    api: Any = None,
    contract: Any = None,
    allow_cnyes_fallback: bool = True,
    now_dt: Optional[datetime.datetime] = None,
) -> Tuple[Optional[float], Optional[float], str]:
    """
    取得指定交易日對應的前一日夜盤 (15:00~05:00) 高低點。
    回傳: (night_high, night_low, source_tag)，source_tag in ('csv', 'shioaji', 'cnyes', 'none')
    """
    cur_d = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()

    # 1. 先查本地彙整 CSV（會檢驗紀錄完整度，避免讀入半截殘缺夜盤）
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

    # 2. 從 Shioaji API 即時查詢（統一 -1 分鐘對齊起始時間戳）
    if api is not None and contract is not None:
        try:
            prev_4d = (cur_d - datetime.timedelta(days=4)).strftime("%Y-%m-%d")
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

    # 3. CNYES 免登入公開備援
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


def max_trades_for_config(cfg: Any) -> int:
    """計算策略設定允許的當日最大進場次數上限"""
    max_t = getattr(cfg, "max_trades_per_day", None)
    if max_t is not None:
        return int(max_t)
    return int(getattr(cfg, "max_initial_trades", 1)) + (1 if getattr(cfg, "enable_sar", False) else 0)


def _format_stop_and_profit_lines(cfg: Any, direction: int, ref_price: float) -> str:
    """依進場價/基準價格式化停損與各階停利價位"""
    sl = float(cfg.stop_loss_pts)
    b1_t, b1_l = float(cfg.breakeven_trigger_pts), float(cfg.breakeven_lock_pts)
    t15_t = float(getattr(cfg, "tier1_5_trigger_pts", 160.0))
    t15_l = float(getattr(cfg, "tier1_5_lock_pts", 100.0))
    t2_t, t2_l = float(cfg.tier2_trigger_pts), float(cfg.tier2_lock_pts)
    tr_a, tr_d = float(cfg.trailing_activation_pts), float(cfg.trailing_distance_pts)

    sign = 1.0 if direction == 1 else -1.0
    sl_px = ref_price - sign * sl
    b1_trig_px = ref_price + sign * b1_t
    b1_lock_px = ref_price + sign * b1_l
    t15_trig_px = ref_price + sign * t15_t
    t15_lock_px = ref_price + sign * t15_l
    t2_trig_px = ref_price + sign * t2_t
    t2_lock_px = ref_price + sign * t2_l
    tr_act_px = ref_price + sign * tr_a
    tr_floor_px = ref_price + sign * (tr_a - tr_d)

    return (
        f"  • 停損價: {sl_px:.0f} (-{sl:.0f}點)\n"
        f"  • 一階保本: 觸 {b1_trig_px:.0f} (+{b1_t:.0f}) → 鎖 {b1_lock_px:.0f} (+{b1_l:.0f})\n"
        f"  • 中階鎖利: 觸 {t15_trig_px:.0f} (+{t15_t:.0f}) → 鎖 {t15_lock_px:.0f} (+{t15_l:.0f})\n"
        f"  • 二階鎖利: 觸 {t2_trig_px:.0f} (+{t2_t:.0f}) → 鎖 {t2_lock_px:.0f} (+{t2_l:.0f})\n"
        f"  • 三階移停: 觸 {tr_act_px:.0f} (+{tr_a:.0f}) → 回撤 {tr_d:.0f} 點 (保底 {tr_floor_px:.0f} / +{tr_a - tr_d:.0f})"
    )


def build_0915_orb_report(strategy: Any, entries_halted: bool = False) -> Tuple[str, str]:
    """
    於 09:15 ORB 區間鎖定後，計算「今日可否進場」之明確結論與停損/各階停利點提示。
    回傳: (status_code, report_message)，status_code in ('CAN_ENTER', 'NO_ENTRY', 'IN_POSITION')
    """
    cfg = strategy.config
    orb_h = float(strategy.orb_high)
    orb_l = float(strategy.orb_low)
    orb_range = orb_h - orb_l
    max_orb = float(getattr(cfg, "max_orb_range_pts", 320.0))
    max_chase = float(getattr(cfg, "max_breakout_chase_pts", 60.0))
    max_init = int(getattr(cfg, "max_initial_trades", 1))
    entry_start = str(getattr(cfg, "entry_start_time", "09:16:00"))[:5]
    entry_end = str(getattr(cfg, "entry_end_time", "11:30:00"))[:5]

    # 夜盤防撞牆有效觸發界線
    buy_th = orb_h
    sell_th = orb_l
    wall_lines = []
    if getattr(cfg, "use_overnight_wall_filter", False):
        thresh_pts = float(getattr(cfg, "overnight_wall_threshold_pts", 40.0))
        if (
            strategy.overnight_high is not None
            and orb_h < strategy.overnight_high
            and (strategy.overnight_high - orb_h) <= thresh_pts
        ):
            buy_th = float(strategy.overnight_high)
            wall_lines.append(f"• 🛡️ 啟動做多防撞牆: ORB上界距夜高僅 {buy_th - orb_h:.0f} 點，需突破夜高 {buy_th:.0f} 才開多！")
        if (
            strategy.overnight_low is not None
            and orb_l > strategy.overnight_low
            and (orb_l - strategy.overnight_low) <= thresh_pts
        ):
            sell_th = float(strategy.overnight_low)
            wall_lines.append(f"• 🛡️ 啟動做空防撞地: ORB下界距夜低僅 {orb_l - sell_th:.0f} 點，需跌破夜低 {sell_th:.0f} 才開空！")
        if not wall_lines and strategy.overnight_high is not None and strategy.overnight_low is not None:
            wall_lines.append(f"• 🌙 夜盤參考水位: 夜高 {strategy.overnight_high:.0f} / 夜低 {strategy.overnight_low:.0f} (距離安全)")

    wall_block = ("\n" + "\n".join(wall_lines)) if wall_lines else ""
    header = (
        f"📌 [09:15 ORB 區間確立]\n"
        f"現貨開盤 15 分鐘關鍵區間已鎖定！\n"
        f"• 原始 ORB 區間: {orb_l:.0f} ~ {orb_h:.0f} (振幅 {orb_range:.0f} 點 / 上限 {max_orb:.0f} 點){wall_block}"
    )

    sl = float(cfg.stop_loss_pts)
    b1_t, b1_l = float(cfg.breakeven_trigger_pts), float(cfg.breakeven_lock_pts)
    t15_t = float(getattr(cfg, "tier1_5_trigger_pts", 160.0))
    t15_l = float(getattr(cfg, "tier1_5_lock_pts", 100.0))
    t2_t, t2_l = float(cfg.tier2_trigger_pts), float(cfg.tier2_lock_pts)
    tr_a, tr_d = float(cfg.trailing_activation_pts), float(cfg.trailing_distance_pts)
    pts_spec = (
        f"停損 -{sl:.0f} | 一階 +{b1_t:.0f}→+{b1_l:.0f} | 中階 +{t15_t:.0f}→+{t15_l:.0f} | "
        f"二階 +{t2_t:.0f}→+{t2_l:.0f} | 三階 +{tr_a:.0f}(回撤{tr_d:.0f})"
    )

    if getattr(strategy, "position", 0) != 0 and float(getattr(strategy, "entry_price", 0.0)) > 0:
        pos_dir = int(strategy.position)
        entry_p = float(strategy.entry_price)
        dir_txt = "🟢多單" if pos_dir == 1 else "🔴空單"
        levels_txt = _format_stop_and_profit_lines(cfg, pos_dir, entry_p)
        msg = (
            f"{header}\n"
            f"• 📢 進場狀態: ✅ 已於 09:15 前早盤強勢突破進場 ({dir_txt} @ {entry_p:.0f}，今日不再開新倉)\n"
            f"• 🎯 今日點數規格: {pts_spec}\n"
            f"• 📍 實際持倉停損與各階停利價位 (依成交價 {entry_p:.0f} 計算):\n{levels_txt}"
        )
        return "IN_POSITION", msg

    no_entry_reasons = []
    if orb_range > max_orb:
        no_entry_reasons.append(f"ORB 振幅 {orb_range:.0f} 點超過上限 {max_orb:.0f} 點 ({entry_start} 後不追突破)")
    if entries_halted:
        no_entry_reasons.append("目前已設定停開新倉")
    if int(getattr(strategy, "trade_count", 0)) >= max_init:
        no_entry_reasons.append(f"今日首次進場次數已滿 ({strategy.trade_count}/{max_init})")

    if no_entry_reasons:
        reason_str = "；".join(no_entry_reasons)
        msg = (
            f"{header}\n"
            f"• 📢 進場判斷: 🚫 今日不進場\n"
            f"• ℹ️ 原因說明: {reason_str}，今日空手觀望。"
        )
        return "NO_ENTRY", msg

    long_levels = _format_stop_and_profit_lines(cfg, 1, buy_th)
    short_levels = _format_stop_and_profit_lines(cfg, -1, sell_th)
    msg = (
        f"{header}\n"
        f"• 📢 進場判斷: ✅ 可進場 ({entry_start} ~ {entry_end} 符合空間上限)\n"
        f"• 突破觸發區間: 做多 {buy_th:.0f}~{buy_th + max_chase:.0f} / 做空 {sell_th - max_chase:.0f}~{sell_th:.0f} (+ VWAP/EMA 共振)\n"
        f"• 🎯 今日 ATR 點數規格: {pts_spec}\n"
        f"• 📈 若做多 (以界線 {buy_th:.0f} 試算，實際以成交價為準):\n{long_levels}\n"
        f"• 📉 若做空 (以界線 {sell_th:.0f} 試算，實際以成交價為準):\n{short_levels}"
    )
    return "CAN_ENTER", msg
