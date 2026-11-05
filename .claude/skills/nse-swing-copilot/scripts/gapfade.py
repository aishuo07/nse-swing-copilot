#!/usr/bin/env python3
"""PAPER-ONLY intraday gap-fade test. It never places orders and never asks you to.

Rule (frozen 5-Nov-2026, same as the 9-year bhavcopy backtest in references/sources.md):
  Universe : top 200 NSE stocks by 126-day average traded value, ≥ ₹10 cr/day over 50 days.
  Signal   : today's open vs yesterday's close. Gap ≤ −2% → paper BUY, gap ≥ +2% → paper SHORT.
             Biggest 5 gaps by size. Skip stocks with a split/bonus/demerger ex-date today.
  Exit     : same day close. Variant: stop at 1×ATR(14) from the open.
  Costs    : Zerodha intraday (MIS) charges + 0.05% slippage each side. Paper capital ₹10,000,
             5 slots × ₹2,000, no leverage.

Two results are logged per trade, because the backtest edge depends on the 09:15 auction price:
  auction  — official open → official close (NSE bhavcopy). What the backtest assumed.
  real920  — 09:20 5-min bar open → 15:15 bar open (Yahoo). What you could actually get by hand.

Commands:
  gapfade.py morning            # ~09:20: phone alert with today's paper picks (Yahoo prices)
  gapfade.py evening [--date D] # after ~19:00: official results from bhavcopy → data/paper/gapfade.csv
                                #   (also fills any missed day in the last 10 trading days)
  gapfade.py stats              # running results vs the pass/fail rule
  gapfade.py evening --date 2026-11-04 --dry   # print a day without logging it

Pass/fail (decided before the test, review after 60 trading days ≈ 250 trades):
  keep testing only if real920 averages ≥ +0.15% net per trade with t ≥ 2. Otherwise drop it.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

import common as c
import bhav

PAPER = c.DATA / "paper"
PAPER.mkdir(parents=True, exist_ok=True)
LOG = PAPER / "gapfade.csv"
GAP, TOPN, SLIP, CAPITAL, SLOT = 0.02, 5, 0.0005, 10000.0, 0.20
UNIV, MIN_VAL_CR, LOOKBACK = 200, 10.0, 126
PASS_MEAN, PASS_T, REVIEW_DAYS = 0.15, 2.0, 60


# ------------------------------------------------------------------ data
def trading_days_before(d: date, n: int) -> list[date]:
    out, x = [], d
    while len(out) < n:
        x = c.prev_trading_day(x)
        out.append(x)
    return out[::-1]


def day_frame(d: date) -> pd.DataFrame | None:
    st = bhav.fetch(d)
    p = bhav.BHAV / f"{d:%Y%m%d}.zip"
    if not p.exists():
        return None if st != "fail" else None
    return bhav.parse(p).set_index("sym")


def history_before(d: date) -> dict:
    """Universe, liquidity and ATR as of the close before d, from the last 126 bhavcopies."""
    days = trading_days_before(d, LOOKBACK)
    frames = []
    for x in days:
        f = day_frame(x)
        if f is not None:
            frames.append(f.assign(date=pd.Timestamp(x)))
    if len(frames) < 60:
        c.die(f"only {len(frames)} bhavcopies before {d}; run `bhav.py download` first.")
    raw = pd.concat(frames).reset_index()
    val = raw.pivot_table(index="date", columns="sym", values="val")
    avg126 = val.mean()
    avg50 = val.iloc[-50:].mean()
    members = set(avg126[val.count() >= 100].nlargest(UNIV).index)
    liquid = set(avg50[avg50 / 1e7 >= MIN_VAL_CR].index)
    H, L, C = (raw.pivot_table(index="date", columns="sym", values=k) for k in ("h", "l", "c"))
    tr = pd.concat({"a": H - L, "b": (H - C.shift()).abs(), "c": (L - C.shift()).abs()}).groupby(level=1).max()
    atr = tr.iloc[-14:].mean()
    return {"universe": members & liquid, "atr": atr, "last_close": C.iloc[-1]}


def ca_today(d: date) -> set:
    """Symbols with a split / bonus / demerger going ex on d (they gap for no trading reason)."""
    out = set()
    for x in trading_days_before(d, 3):
        bhav.fetch_bc(x)
        p = bhav.BC / f"{x:%Y%m%d}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p, dtype=str, on_bad_lines="skip", encoding_errors="replace")
        df.columns = [k.strip().upper() for k in df.columns]
        if not {"SYMBOL", "EX_DT", "PURPOSE"} <= set(df.columns):
            continue
        df = df.dropna(subset=["SYMBOL", "EX_DT", "PURPOSE"])
        ex = pd.to_datetime(df["EX_DT"].str.strip(), format="%d/%m/%Y", errors="coerce")
        ex = ex.fillna(pd.to_datetime(df["EX_DT"].str.strip(), format="%Y-%m-%d", errors="coerce"))
        hit = (ex.dt.date == d) & df["PURPOSE"].map(lambda s: bhav.ca_factor(s) is not None)
        out |= set(df.loc[hit, "SYMBOL"].str.strip())
    return out


def picks(opens: pd.Series, prev: pd.Series, hist: dict, skip: set) -> pd.DataFrame:
    u = [s for s in hist["universe"] if s in opens.index and s in prev.index and s not in skip]
    gap = (opens[u] / prev[u] - 1).dropna()
    gap = gap[(gap.abs() >= GAP) & (gap.abs() < 0.20)]       # >20% = data error or news halt
    top = gap.reindex(gap.abs().sort_values(ascending=False).index).head(TOPN)
    return pd.DataFrame({"sym": top.index, "gap": top.values,
                         "side": np.where(top.values < 0, 1, -1), "prev": prev[top.index].values})


def net_pct(entry: float, exit_: float, side: int) -> float:
    e, x = entry * (1 + SLIP * side), exit_ * (1 - SLIP * side)
    q = max(1, int(CAPITAL * SLOT / e))
    buy, sell = (e, x) if side == 1 else (x, e)
    cost = c.charges("intraday", buy, sell, q)["total"]
    return ((x - e) * side * q - cost) / (e * q) * 100


def yahoo_5m(syms: list[str], d: date) -> dict:
    """{sym: (09:20 bar open, 15:15 bar open)} for day d, if Yahoo has it (last ~60 days)."""
    import yfinance as yf
    if not syms:
        return {}
    tick = {c.yf_symbol(s): s for s in syms}
    raw = yf.download(list(tick), period="60d" if (date.today() - d).days > 5 else "5d", interval="5m",
                      group_by="ticker", auto_adjust=False, progress=False, threads=True)
    out = {}
    for t, s in tick.items():
        try:
            g = (raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw).dropna(subset=["Open"])
            g.index = g.index.tz_convert("Asia/Kolkata").tz_localize(None)
            g = g[g.index.date == d]
            a, b = g.at_time("09:20"), g.at_time("15:15")
            if len(a) and len(b):
                out[s] = (float(a["Open"].iloc[0]), float(b["Open"].iloc[0]))
        except Exception:
            continue
    return out


# ------------------------------------------------------------------ commands
def notify(title, msg):
    try:
        import alerts
        alerts.notify(title, msg)
    except Exception as e:
        print(f"(notify failed: {e})\n{title}: {msg}")


def morning(a):
    d = c.now_ist().date()
    if not c.is_trading_day(d):
        print("Not a trading day."); return
    import yfinance as yf
    hist = history_before(d)
    syms = sorted(hist["universe"])
    raw = yf.download([c.yf_symbol(s) for s in syms], period="1d", interval="1m", group_by="ticker",
                      auto_adjust=False, progress=False, threads=True)
    opens = {}
    for s in syms:
        try:
            g = raw[c.yf_symbol(s)].dropna(subset=["Open"])
            g.index = g.index.tz_convert("Asia/Kolkata").tz_localize(None)
            g = g[g.index.date == d]
            if len(g):
                opens[s] = float(g["Open"].iloc[0])
        except Exception:
            continue
    if len(opens) < 50:
        print(f"Only {len(opens)} live opens from Yahoo; skipping the alert (evening log still runs).")
        return
    pk = picks(pd.Series(opens), hist["last_close"], hist, ca_today(d))
    if pk.empty:
        notify("📝 PAPER gap fade", f"{d:%d-%b}: no stock gapped ≥2% today. Nothing to watch.")
        return
    lines = [f"{'BUY' if r.side == 1 else 'SHORT'} {r.sym} (gap {r.gap * 100:+.1f}%)" for r in pk.itertuples()]
    notify("📝 PAPER gap fade — do NOT trade",
           f"{d:%d-%b} paper picks: " + " · ".join(lines) + ". Exit at close. Results logged tonight.")


def evaluate(d: date) -> pd.DataFrame | None:
    today = day_frame(d)
    if today is None:
        return None
    hist = history_before(d)
    pk = picks(today["o"], today["prev"], hist, ca_today(d))
    if pk.empty:
        return pd.DataFrame([{"date": d.isoformat(), "sym": "-", "side": 0}])
    y = yahoo_5m(list(pk.sym), d)
    rows = []
    for r in pk.itertuples():
        t, side = today.loc[r.sym], int(r.side)
        o, h, l, cl = float(t.o), float(t.h), float(t.l), float(t.c)
        atr = float(hist["atr"].get(r.sym, np.nan))
        stop = o - side * atr
        hit = np.isfinite(atr) and ((side == 1 and l <= stop) or (side == -1 and h >= stop))
        e920, x1515 = y.get(r.sym, (np.nan, np.nan))
        rows.append({"date": d.isoformat(), "sym": r.sym, "side": side, "gap_pct": round(r.gap * 100, 2),
                     "prev": r.prev, "open": o, "high": h, "low": l, "close": cl,
                     "auction_pct": round(net_pct(o, cl, side), 3),
                     "atrstop_pct": round(net_pct(o, stop if hit else cl, side), 3),
                     "e920": e920, "x1515": x1515,
                     "real920_pct": round(net_pct(e920, x1515, side), 3) if np.isfinite(e920) else np.nan})
    return pd.DataFrame(rows)


def load_log() -> pd.DataFrame:
    return pd.read_csv(LOG) if LOG.exists() else pd.DataFrame(columns=["date"])


def evening(a):
    target = date.fromisoformat(a.date) if a.date else c.now_ist().date()
    if a.dry:
        res = evaluate(target)
        print("bhavcopy not out yet" if res is None else res.to_string(index=False))
        return
    log = load_log()
    done = set(log["date"].astype(str))
    days = [x for x in trading_days_before(target, 9) + [target]
            if c.is_trading_day(x) and x >= START and x.isoformat() not in done]
    new = []
    for x in days:
        res = evaluate(x)
        if res is None:
            print(f"{x}: bhavcopy not published yet; will retry next run.")
            continue
        new.append(res)
    if not new:
        print("Nothing new to log."); return
    add = pd.concat(new)
    pd.concat([log, add]).to_csv(LOG, index=False)
    for x, g in add.groupby("date"):
        g = g[g.side != 0]
        if g.empty:
            notify("📝 PAPER gap fade", f"{x}: no ≥2% gaps, no paper trades.")
            continue
        tot = load_log(); tot = tot[tot.side != 0]
        notify("📝 PAPER gap fade result",
               f"{x}: {len(g)} paper trades · auction {g.auction_pct.mean():+.2f}% · "
               f"09:20 entry {g.real920_pct.mean():+.2f}% avg/trade. "
               f"Test so far: {tot.date.nunique()} days, 09:20 avg {tot.real920_pct.mean():+.2f}%.")


def tstat(s: pd.Series) -> float:
    s = s.dropna()
    return s.mean() / s.std() * math.sqrt(len(s)) if len(s) > 2 and s.std() > 0 else float("nan")


def stats(a):
    log = load_log()
    t = log[log.get("side", pd.Series(dtype=int)) != 0] if len(log) else log
    if t.empty:
        print("No paper trades logged yet. First results arrive the evening after the first trading day."); return
    days = log["date"].nunique()
    print(f"PAPER gap fade — {days} trading days ({log['date'].min()} → {log['date'].max()}), {len(t)} trades\n")
    print(f"{'version':<26}{'avg/trade':>10}{'win%':>7}{'t':>7}{'₹ on ₹10k':>12}")
    for col, name in (("auction_pct", "09:15 auction (backtest)"), ("atrstop_pct", "auction + 1×ATR stop"),
                      ("real920_pct", "09:20 entry (realistic)")):
        s = t[col].dropna()
        rupees = (s / 100 * CAPITAL * SLOT).sum()
        print(f"{name:<26}{s.mean():>+9.2f}%{(s > 0).mean() * 100:>6.0f}%{tstat(s):>7.1f}{rupees:>+12,.0f}")
    print(f"\nBacktest expectation: auction +0.38%/trade over 9 years, but ≈0% in 2026; "
          f"09:20 entry was −0.03% over the last 60 days.")
    s = t["real920_pct"].dropna()
    if days < REVIEW_DAYS:
        print(f"Verdict: too early — {days}/{REVIEW_DAYS} days. Don't read anything into this yet.")
    elif s.mean() >= PASS_MEAN and tstat(s) >= PASS_T:
        print(f"Verdict: PASSED the pre-set bar (≥ +{PASS_MEAN}% with t ≥ {PASS_T}). "
              f"Worth discussing a tiny real test — your call.")
    else:
        print(f"Verdict: FAILED the pre-set bar (needs ≥ +{PASS_MEAN}% with t ≥ {PASS_T}). Drop it.")


START = date(2026, 11, 5)   # first forward-test day; earlier days are only for --dry checks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("morning").set_defaults(fn=morning)
    e = sub.add_parser("evening"); e.add_argument("--date"); e.add_argument("--dry", action="store_true")
    e.set_defaults(fn=evening)
    sub.add_parser("stats").set_defaults(fn=stats)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
