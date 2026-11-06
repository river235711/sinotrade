import pandas as pd
df = pd.read_csv("/mnt/c/Users/ting0/.gemini/antigravity/scratch/tmf_daytrading/data/tmfr1_full_3months_1m.csv")
df["ts"] = pd.to_datetime(df["ts"])

print("--- 2026-09-24 to 2026-09-30 ---")
sub1 = df[(df["ts"] >= "2026-09-24") & (df["ts"] <= "2026-09-30")]
for d, g in sub1.groupby(sub1["ts"].dt.date):
    print(d, g["ts"].min(), g["ts"].max(), len(g))

print("\n--- 2026-10-22 to 2026-10-28 ---")
sub2 = df[(df["ts"] >= "2026-10-22") & (df["ts"] <= "2026-10-28")]
for d, g in sub2.groupby(sub2["ts"].dt.date):
    print(d, g["ts"].min(), g["ts"].max(), len(g))
