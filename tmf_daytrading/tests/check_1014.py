import os
import sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import pandas as pd
from config import ORBStrategyConfig
from strategies.orb_strategy import ORBStrategy
from strategies.atr_scaler import config_for_date, load_daily_history

# Let's inspect 2026-10-14
k20 = pd.read_csv(os.path.join(BASE_DIR, "data", "tmf_20days_1m_kbars.csv"))
k30 = pd.read_csv(os.path.join(BASE_DIR, "data", "tmf_30days_1m_kbars.csv"))

b20 = k20[k20['date'] == '2026-10-14']
b30 = k30[k30['date'] == '2026-10-14']

print("20days 2026-10-14 first 3 bars:")
print(b20.head(3))
print("30days 2026-10-14 first 3 bars:")
print(b30.head(3))

# Check ATR
base_cfg = ORBStrategyConfig()
cfg20, info20 = config_for_date(base_cfg, os.path.join(BASE_DIR, "data"), '2026-10-14')
print("ATR config:", info20)
print("SL:", cfg20.stop_loss_pts, "BE:", cfg20.breakeven_trigger_pts, "Lock:", cfg20.breakeven_lock_pts)
print("Tier 1.5:", getattr(cfg20, 'tier1_5_trigger_pts', None), "Tier 2:", cfg20.tier2_trigger_pts)
