#!/usr/bin/env python3
"""Portfolio backtest of the skill's setup families on NSE stocks, with Zerodha charges + slippage.

Strategies (long only, signals at the close, fills at the next open, gap > 3% above signal skipped):
  breakout  Stage-2 trend template + RS ≥ 70 + tight 20-day base, close > prior 20-day high on
            ≥1.5× 50d volume. Stop 10-day low −1% (≤8%). Breakeven at +1R, then 2×ATR trail from +2R.
            (Minervini/O'Neil — India breakout-planner, tradermonty vcp-screener)
  burst     Stockbee 4% momentum burst: +4% close, volume > yesterday, close in top 30% of range,
            <10% gain in prior 3 days, prior 20-day range ≤ 20%, above 50 DMA. Stop = signal-day low.
            Exit after 5 bars or stop; breakeven at +1R. (tradermonty stockbee-momentum-burst-screener)
  pullback  Close > rising 100 EMA, low touches 20 EMA and closes back above it, RS ≥ 70.
            Stop 2×ATR, target 2R, exit on close under 100 EMA, max 20 bars. (India backtest-lab ema-pullback)
  high52    Close above prior 252-bar high, RS ≥ 70. Stop 2.5×ATR, exit on close under 50 DMA.
            (India backtest-lab 52w-high)

Market filters: none | ma50 (NIFTY > 50 DMA) | ma50_200 (NIFTY > 50 and 200 DMA)

  backtest.py                                  # primary high52 system, 5y, ma50_200 vs none
  backtest.py --matrix --years 9               # every strategy × filter, split into two halves
  backtest.py --strategy burst --market ma50 --years 9
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import common as c

STRATS = ("breakout", "burst", "pullback", "high52")
MARKETS = ("none", "ma50", "ma50_200", "leaders90", "leaders95")


def prep(df):
    d = df.copy()
    cl, v = d["Close"], d["Volume"]
    d["s50"], d["s150"], d["s200"] = c.sma(cl, 50), c.sma(cl, 150), c.sma(cl, 200)
    d["s200_22"] = d["s200"].shift(22)
    d["e20"], d["e100"] = c.ema(cl, 20), c.ema(cl, 100)
    d["e100_10"] = d["e100"].shift(10)
    d["h52"] = d["High"].rolling(252).max()
    d["l52"] = d["Low"].rolling(252).min()
    d["hi252p"] = d["High"].rolling(252).max().shift()
    d["atr"] = c.atr(d)
    d["atr10"], d["atr50"] = c.atr(d, 10), c.atr(d, 50)
    d["v50"] = v.rolling(50).mean().shift()
    d["vprev"] = v.shift()
    d["hi20"] = d["High"].rolling(20).max().shift()
    d["lo20"] = d["Low"].rolling(20).min().shift()
    d["lo10p"] = d["Low"].rolling(10).min().shift()
    d["chg"] = cl.pct_change()
    d["ret3p"] = cl.shift() / cl.shift(4) - 1
    rng = (d["High"] - d["Low"]).replace(0, np.nan)
    d["clv"] = (cl - d["Low"]) / rng
    d["rsraw"] = (0.4 * cl / cl.shift(63) + 0.2 * cl / cl.shift(126) + 0.2 * cl / cl.shift(189)
                  + 0.2 * cl / cl.shift(252))
    d["turn_cr"] = (cl * v).rolling(50).mean() / 1e7
    return d


def signal(strat, r, rs):
    cl = r["Close"]
    if r["turn_cr"] < 10 or np.isnan(r["s200_22"]):
        return False
    if strat == "breakout":
        if rs < 70:
            return False
        tt = (cl > r["s150"] and cl > r["s200"] and r["s150"] > r["s200"] and r["s200"] > r["s200_22"]
              and r["s50"] > r["s150"] and cl > r["s50"] and cl >= 1.3 * r["l52"] and cl >= 0.75 * r["h52"])
        return bool(tt and (r["hi20"] / r["lo20"] - 1) * 100 <= 15 and r["atr10"] / r["atr50"] < 1.035
                    and cl > r["hi20"] and r["Volume"] >= 1.5 * r["v50"])
    if strat == "burst":
        return bool(r["chg"] >= 0.04 and r["Volume"] > r["vprev"] and r["clv"] >= 0.7 and r["ret3p"] < 0.10
                    and (r["hi20"] / r["lo20"] - 1) <= 0.20 and cl > r["s50"])
    if strat == "pullback":
        return bool(rs >= 70 and cl > r["e100"] and r["e100"] > r["e100_10"] and r["Low"] <= r["e20"]
                    and cl > r["e20"])
    if strat == "high52":
        return bool(rs >= 70 and cl > r["hi252p"])
    return False


def initial_stop(strat, d, t, o, sig_row):
    a = sig_row["atr"]
    if strat == "breakout":
        return max(sig_row["lo10p"] * 0.99 if not np.isnan(sig_row["lo10p"]) else 0, o * 0.92)
    if strat == "burst":
        return max(sig_row["Low"], o * 0.92)
    if strat == "pullback":
        return o - 2 * a
    return o - 2.5 * a


def run(data, nifty, start, end, strat, market, a):
    dates = nifty.loc[start:end].index
    ncl = nifty["Close"]
    n50, n200 = c.sma(ncl, 50), c.sma(ncl, 200)
    rs = pd.DataFrame({s: d["rsraw"] for s, d in data.items()}).reindex(dates)
    rsr = rs.rank(axis=1, pct=True) * 98 + 1
    slip = a.slippage_bps / 1e4
    cash = a.capital
    pos, trades, curve, pending = {}, [], [], []
    for t in dates:
        # fills at the open
        for s, sig_close, sig_row, rf in pending:
            d = data[s]
            if t not in d.index or s in pos or len(pos) >= a.max_pos:
                continue
            o = d.at[t, "Open"] * (1 + slip)
            if np.isnan(o) or o > sig_close * 1.03:
                continue
            stop = initial_stop(strat, d, t, o, sig_row)
            if not (stop < o) or (o - stop) / o < 0.005:
                continue
            mtm = cash + sum(p["qty"] * data[k]["Close"].asof(t) for k, p in pos.items())
            qty = int(min(mtm * a.risk * rf / 100 / (o - stop), mtm * a.max_pos_pct / 100 / o, cash / o))
            if qty <= 0:
                continue
            cash -= qty * o
            pos[s] = {"entry": o, "stop": stop, "init": stop, "qty": qty, "hc": o, "date": t, "bars": 0}
        pending = []
        # manage
        for s in list(pos):
            d = data[s]
            if t not in d.index:
                continue
            p = pos[s]
            p["bars"] += 1
            r = d.loc[t]
            o, lo, hi, cl = r["Open"], r["Low"], r["High"], r["Close"]
            R1 = p["entry"] - p["init"]
            px = None
            if lo <= p["stop"]:
                px = min(o, p["stop"]) * (1 - slip)
            elif strat == "pullback" and hi >= p["entry"] + 2 * R1:
                px = max(o, p["entry"] + 2 * R1) * (1 - slip)
            elif strat == "burst" and p["bars"] >= 5:
                px = cl * (1 - slip)
            elif strat == "pullback" and (p["bars"] >= 20 or cl < r["e100"]):
                px = cl * (1 - slip)
            elif strat == "high52" and cl < r["s50"]:
                px = cl * (1 - slip)
            elif p["bars"] >= a.max_hold:
                px = cl * (1 - slip)
            if px is not None:
                ch = c.charges("delivery", p["entry"], px, p["qty"])
                cash += p["qty"] * px - ch["total"]
                trades.append({"symbol": s, "entry_date": p["date"], "exit_date": t, "entry": p["entry"],
                               "exit": px, "pnl": ch["net_pnl"], "r": ch["net_pnl"] / (R1 * p["qty"]),
                               "bars": p["bars"]})
                del pos[s]
                continue
            p["hc"] = max(p["hc"], cl)
            if p["hc"] >= p["entry"] + R1:
                p["stop"] = max(p["stop"], p["entry"] * 1.003)
            if strat == "breakout" and p["hc"] >= p["entry"] + 2 * R1:
                p["stop"] = max(p["stop"], p["hc"] - 2 * r["atr"])
        # signals at the close
        trend_on = ncl.at[t] > n50.at[t] and ncl.at[t] > n200.at[t]
        ok = (market == "none"
              or (market == "ma50" and ncl.at[t] > n50.at[t])
              or (market == "ma50_200" and trend_on)
              or market.startswith("leaders"))
        # leaders*: when the NIFTY filter is OFF, still allow the very strongest stocks
        # (RS >= 90/95) at half risk and at most 2 open positions.
        rs_min = 0 if (trend_on or not market.startswith("leaders")) else int(market[-2:])
        rf = 1.0 if rs_min == 0 else 0.5
        cap = a.max_pos if rs_min == 0 else min(a.max_pos, 2)
        if ok and len(pos) < cap:
            cands = []
            for s, d in data.items():
                if s in pos or t not in d.index:
                    continue
                rr = rsr.at[t, s] if s in rsr.columns else np.nan
                if np.isnan(rr) or rr < rs_min:
                    continue
                row = d.loc[t]
                if signal(strat, row, rr):
                    cands.append((s, row["Close"], row, rr))
            cands.sort(key=lambda x: -x[3])
            pending = [(s, cl_, row, rf) for s, cl_, row, _ in cands[: cap - len(pos)]]
        curve.append((t, cash + sum(p["qty"] * data[k]["Close"].asof(t) for k, p in pos.items())))
    return pd.Series(dict(curve)), pd.DataFrame(trades)


def metrics(eq, tr, nifty):
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 0.1)
    nf = nifty["Close"].loc[eq.index[0]:eq.index[-1]]
    m = {"cagr": ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100,
         "mdd": (eq / eq.cummax() - 1).min() * 100,
         "nifty_cagr": ((nf.iloc[-1] / nf.iloc[0]) ** (1 / yrs) - 1) * 100,
         "nifty_mdd": (nf / nf.cummax() - 1).min() * 100, "trades": len(tr)}
    if len(tr):
        w, l = tr[tr.pnl > 0], tr[tr.pnl <= 0]
        m.update(win=len(w) / len(tr) * 100, avg_r=tr.r.mean(),
                 pf=w.pnl.sum() / -l.pnl.sum() if l.pnl.sum() < 0 else float("inf"), hold=tr.bars.mean())
    else:
        m.update(win=0, avg_r=0, pf=0, hold=0)
    return m


def load(a):
    u = c.universe(a.universe)
    raw = c.history(list(u["Symbol"]) + ["NIFTY"], period=f"{a.years + 2}y", max_age_hours=24)
    nifty = raw.pop("NIFTY")
    data = {s: prep(d) for s, d in raw.items() if len(d) > 300}
    return data, nifty


def detail(name, eq, tr, nifty):
    m = metrics(eq, tr, nifty)
    print(f"\n{name}  ({eq.index[0]:%b %Y} → {eq.index[-1]:%b %Y})")
    print("-" * 74)
    print(f"System : CAGR {m['cagr']:6.1f}%   max drawdown {m['mdd']:6.1f}%   final {c.inr(eq.iloc[-1])}")
    print(f"NIFTY  : CAGR {m['nifty_cagr']:6.1f}%   max drawdown {m['nifty_mdd']:6.1f}%   (buy & hold, price only)")
    if tr.empty:
        return
    print(f"Trades {m['trades']} · win {m['win']:.0f}% · avg {m['avg_r']:+.2f}R · profit factor {m['pf']:.2f} · "
          f"avg hold {m['hold']:.0f} bars")
    yr = tr.groupby(tr.exit_date.dt.year).agg(n=("r", "size"), avgR=("r", "mean"))
    print("By year: " + " | ".join(f"{y}: {int(r.n)}t {r.avgR:+.2f}R" for y, r in yr.iterrows()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--universe", default="nifty200")
    ap.add_argument("--capital", type=float, default=500000)
    ap.add_argument("--risk", type=float, default=1.0)
    ap.add_argument("--max-pos", type=int, default=6)
    ap.add_argument("--max-pos-pct", type=float, default=20.0, help="max %% of equity per stock")
    ap.add_argument("--max-hold", type=int, default=120)
    ap.add_argument("--slippage-bps", type=float, default=10)
    ap.add_argument("--strategy", choices=STRATS, default="high52")
    ap.add_argument("--market", choices=MARKETS)
    ap.add_argument("--matrix", action="store_true", help="every strategy × market filter, two halves")
    a = ap.parse_args()
    data, nifty = load(a)
    end = nifty.index[-1]
    start = end - pd.DateOffset(years=a.years)
    print(f"Backtest: {len(data)} {a.universe} stocks · {a.years}y · risk {a.risk}%/trade · max {a.max_pos} "
          f"positions · Zerodha charges + {a.slippage_bps:g} bps slippage each side")
    print("CAVEAT: current index members only (survivorship bias) → real results would be worse.")
    if a.matrix:
        mid = start + (end - start) / 2
        print(f"\nFirst half {start:%b %Y}–{mid:%b %Y} | second half {mid:%b %Y}–{end:%b %Y}")
        print(f"{'strategy':<9}{'market':<9}| {'CAGR':>6}{'maxDD':>7}{'trd':>5}{'win%':>5}{'avgR':>6}{'PF':>5} "
              f"| {'CAGR':>6}{'maxDD':>7}{'trd':>5}{'win%':>5}{'avgR':>6}{'PF':>5} | full CAGR")
        rows = []
        for st in STRATS:
            for mk in MARKETS:
                halves = []
                for s0, e0 in ((start, mid), (mid, end)):
                    eq, tr = run(data, nifty, s0, e0, st, mk, a)
                    halves.append(metrics(eq, tr, nifty))
                eqf, trf = run(data, nifty, start, end, st, mk, a)
                full = metrics(eqf, trf, nifty)
                rows.append((st, mk, halves, full))
                h1, h2 = halves
                print(f"{st:<9}{mk:<9}| {h1['cagr']:6.1f}{h1['mdd']:7.1f}{h1['trades']:5d}{h1['win']:5.0f}"
                      f"{h1['avg_r']:+6.2f}{h1['pf']:5.2f} | {h2['cagr']:6.1f}{h2['mdd']:7.1f}{h2['trades']:5d}"
                      f"{h2['win']:5.0f}{h2['avg_r']:+6.2f}{h2['pf']:5.2f} | {full['cagr']:6.1f}% (DD {full['mdd']:.0f}%)",
                      flush=True)
        h = rows[0][2]
        print(f"{'NIFTY':<18}| {h[0]['nifty_cagr']:6.1f}{h[0]['nifty_mdd']:7.1f}{'':>21} | "
              f"{h[1]['nifty_cagr']:6.1f}{h[1]['nifty_mdd']:7.1f}{'':>21} | {rows[0][3]['nifty_cagr']:6.1f}%")
    else:
        for mk in ([a.market] if a.market else ["ma50_200", "none"]):
            eq, tr = run(data, nifty, start, end, a.strategy, mk, a)
            detail(f"{a.strategy.upper()} · market filter: {mk}", eq, tr, nifty)
            tr.to_csv(c.STATE / f"backtest_{a.strategy}_{mk}.csv", index=False)
    print("\nPast results don't guarantee future returns. Paper-trade a system for 1–2 months before real money.")
