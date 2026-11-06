import pandas as pd
df = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmfr1_full_3months_1m.csv")
df['ts'] = pd.to_datetime(df['ts'])

sub = df[(df['ts'] >= '2026-10-14 08:45:00') & (df['ts'] <= '2026-10-14 13:45:00')]
print("Shioaji bars between 08:45 and 13:45:", len(sub))
print("First bar:", sub['ts'].iloc[0])
print("Last bar:", sub['ts'].iloc[-1])

# If we shift ts - 1 minute:
sub_shifted = sub.copy()
sub_shifted['ts_start'] = sub_shifted['ts'] - pd.Timedelta(minutes=1)
print("Shifted first:", sub_shifted['ts_start'].iloc[0])
print("Shifted last:", sub_shifted['ts_start'].iloc[-1])
