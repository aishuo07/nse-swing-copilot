"""Intraday (enter today, exit today) strategies testable on DAILY bars: 9 years of NSE bhavcopy,
point-in-time top-200 universe (survivorship-free). Zerodha MIS charges on ₹10k notional per trade +
5 bps slippage per side. Stops are checked pessimistically (if the day's range touches the stop, it was hit).
Portfolio: up to 5 trades a day, 20% of capital each (1x, no leverage) → daily return → CAGR.
"""
import sys
import numpy as np, pandas as pd
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / ".claude/skills/nse-swing-copilot/scripts"))
import common as c, bhav

SLIP = 0.0005
panel = bhav.load_panel()
mem = bhav.pit_universe(panel, 200)
start = pd.Timestamp("2017-11-01")
O, H, L, C = (pd.DataFrame({s: d[k] for s, d in panel.items()}).sort_index() for k in ("Open", "High", "Low", "Close"))
V = pd.DataFrame({s: d["Value"] for s, d in panel.items()}).sort_index()
dates = O.index[O.index >= start]
# membership mask
act = pd.DataFrame(False, index=O.index, columns=O.columns)
for r, m in mem.items():
    cols = [x for x in m if x in act.columns]
    act.loc[act.index > r, :] = False
    act.loc[act.index > r, cols] = True
act = act.loc[dates]
pC, pH, pL = C.shift(), H.shift(), L.shift()
rng = (H - L)
nr7 = rng.shift() <= rng.shift().rolling(7).min()
tr = pd.concat({"a": H - L, "b": (H - pC).abs(), "c": (L - pC).abs()}).groupby(level=1).max()
atr = tr.rolling(14).mean().shift()
gap = O / pC - 1
prevret = pC / C.shift(2) - 1
liq = (V.rolling(50).mean().shift() / 1e7) >= 10     # ≥ ₹10 cr average daily value


def cost_pct(e, x, side):
    q = max(1, int(10000 / e))
    buy, sell = (e, x) if side == 1 else (x, e)
    return c.charges("intraday", buy, sell, q)["total"] / (e * q)


def trades(name, side_df, entry, stop=None, rank=None):
    """side_df: +1/-1/0 per (date, stock). entry: price frame. stop: price frame or None."""
    m = (side_df != 0) & act & liq.reindex_like(act)
    rows = []
    idx = np.argwhere(m.loc[dates].to_numpy())
    S = side_df.loc[dates].to_numpy(); E = entry.loc[dates].to_numpy(); Cc = C.loc[dates].to_numpy()
    Hh = H.loc[dates].to_numpy(); Ll = L.loc[dates].to_numpy()
    St = None if stop is None else stop.loc[dates].to_numpy()
    Rk = None if rank is None else rank.loc[dates].to_numpy()
    cols = list(O.columns)
    for i, j in idx:
        sd, e = int(S[i, j]), E[i, j]
        if not np.isfinite(e) or e <= 0 or not np.isfinite(Cc[i, j]):
            continue
        e = e * (1 + SLIP * sd)
        x = Cc[i, j]
        if St is not None and np.isfinite(St[i, j]):
            st = St[i, j]
            if (sd == 1 and Ll[i, j] <= st) or (sd == -1 and Hh[i, j] >= st):
                x = st
        x = x * (1 - SLIP * sd)
        g = (x - e) / e * sd
        rows.append({"date": dates[i], "sym": cols[j], "side": sd, "gross": g, "net": g - cost_pct(e, x, sd),
                     "rank": 0 if Rk is None else Rk[i, j]})
    t = pd.DataFrame(rows)
    if t.empty:
        print(f"{name:<36} no trades"); return
    p = t.sort_values("rank", ascending=False).groupby("date").head(5)
    daily = p.groupby("date").net.sum() * 0.20
    eq = (1 + daily.reindex(dates, fill_value=0)).cumprod()
    yrs = (dates[-1] - dates[0]).days / 365.25
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = (eq / eq.cummax() - 1).min() * 100
    yr = t.groupby(t.date.dt.year).net.mean() * 100
    tstat = t.net.mean() / t.net.std() * np.sqrt(len(t))
    print(f"{name:<36}{len(t):7d}  gross {t.gross.mean()*100:+.3f}%  net {t.net.mean()*100:+.3f}%  "
          f"win {(t.net > 0).mean()*100:3.0f}%  t={tstat:+5.1f} | portfolio CAGR {cagr:+6.1f}% DD {dd:6.1f}% | "
          f"years +: {(yr > 0).sum()}/{len(yr)}")


print(f"9-yr daily-bar intraday tests: {dates[0]:%b %Y} → {dates[-1]:%b %Y}, PIT top-200, liquid (≥₹10 cr/day)\n")
z = pd.DataFrame(0, index=O.index, columns=O.columns)
trades("Gap fade (|gap| ≥ 2%)", z.mask(gap <= -0.02, 1).mask(gap >= 0.02, -1), O, rank=gap.abs())
trades("Gap-and-go (|gap| ≥ 2%)", z.mask(gap <= -0.02, -1).mask(gap >= 0.02, 1), O, rank=gap.abs())
trades("Gap fade, stop 1×ATR", z.mask(gap <= -0.02, 1).mask(gap >= 0.02, -1), O,
       stop=(O - atr).where(gap <= -0.02, O + atr), rank=gap.abs())
trades("Buy after a ≥4% down day (open→close)", z.mask(prevret <= -0.04, 1), O, rank=-prevret)
up_b = (H > pH) & nr7
dn_b = (L < pL) & nr7 & ~up_b
trades("NR7 breakout (stop = other side)", z.mask(up_b, 1).mask(dn_b, -1),
       np.maximum(O, pH).where(up_b, np.minimum(O, pL)), stop=pL.where(up_b, pH), rank=-rng.shift() / pC)
trades("Prev-day high/low breakout", z.mask(H > pH, 1).mask((L < pL) & ~(H > pH), -1),
       np.maximum(O, pH).where(H > pH, np.minimum(O, pL)), stop=(np.maximum(O, pH) - 0.5 * atr).where(H > pH, np.minimum(O, pL) + 0.5 * atr))
P = (pH + pL + pC) / 3; BC = (pH + pL) / 2; TC = 2 * P - BC
top, bot = np.maximum(TC, BC), np.minimum(TC, BC)
narrow = (top - bot) / pC < 0.0025
trades("Narrow CPR, open outside → trend", z.mask(narrow & (O > top), 1).mask(narrow & (O < bot), -1), O,
       stop=bot.where(O > top, top), rank=-(top - bot) / pC)
trades("Open→close every liquid stock (long)", z.mask(O > 0, 1), O)
