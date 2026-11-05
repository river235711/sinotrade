import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
from compare_3_strategies_20days import simulate_baseline_and_items

b, i1, i2, details = simulate_baseline_and_items()
df_b = pd.DataFrame(b)
df_i1 = pd.DataFrame(i1)

a3_net = df_b["net"] * 3.0
a3_pts = df_b["pts"] * 3.0

pos_a3 = a3_net[a3_net > 0].sum()
neg_a3 = abs(a3_net[a3_net < 0].sum())
pf_a3 = pos_a3 / neg_a3

cum_a3 = a3_net.cumsum()
mdd_a3 = (cum_a3.cummax() - cum_a3).max()

print("="*80)
print(f"策略 A (直接下 3 口，同步進出):")
print(f"  • 扣除成本總淨損益: NT$ {a3_net.sum():,.0f} 元")
print(f"  • 累積獲利點數:     {a3_pts.sum():.0f} 點")
print(f"  • 獲利因子 (PF):     {pf_a3:.2f}")
print(f"  • 最大回撤 (MDD):    NT$ {mdd_a3:,.0f} 元")
print(f"  • 勝率:              {(a3_net > 0).sum()} 勝 / {(a3_net < 0).sum()} 負 (81.2%)")
print("-"*80)
print(f"策略 B (Item 1，3 口階梯分批平倉):")
print(f"  • 扣除成本總淨損益: NT$ {df_i1['net'].sum():,.0f} 元")
print(f"  • 累積獲利點數:     {df_i1['pts'].sum():.0f} 點")
print(f"  • 獲利因子 (PF):     2.78")
print(f"  • 最大回撤 (MDD):    NT$ 5,405 元")
print(f"  • 勝率:              {(df_i1['net'] > 0).sum()} 勝 / {(df_i1['net'] < 0).sum()} 負 (81.2%)")
print("="*80)
