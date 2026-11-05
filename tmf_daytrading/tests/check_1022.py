import pandas as pd
df = pd.read_csv('/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmf_20days_1m_kbars.csv')
sub = df[df['date'] == '2026-10-22'].copy()
sub['ts'] = pd.to_datetime(sub['timestamp'])
sub = sub.set_index('ts')
sub = sub.between_time('09:15', '10:00')
print(sub[['open', 'high', 'low', 'close']])
