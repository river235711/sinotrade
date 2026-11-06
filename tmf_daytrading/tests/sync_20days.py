import os
import pandas as pd

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data_dir = os.path.join(base_dir, "data")
df30 = pd.read_csv(os.path.join(data_dir, "tmf_30days_1m_kbars.csv"))
last20_dates = sorted(df30["date"].unique())[-20:]
df20_new = df30[df30["date"].isin(last20_dates)].copy()

out_path = os.path.join(data_dir, "tmf_20days_1m_kbars.csv")
df20_new.to_csv(out_path, index=False)
print(f"Updated {out_path}: {len(df20_new)} bars across {len(last20_dates)} dates ({last20_dates[0]} ~ {last20_dates[-1]})")
