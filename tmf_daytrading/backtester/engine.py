"""
回測核心引擎
負責載入期交所真實行情資料、合成K棒、執行策略並產出圖表。
"""
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from typing import Dict, Any, Optional

from config import InstrumentConfig, ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date
from backtester.metrics import BacktestMetrics

class BacktestEngine:
    def __init__(self, data_path: str, inst_config: InstrumentConfig = None):
        self.data_path = data_path
        self.inst_config = inst_config or InstrumentConfig()
        self.kbars = None
        self.day_df = None
        self.target_date_fmt = None

    def load_taifex_csv(self, target_date: str = "20261030", contract_month: str = "AUTO") -> pd.DataFrame:
        """載入期交所 CSV 檔案並合成日盤 1分K (若存在快取則優先讀取快取加速)"""
        cache_path = os.path.join(os.path.dirname(self.data_path), "tmf_20days_1m_kbars.csv")
        day_date_fmt = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
        self.target_date_fmt = day_date_fmt
        if os.path.exists(cache_path):
            cached_df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            sub = cached_df[cached_df['date'] == day_date_fmt].copy()
            if len(sub) > 0:
                self.active_contract_month = str(sub['contract'].iloc[0])
                self.kbars = sub[['open', 'high', 'low', 'close', 'volume']].copy()
                return self.kbars

        if not os.path.exists(self.data_path):
            raise FileNotFoundError(f"找不到期交所資料檔: {self.data_path}")

        df = pd.read_csv(self.data_path, encoding='big5', low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        df['商品代號'] = df['商品代號'].astype(str).str.strip()
        df['到期月份(週別)'] = df['到期月份(週別)'].astype(str).str.strip()
        df['成交日期'] = df['成交日期'].astype(str).str.strip()
        df['成交時間'] = df['成交時間'].astype(str).str.zfill(6)

        # 先篩選商品、目標日期與日盤時段
        day_all = df[
            (df['商品代號'] == self.inst_config.symbol) &
            (df['成交日期'] == target_date) &
            (df['成交時間'] >= '084500') &
            (df['成交時間'] <= '134500') &
            (~df['到期月份(週別)'].str.contains('/'))  # 排除跨月價差單
        ].copy()

        if len(day_all) == 0:
            raise ValueError(f"在 {target_date} 查無 {self.inst_config.symbol} 日盤資料！")

        day_all['成交數量'] = pd.to_numeric(day_all['成交數量(B+S)'], errors='coerce') / 2.0

        # 自動選擇成交量最大的近月主力合約
        if contract_month == "AUTO":
            vol_by_month = day_all.groupby('到期月份(週別)')['成交數量'].sum()
            selected_month = str(vol_by_month.idxmax())
        else:
            selected_month = contract_month

        self.active_contract_month = selected_month
        day_ticks = day_all[day_all['到期月份(週別)'] == selected_month].copy()
        day_ticks['成交價格'] = pd.to_numeric(day_ticks['成交價格'], errors='coerce')

        if len(day_ticks) == 0:
            raise ValueError(f"在 {target_date} 查無 {self.inst_config.symbol} {selected_month} 日盤資料！")

        day_ticks['timestamp'] = pd.to_datetime(
            day_ticks['成交日期'] + ' ' + day_ticks['成交時間'], 
            format='%Y%m%d %H%M%S'
        )
        day_ticks = day_ticks.set_index('timestamp')

        # 逐分重採樣 (Resample to 1-Minute OHLCV)
        kbars = day_ticks['成交價格'].resample('1min').ohlc()
        kbars['volume'] = day_ticks['成交數量'].resample('1min').sum()
        kbars = kbars.dropna()

        self.kbars = kbars
        self.day_df = day_ticks
        return kbars

    def run_orb_strategy(self, strategy_config: ORBStrategyConfig = None) -> Dict[str, Any]:
        """執行 ORB 策略回測 (含 VWAP + EMA 指標與 20日 ATR 動態波動率縮放)"""
        if self.kbars is None:
            raise ValueError("請先執行 load_taifex_csv() 載入行情數據")

        base_cfg = strategy_config or ORBStrategyConfig()
        atr_info = {"atr": None, "ratio": 1.0, "applied": False, "atr_days": 0}
        eff_cfg = base_cfg
        if getattr(base_cfg, "use_atr_scaling", False) and self.target_date_fmt:
            data_dir = os.path.dirname(os.path.abspath(self.data_path))
            eff_cfg, atr_info = config_for_date(base_cfg, data_dir, self.target_date_fmt)

        strategy = ORBStrategy(config=eff_cfg, inst_config=self.inst_config)
        self.kbars = strategy.prepare_indicators(self.kbars)
        orb_high, orb_low = strategy.calculate_orb_levels(self.kbars)

        prev_bar = None
        for ts, bar in self.kbars.iterrows():
            strategy.execute_bar(ts, bar, prev_bar)
            prev_bar = bar

        metrics = BacktestMetrics.calculate(strategy.trades, self.inst_config)
        metrics["orb_high"] = orb_high
        metrics["orb_low"] = orb_low
        metrics["kbars"] = self.kbars
        metrics["atr_info"] = atr_info
        metrics["effective_config"] = eff_cfg
        return metrics

    def plot_results(self, metrics: Dict[str, Any], output_path: str = "backtest_result.png"):
        """繪製回測績效圖與 K 線進出場點"""
        kbars = metrics["kbars"]
        trades_df = metrics["trades_df"]
        orb_high = metrics["orb_high"]
        orb_low = metrics["orb_low"]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), gridspec_kw={'height_ratios': [2.5, 1]}, sharex=True)
        plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')

        # 上圖：價格與進出場點
        times = kbars.index
        ax1.plot(times, kbars['close'], color='#2c3e50', linewidth=1.2, label='TMF Close Price')
        
        # 標示 ORB 高低點區間
        if orb_high is not None and orb_low is not None:
            ax1.axhline(orb_high, color='#e74c3c', linestyle='--', linewidth=1.2, label=f'ORB High ({orb_high:.0f})')
            ax1.axhline(orb_low, color='#27ae60', linestyle='--', linewidth=1.2, label=f'ORB Low ({orb_low:.0f})')
            ax1.axvspan(times[0], times[min(15, len(times)-1)], color='#f39c12', alpha=0.15, label='ORB Window (08:45-09:00)')

        # 標記進出場點
        if not trades_df.empty:
            for idx, trade in trades_df.iterrows():
                e_time = pd.to_datetime(trade['entry_time'])
                x_time = pd.to_datetime(trade['exit_time'])
                e_price = trade['entry_price']
                x_price = trade['exit_price']
                pnl = trade['pnl_pts']
                act = trade['action']

                # 進場箭頭
                if act == 'BUY':
                    ax1.scatter([e_time], [e_price], marker='^', color='#e74c3c', s=120, zorder=5, label='Buy Entry' if idx==0 else "")
                else:
                    ax1.scatter([e_time], [e_price], marker='v', color='#27ae60', s=120, zorder=5, label='Sell Entry' if idx==0 else "")

                # 出場標記
                exit_color = '#e74c3c' if pnl < 0 else '#27ae60'
                ax1.scatter([x_time], [x_price], marker='X', color=exit_color, s=100, zorder=5)
                ax1.annotate(
                    f"{act} Exit\n{pnl:+.0f}pts (${trade['net_ntd']:+.0f})",
                    (x_time, x_price),
                    textcoords="offset points",
                    xytext=(0, 15 if act=='BUY' else -25),
                    ha='center',
                    fontsize=8,
                    fontweight='bold',
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=exit_color, alpha=0.9)
                )

        ax1.set_title(f"TMF Micro Taiwan Index Day Trading - ORB Strategy Backtest (2026-10-30)", fontsize=14, fontweight='bold', pad=12)
        ax1.set_ylabel("Price (Points)", fontsize=11)
        ax1.legend(loc='upper right', frameon=True)
        ax1.grid(True, linestyle=':', alpha=0.6)

        # 下圖：累積淨損益 (NTD)
        if not trades_df.empty:
            # 建立時間軸累積損益
            equity_curve = pd.Series(0.0, index=times)
            for _, t in trades_df.iterrows():
                x_time = pd.to_datetime(t['exit_time'])
                equity_curve.loc[equity_curve.index >= x_time] += t['net_ntd']

            ax2.plot(equity_curve.index, equity_curve.values, color='#8e44ad', linewidth=2.0, label='Cumulative Net PnL (NTD)')
            ax2.fill_between(equity_curve.index, equity_curve.values, 0, color='#8e44ad', alpha=0.15)
        else:
            ax2.plot(times, [0]*len(times), color='#8e44ad', label='Cumulative Net PnL (NTD)')

        ax2.set_ylabel("Net PnL (NTD)", fontsize=11)
        ax2.set_xlabel("Time", fontsize=11)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax2.legend(loc='upper left', frameon=True)
        ax2.grid(True, linestyle=':', alpha=0.6)

        plt.tight_layout()
        plt.savefig(output_path, dpi=200)
        plt.close()
        return output_path
