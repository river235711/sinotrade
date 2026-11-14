"""
回測核心引擎 (V2)
直接複用 core.orb_strategy + core.indicators + core.atr_scaler，確保回測與實盤執行同一套純領域邏輯。
"""
import os
from typing import Any, Dict, Optional

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from backtester.metrics import BacktestMetrics
from config import InstrumentConfig, ORBStrategyConfig
from core.atr_scaler import scale_config_from_daily
from core.orb_strategy import ORBStrategy
from gateways.data_repo import load_daily_history


class BacktestEngine:
    def __init__(
        self,
        data_path: str,
        inst_config: InstrumentConfig = None,
        cached_kbars_df: Optional[pd.DataFrame] = None,
        daily_history_df: Optional[pd.DataFrame] = None,
        overnight_levels_df: Optional[pd.DataFrame] = None,
    ):
        self.data_path = data_path
        self.inst_config = inst_config or InstrumentConfig()
        self.cached_kbars_df = cached_kbars_df
        self.daily_history_df = daily_history_df
        self.overnight_levels_df = overnight_levels_df
        self.kbars = None
        self.day_df = None
        self.target_date_fmt = None
        self.active_contract_month = ""

    def load_taifex_csv(self, target_date: str = "20261030", contract_month: str = "AUTO") -> pd.DataFrame:
        """載入期交所 CSV 或快取 1 分 K 資料"""
        cache_path = os.path.join(os.path.dirname(self.data_path), "tmf_20days_1m_kbars.csv")
        day_date_fmt = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
        self.target_date_fmt = day_date_fmt
        cached_df = self.cached_kbars_df
        if cached_df is None and os.path.exists(cache_path):
            cached_df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if cached_df is not None and not cached_df.empty:
            sub = cached_df[cached_df["date"] == day_date_fmt].copy()
            if len(sub) > 0:
                self.active_contract_month = str(sub["contract"].iloc[0])
                self.kbars = sub[["open", "high", "low", "close", "volume"]].copy()
                return self.kbars

        if not os.path.exists(self.data_path):
            raise FileNotFoundError(f"找不到期交所資料檔: {self.data_path}")

        df = pd.read_csv(self.data_path, encoding="big5", low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        df["商品代號"] = df["商品代號"].astype(str).str.strip()
        df["到期月份(週別)"] = df["到期月份(週別)"].astype(str).str.strip()
        df["成交日期"] = df["成交日期"].astype(str).str.strip()
        df["成交時間"] = df["成交時間"].astype(str).str.zfill(6)

        day_all = df[
            (df["商品代號"] == self.inst_config.symbol)
            & (df["成交日期"] == target_date)
            & (df["成交時間"] >= "084500")
            & (df["成交時間"] <= "134500")
            & (~df["到期月份(週別)"].str.contains("/"))
        ].copy()

        if len(day_all) == 0:
            raise ValueError(f"在 {target_date} 查無 {self.inst_config.symbol} 日盤資料！")

        day_all["成交數量"] = pd.to_numeric(day_all["成交數量(B+S)"], errors="coerce") / 2.0

        if contract_month == "AUTO":
            vol_by_month = day_all.groupby("到期月份(週別)")["成交數量"].sum()
            selected_month = str(vol_by_month.idxmax())
        else:
            selected_month = contract_month

        self.active_contract_month = selected_month
        day_ticks = day_all[day_all["到期月份(週別)"] == selected_month].copy()
        day_ticks["成交價格"] = pd.to_numeric(day_ticks["成交價格"], errors="coerce")

        if len(day_ticks) == 0:
            raise ValueError(f"在 {target_date} 查無 {self.inst_config.symbol} {selected_month} 日盤資料！")

        day_ticks["timestamp"] = pd.to_datetime(
            day_ticks["成交日期"] + " " + day_ticks["成交時間"],
            format="%Y%m%d %H%M%S",
        )
        day_ticks = day_ticks.set_index("timestamp")

        kbars = day_ticks["成交價格"].resample("1min").ohlc()
        kbars["volume"] = day_ticks["成交數量"].resample("1min").sum()
        kbars = kbars.dropna()

        self.kbars = kbars
        self.day_df = day_ticks
        return kbars

    def run_orb_strategy(self, strategy_config: ORBStrategyConfig = None) -> Dict[str, Any]:
        """執行 ORB 策略回測 (完整複用 core/ 策略引擎與實盤兩階段 09:15 ORB 時序)"""
        if self.kbars is None:
            raise ValueError("請先執行 load_taifex_csv() 載入行情數據")

        base_cfg = strategy_config or ORBStrategyConfig()
        atr_info = {"atr": None, "ratio": 1.0, "applied": False, "atr_days": 0}
        eff_cfg = base_cfg
        if getattr(base_cfg, "use_atr_scaling", False) and self.target_date_fmt:
            data_dir = os.path.dirname(os.path.abspath(self.data_path))
            daily_df = self.daily_history_df if self.daily_history_df is not None else load_daily_history(data_dir)
            eff_cfg, atr_info = scale_config_from_daily(base_cfg, daily_df, self.target_date_fmt)

        strategy = ORBStrategy(config=eff_cfg, inst_config=self.inst_config)

        if getattr(eff_cfg, "use_overnight_wall_filter", False) and self.target_date_fmt:
            if self.overnight_levels_df is not None:
                if not self.overnight_levels_df.empty:
                    match_row = self.overnight_levels_df[
                        self.overnight_levels_df["trading_date"] == self.target_date_fmt
                    ]
                    if not match_row.empty:
                        strategy.set_overnight_levels(
                            overnight_high=match_row.iloc[0]["night_high"],
                            overnight_low=match_row.iloc[0]["night_low"],
                        )
            else:
                data_dir = os.path.dirname(os.path.abspath(self.data_path))
                for fn in ["overnight_levels_summary.csv", "overnight_levels_30days.csv"]:
                    levels_file = os.path.join(data_dir, fn)
                    if os.path.exists(levels_file):
                        try:
                            df_lvl = pd.read_csv(levels_file)
                            match_row = df_lvl[df_lvl["trading_date"] == self.target_date_fmt]
                            if not match_row.empty:
                                strategy.set_overnight_levels(
                                    overnight_high=match_row.iloc[0]["night_high"],
                                    overnight_low=match_row.iloc[0]["night_low"],
                                )
                                break
                        except Exception:
                            pass

        self.kbars = strategy.prepare_indicators(self.kbars)

        orb_high, orb_low = None, None
        times = self.kbars.index
        n_bars = len(times)
        opens = self.kbars["open"].to_numpy(dtype=float)
        highs = self.kbars["high"].to_numpy(dtype=float)
        lows = self.kbars["low"].to_numpy(dtype=float)
        closes = self.kbars["close"].to_numpy(dtype=float)
        vwaps = self.kbars["vwap"].to_numpy(dtype=float)
        ema9s = self.kbars["ema9"].to_numpy(dtype=float)
        ema21s = self.kbars["ema21"].to_numpy(dtype=float)
        t_strs = times.strftime("%H:%M:%S").tolist()
        orb_end_time = eff_cfg.orb_end_time
        one_min = pd.Timedelta(minutes=1)

        for idx in range(n_bars):
            ts = times[idx]
            t_str = t_strs[idx]
            if not strategy.orb_calculated:
                wall_clock_str = (ts + one_min).strftime("%H:%M:%S")
                if wall_clock_str >= orb_end_time:
                    slice_df = self.kbars.iloc[: idx + 1].copy()
                    if idx + 1 < n_bars and t_strs[idx + 1] == orb_end_time:
                        next_ts = times[idx + 1]
                        open_p = float(opens[idx + 1])
                        open_row = self.kbars.iloc[idx + 1].copy()
                        open_row["high"] = open_p
                        open_row["low"] = open_p
                        open_row["close"] = open_p
                        slice_df.loc[next_ts] = open_row
                    orb_high, orb_low = strategy.calculate_orb_levels(slice_df)
            elif t_str == orb_end_time:
                orb_high, orb_low = strategy.calculate_orb_levels(self.kbars.iloc[: idx + 1])

            strategy.execute_values(
                ts,
                opens[idx],
                highs[idx],
                lows[idx],
                closes[idx],
                vwaps[idx],
                ema9s[idx],
                ema21s[idx],
                t_str=t_str,
            )

        if not strategy.orb_calculated:
            orb_high, orb_low = strategy.calculate_orb_levels(self.kbars)

        metrics = BacktestMetrics.calculate(strategy.trades, self.inst_config)
        metrics["orb_high"] = orb_high
        metrics["orb_low"] = orb_low
        metrics["kbars"] = self.kbars
        metrics["atr_info"] = atr_info
        metrics["effective_config"] = eff_cfg
        return metrics

    def plot_results(self, metrics: Dict[str, Any], output_path: str = "backtest_result.png"):
        kbars = metrics["kbars"]
        trades_df = metrics["trades_df"]
        orb_high = metrics["orb_high"]
        orb_low = metrics["orb_low"]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [2.5, 1]}, sharex=True)
        plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")

        times = kbars.index
        ax1.plot(times, kbars["close"], color="#2c3e50", linewidth=1.2, label="TMF Close Price")

        if orb_high is not None and orb_low is not None:
            ax1.axhline(orb_high, color="#e74c3c", linestyle="--", linewidth=1.2, label=f"ORB High ({orb_high:.0f})")
            ax1.axhline(orb_low, color="#27ae60", linestyle="--", linewidth=1.2, label=f"ORB Low ({orb_low:.0f})")
            t_strs = times.strftime("%H:%M:%S")
            orb_span = times[(t_strs >= "09:00:00") & (t_strs <= "09:15:00")]
            span_start = orb_span[0] if len(orb_span) > 0 else times[0]
            span_end = orb_span[-1] if len(orb_span) > 0 else times[min(15, len(times) - 1)]
            ax1.axvspan(span_start, span_end, color="#f39c12", alpha=0.15, label="ORB Window (09:00-09:15)")

        if not trades_df.empty:
            for idx, trade in trades_df.iterrows():
                e_time = pd.to_datetime(trade["entry_time"])
                x_time = pd.to_datetime(trade["exit_time"])
                e_price = trade["entry_price"]
                x_price = trade["exit_price"]
                pnl = trade["pnl_pts"]
                act = trade["action"]

                if act == "BUY":
                    ax1.scatter([e_time], [e_price], marker="^", color="#e74c3c", s=120, zorder=5, label="Buy Entry" if idx == 0 else "")
                else:
                    ax1.scatter([e_time], [e_price], marker="v", color="#27ae60", s=120, zorder=5, label="Sell Entry" if idx == 0 else "")

                exit_color = "#e74c3c" if pnl < 0 else "#27ae60"
                ax1.scatter([x_time], [x_price], marker="X", color=exit_color, s=100, zorder=5)
                ax1.annotate(
                    f"{act} Exit\n{pnl:+.0f}pts (${trade['net_ntd']:+.0f})",
                    (x_time, x_price),
                    textcoords="offset points",
                    xytext=(0, 15 if act == "BUY" else -25),
                    ha="center",
                    fontsize=8,
                    fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=exit_color, alpha=0.9),
                )

        plot_date = self.target_date_fmt or (times[0].strftime("%Y-%m-%d") if len(times) > 0 else "")
        ax1.set_title(f"TMF Micro Taiwan Index Day Trading - ORB Strategy Backtest ({plot_date})", fontsize=14, fontweight="bold", pad=12)
        ax1.set_ylabel("Price (Points)", fontsize=11)
        ax1.legend(loc="upper right", frameon=True)
        ax1.grid(True, linestyle=":", alpha=0.6)

        if not trades_df.empty:
            equity_curve = pd.Series(0.0, index=times)
            for _, t in trades_df.iterrows():
                x_time = pd.to_datetime(t["exit_time"])
                equity_curve.loc[equity_curve.index >= x_time] += t["net_ntd"]
            ax2.plot(equity_curve.index, equity_curve.values, color="#8e44ad", linewidth=2.0, label="Cumulative Net PnL (NTD)")
            ax2.fill_between(equity_curve.index, equity_curve.values, 0, color="#8e44ad", alpha=0.15)
        else:
            ax2.plot(times, [0] * len(times), color="#8e44ad", label="Cumulative Net PnL (NTD)")

        ax2.set_ylabel("Net PnL (NTD)", fontsize=11)
        ax2.set_xlabel("Time", fontsize=11)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax2.legend(loc="upper left", frameon=True)
        ax2.grid(True, linestyle=":", alpha=0.6)

        plt.tight_layout()
        plt.savefig(output_path, dpi=200)
        plt.close()
        return output_path
