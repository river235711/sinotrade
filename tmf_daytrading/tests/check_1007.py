import os
import sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import pandas as pd
from config import ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date, load_daily_history

# Compare 2026-10-07 with 20days kbars vs 30days kbars
k20 = pd.read_csv(os.path.join(BASE_DIR, "data", "tmf_20days_1m_kbars.csv"))
k30 = pd.read_csv(os.path.join(BASE_DIR, "data", "tmf_30days_1m_kbars.csv"))

print("20days data rows for 2026-10-07:", len(k20[k20['date'] == '2026-10-07']))
print("30days data rows for 2026-10-07:", len(k30[k30['date'] == '2026-10-07']))

# Check ATR config for 2026-10-07
base_cfg = ORBStrategyConfig()
cfg20, info20 = config_for_date(base_cfg, os.path.join(BASE_DIR, "data"), '2026-10-07')
print("ATR info:", info20)
