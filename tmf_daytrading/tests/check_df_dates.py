import pandas as pd
df20 = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmf_20days_1m_kbars.csv")
print("df20 dates count:", len(df20["date"].unique()))
print("df20 range:", sorted(df20["date"].unique())[0], "to", sorted(df20["date"].unique())[-1])

df30 = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmf_30days_1m_kbars.csv")
print("df30 dates count:", len(df30["date"].unique()))
last20 = sorted(df30["date"].unique())[-20:]
print("df30 last 20:", last20[0], "to", last20[-1])
