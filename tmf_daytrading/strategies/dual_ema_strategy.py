"""
雙均線順勢策略 (Dual EMA + VWAP)
"""
import pandas as pd
from typing import Dict, Any, List
from config import InstrumentConfig

class DualEMAStrategy:
    def __init__(self, fast_period: int = 5, slow_period: int = 20, inst_config: InstrumentConfig = None):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.inst_config = inst_config or InstrumentConfig()
        self.reset()

    def reset(self):
        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.trades: List[Dict[str, Any]] = []

    def prepare_indicators(self, kbars: pd.DataFrame) -> pd.DataFrame:
        df = kbars.copy()
        df['ema_fast'] = df['close'].ewm(span=self.fast_period, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=self.slow_period, adjust=False).mean()
        df['cum_vol'] = df['volume'].cumsum()
        df['cum_pv'] = (df['close'] * df['volume']).cumsum()
        df['vwap'] = df['cum_pv'] / df['cum_vol'].replace(0, 1)
        return df
