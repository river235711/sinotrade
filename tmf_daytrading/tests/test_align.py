import os
import sys
import pandas as pd

k20 = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmf_20days_1m_kbars.csv")
shioaji_full = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmfr1_full_3months_1m.csv")
shioaji_full['ts'] = pd.to_datetime(shioaji_full['ts']) - pd.Timedelta(minutes=1)
shioaji_full['date'] = shioaji_full['ts'].dt.strftime('%Y-%m-%d')
shioaji_day = shioaji_full[(shioaji_full['ts'].dt.time >= pd.to_datetime('08:45').time()) & (shioaji_full['ts'].dt.time <= pd.to_datetime('13:45').time())].copy()

# Compare 2026-10-14
b20 = k20[k20['date'] == '2026-10-14'].reset_index(drop=True)
b_sh = shioaji_day[shioaji_day['date'] == '2026-10-14'].reset_index(drop=True)

print("b20 len:", len(b20), "b_sh len:", len(b_sh))
print("b20 first:", b20['timestamp'].iloc[0], b20['open'].iloc[0], b20['close'].iloc[0])
print("b_sh first:", b_sh['ts'].iloc[0], b_sh['open'].iloc[0], b_sh['close'].iloc[0])

# Check ORB calculation for 2026-10-14
b20['timestamp'] = pd.to_datetime(b20['timestamp'])
b20 = b20.set_index('timestamp')
orb_b20 = b20.between_time('09:00', '09:15')
print("b20 ORB high:", orb_b20['high'].max(), "low:", orb_b20['low'].min(), "bars:", len(orb_b20))

b_sh = b_sh.set_index('ts')
orb_sh = b_sh.between_time('09:00', '09:15')
print("b_sh ORB high:", orb_sh['high'].max(), "low:", orb_sh['low'].min(), "bars:", len(orb_sh))
