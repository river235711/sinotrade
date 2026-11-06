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
    
    # Check how far back we can get 1-min kbars
    kb = api.kbars(tmfr1, start='2026-08-01', end='2026-11-06')
    df = pd.DataFrame({**kb})
    api.logout()
    
    df.columns = [c.lower() for c in df.columns]
    print(f"Total bars from 2026-08-01: {len(df)}")
    if not df.empty:
        df['ts'] = pd.to_datetime(df['ts'])
        print(f"Earliest: {df['ts'].min()}, Latest: {df['ts'].max()}")
        df_day = df[(df['ts'].dt.time >= pd.to_datetime('08:45').time()) & (df['ts'].dt.time <= pd.to_datetime('13:45').time())]
        dates = sorted(df_day['ts'].dt.strftime('%Y-%m-%d').unique())
        print(f"Total day sessions: {len(dates)}")
        print(f"First 5 dates: {dates[:5]}")
        print(f"Last 5 dates: {dates[-5:]}")

if __name__ == "__main__":
    main()
