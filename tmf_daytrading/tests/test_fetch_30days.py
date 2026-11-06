import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import shioaji as sj
from config import ShioajiConfig

def main():
    cfg = ShioajiConfig()
    api = sj.Shioaji()
    api.login(api_key=cfg.api_key, secret_key=cfg.secret_key)
    tmfr1 = [c for c in api.Contracts.Futures.TMF if c.code == 'TMFR1'][0]
    
    # Try fetching from 2026-09-10 to 2026-11-06
    kb = api.kbars(tmfr1, start='2026-09-10', end='2026-11-06')
    df = pd.DataFrame({**kb})
    api.logout()
    
    df.columns = [c.lower() for c in df.columns]
    df['ts'] = pd.to_datetime(df['ts'])
    
    df_day = df[(df['ts'].dt.time >= pd.to_datetime('08:45').time()) & (df['ts'].dt.time <= pd.to_datetime('13:45').time())]
    dates = sorted(df_day['ts'].dt.strftime('%Y-%m-%d').unique())
    print(f"Total day sessions found: {len(dates)}")
    if dates:
        print(f"Full range: {dates[0]} to {dates[-1]}")
        print(f"Last 30 dates: {dates[-30:] if len(dates) >= 30 else dates}")

if __name__ == "__main__":
    main()
