import sys, pickle
import numpy as np, pandas as pd
exec(open("labdaily.py").read().split('print(f"9-yr daily-bar')[0])
# per-trade table for gap fade, no stop
m = (gap.abs() >= 0.02) & act & liq.reindex_like(act)
rows = []
G, Oo, Cc = gap.loc[dates], O.loc[dates], C.loc[dates]
val = (V.rolling(50).mean().shift() / 1e7).loc[dates]
for (d, s) in m.loc[dates].stack().loc[lambda x: x].index:
    g = G.at[d, s]; sd = 1 if g < 0 else -1
    e = Oo.at[d, s] * (1 + SLIP * sd); x = Cc.at[d, s] * (1 - SLIP * sd)
    r = (x - e) / e * sd
    rows.append((d, s, sd, g, val.at[d, s], r, r - cost_pct(e, x, sd)))
t = pd.DataFrame(rows, columns=["date", "sym", "side", "gap", "valcr", "gross", "net"])
f = lambda x: f"n={len(x):5d} net {x.net.mean()*100:+.3f}% win {(x.net>0).mean()*100:.0f}%"
print("long (gap-down buys):  ", f(t[t.side == 1])); print("short (gap-up shorts): ", f(t[t.side == -1]))
for lo, hi in [(10, 50), (50, 200), (200, 1e9)]:
    print(f"liquidity ₹{lo}-{hi if hi < 1e9 else '∞'} cr/day:", f(t[(t.valcr >= lo) & (t.valcr < hi)]))
for a, b in [(0.02, 0.03), (0.03, 0.05), (0.05, 1)]:
    print(f"|gap| {a*100:.0f}–{b*100 if b < 1 else '∞'}%:", f(t[(t.gap.abs() >= a) & (t.gap.abs() < b)]))
print("by year:", " | ".join(f"{y}: {g.net.mean()*100:+.2f}%" for y, g in t.groupby(t.date.dt.year)))
print("NIFTY-50-size names only (top 50 by value that day):")
t["rk"] = t.groupby("date").valcr.rank(ascending=False)
t.to_pickle("gapfade_trades.pkl")
# 5-minute check: does it survive if you enter at 09:15 auction open vs 09:20 / 09:30 bar open?
data = pickle.load(open("orb_5m.pkl", "rb")); data.pop("NIFTY", None)
res = {"09:15 (auction open)": [], "09:20": [], "09:30": [], "09:45": []}
for s, df in data.items():
    if s not in panel: continue
    pc = panel[s]["Close"].shift()
    for d, g in df.groupby(df.index.normalize()):
        g = g.between_time("09:15", "15:25")
        if len(g) < 60 or d not in pc.index or not np.isfinite(pc.at[d]): continue
        gp = g["Open"].iloc[0] / pc.at[d] - 1
        if abs(gp) < 0.02: continue
        sd = 1 if gp < 0 else -1
        xp = g.between_time("15:10", "15:14")["Close"]
        if xp.empty: continue
        x = xp.iloc[-1]
        for k in res:
            b = g.between_time(k[:5], k[:5])
            if b.empty: continue
            e = b["Open"].iloc[0] * (1 + SLIP * sd); xx = x * (1 - SLIP * sd)
            r = (xx - e) / e * sd
            res[k].append(r - cost_pct(e, xx, sd))
print("\nLast 60 days, 5-min data, gap fade exiting 15:10, entry at:")
for k, v in res.items():
    v = np.array(v); print(f"  {k:<22} n={len(v):4d} net {v.mean()*100:+.3f}% win {(v>0).mean()*100:.0f}%")
