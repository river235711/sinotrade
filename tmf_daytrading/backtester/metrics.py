"""
交易績效與統計指標計算模組
"""
import pandas as pd
import numpy as np
from typing import Dict, Any, List
from config import InstrumentConfig

class BacktestMetrics:
    @staticmethod
    def calculate(trades: List[Dict[str, Any]], inst_config: InstrumentConfig = None) -> Dict[str, Any]:
        cfg = inst_config or InstrumentConfig()
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_points": 0.0,
                "net_profit_ntd": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_pts": 0.0,
                "max_drawdown_ntd": 0.0,
                "trades_df": pd.DataFrame()
            }

        tdf = pd.DataFrame(trades)
        
        # 成本計算 (來回手續費 + 來回期交稅 + 單邊滑價)
        tdf['points_pnl'] = tdf['pnl_pts']
        # 扣除滑價 (進出各滑 1 點 = 共扣 2 點)
        slippage_pts = cfg.slippage_points * 2
        tdf['net_points'] = tdf['points_pnl'] - slippage_pts
        
        # 台幣損益 = 點數 * 每點金額
        tdf['gross_ntd'] = tdf['net_points'] * cfg.multiplier
        
        # 手續費 (進場 + 出場)
        commission = cfg.commission_per_order * 2
        # 期交稅 (進場稅 + 出場稅)
        tax = (tdf['entry_price'] + tdf['exit_price']) * cfg.multiplier * cfg.tax_rate
        tdf['net_ntd'] = tdf['gross_ntd'] - commission - tax
        
        # 統計項目
        total_trades = len(tdf)
        wins = tdf[tdf['net_ntd'] > 0]
        losses = tdf[tdf['net_ntd'] <= 0]
        
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0
        
        gross_win_ntd = wins['net_ntd'].sum() if len(wins) > 0 else 0.0
        gross_loss_ntd = abs(losses['net_ntd'].sum()) if len(losses) > 0 else 0.0
        profit_factor = (gross_win_ntd / gross_loss_ntd) if gross_loss_ntd > 0 else (999.0 if gross_win_ntd > 0 else 0.0)
        
        total_points = tdf['points_pnl'].sum()
        total_net_ntd = tdf['net_ntd'].sum()
        
        # 計算權益曲線與最大回撤 (MDD)
        tdf['cum_ntd'] = tdf['net_ntd'].cumsum()
        tdf['peak_ntd'] = tdf['cum_ntd'].cummax()
        tdf['dd_ntd'] = tdf['peak_ntd'] - tdf['cum_ntd']
        max_dd_ntd = tdf['dd_ntd'].max() if len(tdf) > 0 else 0.0
        
        avg_trade_ntd = total_net_ntd / total_trades if total_trades > 0 else 0.0
        
        return {
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_points": total_points,
            "net_profit_ntd": total_net_ntd,
            "profit_factor": profit_factor,
            "max_drawdown_ntd": max_dd_ntd,
            "avg_trade_ntd": avg_trade_ntd,
            "trades_df": tdf
        }
