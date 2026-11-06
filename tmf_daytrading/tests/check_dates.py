import pandas as pd
df = pd.read_csv('data/tmf_20days_1m_kbars.csv')
dates = sorted(df['date'].unique())
print(f"Total dates: {len(dates)}")
print(f"Range: {dates[0]} to {dates[-1]}")
print(dates)
