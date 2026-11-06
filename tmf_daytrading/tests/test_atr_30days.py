import os
import sys
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import pandas as pd
from config import ORBStrategyConfig
from strategies.atr_scaler import config_for_date, load_daily_history

def main():
    data_dir = os.path.join(BASE_DIR, "data")
    kbars_30 = pd.read_csv(os.path.join(data_dir, "tmf_30days_1m_kbars.csv"))
    dates = sorted(kbars_30['date'].unique())
    print(f"Total dates to backtest: {len(dates)}")
    base_cfg = ORBStrategyConfig()
    daily = load_daily_history(data_dir)
    print(f"Daily history total rows: {len(daily)}, index range: {daily.index[0]} ~ {daily.index[-1]}")
    
    for d in [dates[0], dates[14], dates[-1]]:
        cfg, info = config_for_date(base_cfg, data_dir, d, daily=daily)
        print(f"Date {d}: ATR={info['atr']:.1f} (used {info['atr_days']} days), ratio={info['ratio']:.2f}, SL={cfg.stop_loss_pts:.0f}, MaxORB={cfg.max_orb_range_pts:.0f}")

if __name__ == "__main__":
    main()
