import pandas as pd
df = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmfr1_full_3months_1m.csv")
df['ts'] = pd.to_datetime(df['ts'])

sub = df[(df['ts'] >= '2026-10-14 08:40:00') & (df['ts'] <= '2026-10-14 09:20:00')]
print(sub[['ts', 'open', 'high', 'low', 'close', 'volume']].head(10))
