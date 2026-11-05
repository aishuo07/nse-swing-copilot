"""Intraday strategy lab on Yahoo 5-minute data (last ~60 sessions, current NIFTY 200).

Every strategy: one trade per stock per day, entry at the NEXT 5-min bar's open after the signal bar,
5 bps slippage per side, exits at stop / target / 15:15, Zerodha MIS charges, ₹200 risk per trade,
notional capped at ₹50k (5x on ₹10k). Same-bar stop+target → stop. "Portfolio" = first 5 signals
of each day by time (what one person could realistically take).
"""
import sys, pickle
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / ".claude/skills/nse-swing-copilot/scripts"))
import common as c, bhav

HERE = Path(__file__).parent
SLIP, RISK, MAXNOT = 0.0005, 200.0, 50000.0
EOD = pd.Timestamp("15:15").time()


def sim(day, i, side, stop, target, skip_first=False):
    """day: dict of numpy arrays; i: entry bar index. Returns (exit_px, entry_px, why) or None."""
    o, h, l, cl, tm = day["o"], day["h"], day["l"], day["c"], day["t"]
    if i >= len(o) or tm[i] >= EOD:
        return None
    e = o[i] * (1 + SLIP * side)
    if (e - stop) * side <= e * 0.0015:          # stop too close (< 0.15%) or already through
        return None
    for j in range(i, len(o)):
        if tm[j] >= EOD:
            return o[j], e, "eod"
        if skip_first and j == i:
            continue
        if side == 1 and l[j] <= stop:
            return min(o[j], stop), e, "stop"
        if side == -1 and h[j] >= stop:
            return max(o[j], stop), e, "stop"
        if target is not None and ((side == 1 and h[j] >= target) or (side == -1 and l[j] <= target)):
            return target, e, "target"
    return cl[-1], e, "eod"


def book(rows, sym, d, t, side, res, stop):
    if res is None:
        return
    px, e, why = res
    px = px * (1 - SLIP * side)
    R = (e - stop) * side
    qty = int(min(RISK / R, MAXNOT / e))
    if qty <= 0:
        return
    buy, sell = (e, px) if side == 1 else (px, e)
    ch = c.charges("intraday", buy, sell, qty)
    g = (px - e) * side * qty
    rows.append({"sym": sym, "date": d, "time": t, "side": side, "gross_r": g / (R * qty),
                 "net_r": (g - ch["total"]) / (R * qty), "net": g - ch["total"], "why": why})


def prep_days(df):
    out = {}
    for d, g in df.groupby(df.index.normalize()):
        g = g.between_time("09:15", "15:25")
        if len(g) < 60:
            continue
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        v = g["Volume"].replace(0, np.nan).fillna(1)
        vwap = (tp * v).cumsum() / v.cumsum()
        dev = (g["Close"] - vwap)
        sd = dev.expanding(min_periods=12).std()
        tr = pd.concat([g["High"] - g["Low"], (g["High"] - g["Close"].shift()).abs(),
                        (g["Low"] - g["Close"].shift()).abs()], axis=1).max(axis=1)
        out[d] = {"o": g["Open"].to_numpy(), "h": g["High"].to_numpy(), "l": g["Low"].to_numpy(),
                  "c": g["Close"].to_numpy(), "v": g["Volume"].to_numpy(), "t": [x.time() for x in g.index],
                  "vwap": vwap.to_numpy(), "sd": sd.to_numpy(), "atr5": tr.rolling(14, min_periods=5).mean().to_numpy(),
                  "e9": g["Close"].ewm(span=9).mean().to_numpy(), "e21": g["Close"].ewm(span=21).mean().to_numpy(),
                  "ts": list(g.index)}
    return out


