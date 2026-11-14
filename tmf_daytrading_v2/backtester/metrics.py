"""
交易績效與統計指標計算模組 (V2)
"""
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from config import InstrumentConfig


class BacktestMetrics:
    @staticmethod
    def calculate(trades: List[Dict[str, Any]], inst_config: InstrumentConfig = None) -> Dict[str, Any]:
        cfg = inst_config or InstrumentConfig()
        if not trades:
            return {
                "total_trades": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "total_points": 0.0,
                "net_profit_ntd": 0.0,
                "profit_factor": 0.0,
                "avg_trade_ntd": 0.0,
                "max_drawdown_pts": 0.0,
                "max_drawdown_ntd": 0.0,
                "trades_df": pd.DataFrame(),
            }

        tdf = pd.DataFrame(trades)
        tdf["points_pnl"] = tdf["pnl_pts"]
        slippage_pts = cfg.slippage_points * 2
        tdf["net_points"] = tdf["points_pnl"] - slippage_pts
        tdf["gross_ntd"] = tdf["net_points"] * cfg.multiplier

        commission = cfg.commission_per_order * 2
        tax = (tdf["entry_price"] + tdf["exit_price"]) * cfg.multiplier * cfg.tax_rate
        tdf["net_ntd"] = tdf["gross_ntd"] - commission - tax

        total_trades = len(tdf)
        wins = tdf[tdf["net_ntd"] > 0]
        losses = tdf[tdf["net_ntd"] <= 0]

        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

        gross_win_ntd = wins["net_ntd"].sum() if len(wins) > 0 else 0.0
        gross_loss_ntd = abs(losses["net_ntd"].sum()) if len(losses) > 0 else 0.0
        profit_factor = (
            (gross_win_ntd / gross_loss_ntd)
            if gross_loss_ntd > 0
            else (999.0 if gross_win_ntd > 0 else 0.0)
        )

        total_points = tdf["points_pnl"].sum()
        total_net_ntd = tdf["net_ntd"].sum()

        tdf["cum_pts"] = tdf["points_pnl"].cumsum()
        tdf["peak_pts"] = tdf["cum_pts"].cummax()
        max_dd_pts = float((tdf["peak_pts"] - tdf["cum_pts"]).max()) if len(tdf) > 0 else 0.0

        tdf["cum_ntd"] = tdf["net_ntd"].cumsum()
        tdf["peak_ntd"] = tdf["cum_ntd"].cummax()
        tdf["dd_ntd"] = tdf["peak_ntd"] - tdf["cum_ntd"]
        max_dd_ntd = tdf["dd_ntd"].max() if len(tdf) > 0 else 0.0

        avg_trade_ntd = total_net_ntd / total_trades if total_trades > 0 else 0.0

        return {
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_points": total_points,
            "net_profit_ntd": total_net_ntd,
            "profit_factor": profit_factor,
            "max_drawdown_pts": max_dd_pts,
            "max_drawdown_ntd": max_dd_ntd,
            "avg_trade_ntd": avg_trade_ntd,
            "trades_df": tdf,
        }