def main():
    data = pickle.load(open(HERE / "orb_5m.pkl", "rb"))
    nifty = data.pop("NIFTY")
    panel = bhav.load_panel()
    days_n = prep_days(nifty)
    T = lambda s: pd.Timestamp(s).time()
    res = {k: [] for k in ["ORB5 stocks-in-play (Zarattini)", "ORB5 SIP best-case fills", "ORB15 stocks-in-play", "VWAP pullback (trend)",
                           "VWAP 2σ reversion", "EMA 9/21 cross", "Prev-day high/low break",
                           "Narrow CPR breakout", "ORB15 all stocks"]}
    # opening 5-min volume relative to 14 prior days, for the stocks-in-play filter
    rv = {}
    for s, df in data.items():
        first = df.between_time("09:15", "09:19")["Volume"]
        first.index = first.index.normalize()
        rv[s] = first / first.shift().rolling(14, min_periods=5).mean()
    rvdf = pd.DataFrame(rv)
    in_play = {d: set(r.dropna()[r.dropna() >= 1].nlargest(20).index) for d, r in rvdf.iterrows()}
    for s, df in data.items():
        if s not in panel:
            continue
        dd = panel[s]
        datr = c.atr(dd.rename(columns=str)).shift()   # prior-day 14-day ATR (no look-ahead)
        prev = dd[["High", "Low", "Close"]].shift()
        for d, day in prep_days(df).items():
            if d not in prev.index or np.isnan(prev.at[d, "Close"]):
                continue
            ph, pl, pc = prev.at[d, "High"], prev.at[d, "Low"], prev.at[d, "Close"]
            A = datr.get(d, np.nan)
            o, h, l, cl, tm = day["o"], day["h"], day["l"], day["c"], day["t"]
            nd = days_n.get(d)
            ndir = None if nd is None else np.sign(nd["c"] - nd["o"][0])   # NIFTY above/below its open
            ts = day["ts"]
            # 1) Zarattini ORB5: first 5-min candle direction, stop-entry at its high/low, stop 10% ATR, hold to EOD
            if s in in_play.get(d, ()) and not np.isnan(A) and abs(cl[0] - o[0]) > 0:
                side = 1 if cl[0] > o[0] else -1
                lvl = h[0] if side == 1 else l[0]
                for j in range(1, len(o)):
                    if tm[j] >= EOD: break
                    if (side == 1 and h[j] >= lvl) or (side == -1 and l[j] <= lvl):
                        e_bar = j  # stop order fills inside bar j at the level (or the open if gapped)
                        e = max(o[j], lvl) if side == 1 else min(o[j], lvl)
                        stop = e - side * 0.10 * A
                        day2 = dict(day); day2["o"] = o.copy(); day2["o"][j] = e
                        book(res["ORB5 stocks-in-play (Zarattini)"], s, d, tm[j], side, sim(day2, j, side, stop, None), stop)
                        book(res["ORB5 SIP best-case fills"], s, d, tm[j], side, sim(day2, j, side, stop, None, True), stop)
                        
                        break
            # 2/8) ORB15
            hi, lo = h[:3].max(), l[:3].min()
            for j in range(3, len(o)):
                if tm[j] >= T("11:00"): break
                side = 1 if cl[j] > hi else -1 if cl[j] < lo else 0
                if side:
                    stop = lo if side == 1 else hi
                    r = sim(day, j + 1, side, stop, None)
                    book(res["ORB15 all stocks"], s, d, tm[j], side, r, stop)
                    if s in in_play.get(d, ()):
                        book(res["ORB15 stocks-in-play"], s, d, tm[j], side, r, stop)
                    break
            # 3) VWAP pullback in NIFTY's direction after 10:00: touch VWAP, close back on the trend side
            if ndir is not None:
                for j in range(9, len(o)):
                    if tm[j] < T("10:00"): continue
                    if tm[j] >= T("14:30"): break
                    k = min(j, len(ndir) - 1); nside = ndir[k]
                    if nside == 0: continue
                    v_ = day["vwap"][j]; a5 = day["atr5"][j]
                    if np.isnan(a5): continue
                    if nside == 1 and l[j] <= v_ < cl[j] and cl[j - 6] > day["vwap"][j - 6]:
                        stop = min(l[j], v_ - a5)
                        book(res["VWAP pullback (trend)"], s, d, tm[j], 1, sim(day, j + 1, 1, stop, cl[j] + 2 * (cl[j] - stop)), stop); break
                    if nside == -1 and h[j] >= v_ > cl[j] and cl[j - 6] < day["vwap"][j - 6]:
                        stop = max(h[j], v_ + a5)
                        book(res["VWAP pullback (trend)"], s, d, tm[j], -1, sim(day, j + 1, -1, stop, cl[j] - 2 * (stop - cl[j])), stop); break
            # 4) VWAP 2σ reversion: close beyond 2σ band → fade, target VWAP, stop at 3σ
            for j in range(12, len(o)):
                if tm[j] < T("10:00"): continue
                if tm[j] >= T("14:30"): break
                sd, v_ = day["sd"][j], day["vwap"][j]
                if np.isnan(sd) or sd <= 0: continue
                if cl[j] < v_ - 2 * sd:
                    book(res["VWAP 2σ reversion"], s, d, tm[j], 1, sim(day, j + 1, 1, v_ - 3 * sd, v_), v_ - 3 * sd); break
                if cl[j] > v_ + 2 * sd:
                    book(res["VWAP 2σ reversion"], s, d, tm[j], -1, sim(day, j + 1, -1, v_ + 3 * sd, v_), v_ + 3 * sd); break
            # 5) EMA 9/21 cross after 09:45, stop 1.5 × 5-min ATR, target 2R
            e9, e21 = day["e9"], day["e21"]
            for j in range(7, len(o)):
                if tm[j] >= T("14:30"): break
                a5 = day["atr5"][j]
                if np.isnan(a5): continue
                up = e9[j] > e21[j] and e9[j - 1] <= e21[j - 1]
                dn = e9[j] < e21[j] and e9[j - 1] >= e21[j - 1]
                if up or dn:
                    side = 1 if up else -1
                    stop = cl[j] - side * 1.5 * a5
                    book(res["EMA 9/21 cross"], s, d, tm[j], side, sim(day, j + 1, side, stop, cl[j] + side * 3 * a5), stop); break
            # 6) Previous-day high/low break after 09:30, stop 0.25 × daily ATR, hold to EOD
            if not np.isnan(A):
                for j in range(3, len(o)):
                    if tm[j] >= T("14:30"): break
                    side = 1 if cl[j] > ph and cl[j - 1] <= ph else -1 if cl[j] < pl and cl[j - 1] >= pl else 0
                    if side:
                        stop = cl[j] - side * 0.25 * A
                        book(res["Prev-day high/low break"], s, d, tm[j], side, sim(day, j + 1, side, stop, None), stop); break
            # 7) Narrow CPR (width < 0.25% of price): close beyond TC/BC after 09:30, stop other side of CPR
            P = (ph + pl + pc) / 3; BC = (ph + pl) / 2; TC = 2 * P - BC
            top, bot = max(TC, BC), min(TC, BC)
            if (top - bot) / pc < 0.0025:
                for j in range(3, len(o)):
                    if tm[j] >= T("14:30"): break
                    side = 1 if cl[j] > top and o[0] < top * 1.01 else -1 if cl[j] < bot and o[0] > bot * 0.99 else 0
                    if side:
                        stop = bot if side == 1 else top
                        stop = stop - side * 0.001 * cl[j]
                        book(res["Narrow CPR breakout"], s, d, tm[j], side, sim(day, j + 1, side, stop, None), stop); break
    # NIFTY first-30-min → last-30-min (Gao et al.), traded as NIFTY futures (cost ≈ 0.03% round trip)
    g = []
    for d, nd in days_n.items():
        t = nd["t"]
        try:
            i930 = t.index(T("09:45")); i1445 = t.index(T("14:45"))
        except ValueError:
            continue
        r1 = nd["o"][i930] / nd["o"][0] - 1
        side = np.sign(r1)
        last = nd["c"][-1] / nd["o"][i1445] - 1
        g.append({"date": d, "first": r1, "last": last, "pnl_pct": side * last * 100 - 0.03})

    print(f"{len(data)} stocks · {len(days_n)} sessions ({min(days_n):%d-%b} → {max(days_n):%d-%b-%Y}) · ₹200 risk/trade\n")
    print(f"{'strategy':<34}{'trades':>7}{'grossR':>8}{'netR':>8}{'win%':>6}{'PF':>6} | portfolio: first 5/day on ₹10k")
    for k, rows in res.items():
        t = pd.DataFrame(rows)
        if t.empty:
            print(f"{k:<34} no trades"); continue
        w, ls = t[t.net > 0], t[t.net <= 0]
        pf = w.net.sum() / -ls.net.sum() if ls.net.sum() < 0 else np.inf
        p = t.sort_values(["date", "time"]).groupby("date").head(5).groupby("date").net.sum()
        half = len(p) // 2
        print(f"{k:<34}{len(t):7d}{t.gross_r.mean():+8.3f}{t.net_r.mean():+8.3f}{len(w)/len(t)*100:6.0f}{pf:6.2f} | "
              f"₹{p.sum():>7,.0f} total · {(p > 0).mean()*100:3.0f}% green days · halves ₹{p.iloc[:half].sum():,.0f} / ₹{p.iloc[half:].sum():,.0f}")
    G = pd.DataFrame(g)
    print(f"\nNIFTY first-30-min → last-30-min (futures, {len(G)} days): avg {G.pnl_pct.mean():+.3f}%/day net · "
          f"hit {(G.pnl_pct > 0).mean()*100:.0f}% · corr(first,last) {G['first'].corr(G['last']):+.2f}")


if __name__ == "__main__":
    main()
