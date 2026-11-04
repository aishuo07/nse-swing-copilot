#!/usr/bin/env python3
"""Scan an NSE universe for swing-trade setups and rank them.

Logic combined from the source repos:
  * Minervini Stage-2 trend template (8 checks) + VCP / tight-base detection with zig-zag
    contractions, pivot and volume dry-up      — India `breakout-planner`, tradermonty `vcp-screener`
  * IBD-style RS rating 1–99 vs the universe    — India `momentum-scanner`, tradermonty `canslim-screener`
  * Stockbee 4% momentum burst & episodic pivot (gap ≥4% on ≥3× volume)
                                                — tradermonty `stockbee-*`, India `momentum-scanner`
  * Pullback to 20 EMA / 50 SMA inside a Stage-2 trend
  * 6-point multi-timeframe confluence score    — nse-trading-skills `multi-timeframe-analysis`
  * Liquidity filter (₹ crore average daily turnover)

Usage:
  scan.py                                  # NIFTY 200, top 15
  scan.py --universe nifty500 --top 25
  scan.py --symbols DIXON,TRENT,POLYCAB
  scan.py --setup vcp|burst|pullback|ep    # only one setup family
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

import common as c


def rs_raw(cl: pd.Series) -> float:
    """IBD-style weighted 12-month performance (latest quarter double-weighted)."""
    if len(cl) < 253:
        return np.nan
    p = cl.iloc[-1]
    q = [cl.iloc[-63], cl.iloc[-126], cl.iloc[-189], cl.iloc[-252]]
    return 0.4 * p / q[0] + 0.2 * p / q[1] + 0.2 * p / q[2] + 0.2 * p / q[3]


def trend_template(df: pd.DataFrame, rs_rating: float | None):
    cl = df["Close"]
    s50, s150, s200 = c.sma(cl, 50), c.sma(cl, 150), c.sma(cl, 200)
    p = cl.iloc[-1]
    h52, l52 = df["High"].iloc[-252:].max(), df["Low"].iloc[-252:].min()
    checks = {
        "above_150_200": p > s150.iloc[-1] and p > s200.iloc[-1],
        "150_above_200": s150.iloc[-1] > s200.iloc[-1],
        "200_rising_1m": s200.iloc[-1] > s200.iloc[-22],
        "50_above_150_200": s50.iloc[-1] > s150.iloc[-1] and s50.iloc[-1] > s200.iloc[-1],
        "above_50": p > s50.iloc[-1],
        "30pct_above_52w_low": p >= 1.30 * l52,
        "within_25pct_of_52w_high": p >= 0.75 * h52,
        "rs_rating_70plus": rs_rating is not None and rs_rating >= 70,
    }
    return sum(bool(v) for v in checks.values()), checks, h52


# ---- Base detection: ported from Shubhamkumartrichy/claude-trading-skills-india
# breakout-planner/scripts/breakout.py (MIT, (c) 2026 Shubham Kumar). Tested side by side: it finds
# VCP contractions that a plain zig-zag merges (e.g. HAL 11.7% → 7.4% → 3.6%).
def _zigzag(h, lo, start, thr_pct):
    up, dn = 1 + thr_pct / 100, 1 - thr_pct / 100
    pts, cand = [["H", start, h[start]]], None
    for i in range(start + 1, len(h)):
        if pts[-1][0] == "H":
            if h[i] > pts[-1][2]:
                pts[-1], cand = ["H", i, h[i]], None
                continue
            if cand is None or lo[i] < cand[2]:
                cand = ["L", i, lo[i]]
            if cand[1] < i and h[i] >= cand[2] * up:
                pts.append(cand)
                cand = ["H", i, h[i]]
        else:
            if lo[i] < pts[-1][2]:
                pts[-1], cand = ["L", i, lo[i]], None
                continue
            if cand is None or h[i] > cand[2]:
                cand = ["H", i, h[i]]
            if cand[1] < i and lo[i] <= cand[2] * dn:
                pts.append(cand)
                cand = ["L", i, lo[i]]
    return pts, cand


def _contractions(pts, pending, min_pending_depth=1.5, pause_tol=0.01):
    seq = [tuple(p) for p in pts]
    if pending and pending[0] == "L" and seq[-1][0] == "H":
        if (seq[-1][2] - pending[2]) / seq[-1][2] * 100 >= min_pending_depth:
            seq.append(tuple(pending))
    out, last_high = [], seq[0]
    for kind, i, p in seq:
        if kind == "H":
            last_high = (kind, i, p)
            continue
        if not out:
            out.append({"high_i": last_high[1], "high": last_high[2], "low_i": i, "low": p})
            continue
        cur = out[-1]
        if p <= cur["low"] and last_high[2] <= cur["high"]:
            cur["low_i"], cur["low"] = i, p
            continue
        while len(out) > 1 and out[-1]["high"] < last_high[2] * (1 - pause_tol):
            out.pop()
        out.append({"high_i": last_high[1], "high": last_high[2], "low_i": i, "low": p})
    for ct in out:
        ct["depth_pct"] = (ct["high"] - ct["low"]) / ct["high"] * 100
    return out


def base_analysis(df: pd.DataFrame, lookback=150, tighten=0.85, max_chase_pct=3.0, min_base_bars=15):
    h, lo, cl, v = (df[k].tolist() for k in ("High", "Low", "Close", "Volume"))
    n = len(df)
    atr_pct = float(c.atr(df).iloc[-1]) / cl[-1] * 100
    thr = min(8.0, max(3.0, 1.25 * atr_pct))
    w0 = max(0, n - lookback)
    search_end = max(w0 + 1, n - 3)
    start = max(range(w0, search_end), key=lambda i: (h[i], i))
    bars = n - 1 - start
    out = {"type": "none", "contractions": [], "pivot": None, "last_low": None, "length_bars": bars,
           "depth_pct": None, "dryup": None, "grade": "D", "score": 0, "state": None}
    if bars < min_base_bars:
        out.update(type="no base (at highs)", pivot=float(max(h[start:])), last_low=float(min(lo[-10:])),
                   state="no base")
        return out
    pts, pending = _zigzag(h, lo, start, thr)
    cons = _contractions(pts, pending)
    last = cons[-1] if cons else None
    pivot = last["high"] if last else h[start]
    base_end = n - 1
    if last:
        for i in range(last["low_i"] + 1, n):
            if cl[i] > pivot:
                base_end = i - 1
                break
    base_low = min(lo[start:base_end + 1])
    depth = (h[start] - base_low) / h[start] * 100
    dry = None
    if last:
        seg = v[last["high_i"]:base_end + 1]
        ref = v[max(0, last["high_i"] - 50):last["high_i"]]
        if seg and ref and np.mean(ref):
            dry = float(np.mean(seg) / np.mean(ref))
    depths = [x["depth_pct"] for x in cons]
    viol = sum(1 for k in range(1, len(depths)) if depths[k] > depths[k - 1] * tighten)
    vcp_ok = (len(depths) >= 2 and depths[-1] < depths[0] and depths[-1] <= 15 and depths[0] <= 50
              and viol <= (1 if len(depths) >= 3 else 0))
    flat_ok = depth <= 15 and (base_end - start) >= 25
    pts_ = 0
    if vcp_ok:
        typ = f"VCP {len(depths)}T"
        pts_ += 2 if 3 <= len(depths) <= 4 else 1
        pts_ += 2 if viol == 0 else 0
        pts_ += 2 if depths[-1] <= 6 else 1 if depths[-1] <= 10 else 0
        pts_ += 1 if depths[0] <= 35 else 0
        pts_ += 2 if dry is not None and dry <= 0.6 else 1 if dry is not None and dry <= 0.85 else 0
        pts_ += 1 if bars >= 20 else 0
        score = pts_ * 10
    elif flat_ok:
        typ = "flat base"
        pivot = max(h[start:base_end + 1])
        tail = cl[max(start, base_end - 9):base_end + 1]
        tight = (max(tail) - min(tail)) / max(tail) * 100 if tail else 99
        pts_ += 3 if depth <= 10 else 2
        pts_ += 2 if dry is not None and dry <= 0.6 else 1 if dry is not None and dry <= 0.85 else 0
        pts_ += 1 if base_end - start >= 35 else 0
        pts_ += 2 if tight <= 5 else 1 if tight <= 8 else 0
        score = round(pts_ * 100 / 8)
    else:
        typ, score = "loose", (20 if depth <= 35 else 0)
    if typ == "flat base":
        last_low = min(lo[max(start, base_end - 9):base_end + 1])
    else:
        last_low = last["low"] if last else base_low
    dist = (cl[-1] / pivot - 1) * 100
    if cl[-1] < last_low:
        state = "failed"
    elif dist < -15:
        state = "deep below pivot"
    elif cl[-1] < pivot:
        state = "pre-breakout"
    elif dist <= max_chase_pct:
        state = "breakout zone"
    else:
        state = "extended"
    out.update(type=typ, contractions=[round(d, 1) for d in depths], pivot=float(pivot),
               last_low=float(last_low), depth_pct=round(depth, 1),
               dryup=None if dry is None else round(dry, 2), score=score, state=state,
               grade="A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D")
    return out


def momentum_signals(df: pd.DataFrame):
    o, h, l, cl, v = (df[k] for k in ("Open", "High", "Low", "Close", "Volume"))
    chg = cl.iloc[-1] / cl.iloc[-2] - 1
    rng = h.iloc[-1] - l.iloc[-1]
    clv = (cl.iloc[-1] - l.iloc[-1]) / rng if rng else 0
    v50 = v.iloc[-51:-1].mean()
    prior3 = cl.iloc[-2] / cl.iloc[-5] - 1
    sig = []
    # Stockbee 4% burst: +4%, volume > yesterday, closes near high, not after a 3-day run
    if chg >= 0.04 and v.iloc[-1] > v.iloc[-2] and clv >= 0.7 and prior3 < 0.10:
        sig.append("burst")
    # Episodic pivot: gap ≥4% on ≥3× average volume and holds the gap
    gap = o.iloc[-1] / cl.iloc[-2] - 1
    if gap >= 0.04 and v50 and v.iloc[-1] >= 3 * v50 and cl.iloc[-1] >= o.iloc[-1]:
        sig.append("ep")
    # range expansion: today's range > each of prior 3, close in upper half
    if rng > (h - l).iloc[-4:-1].max() and clv >= 0.5 and chg > 0.02:
        sig.append("range-exp")
    return sig, {"day_chg_pct": round(chg * 100, 2), "close_loc": round(clv, 2),
                 "vol_x50": round(v.iloc[-1] / v50, 2) if v50 else None, "gap_pct": round(gap * 100, 2)}


def pullback_signal(df: pd.DataFrame):
    cl, lo = df["Close"], df["Low"]
    e20, s50 = c.ema(cl, 20), c.sma(cl, 50)
    r = c.rsi(cl)
    touched = ((lo.iloc[-3:] <= e20.iloc[-3:] * 1.01).any() or (lo.iloc[-3:] <= s50.iloc[-3:] * 1.01).any())
    held = cl.iloc[-1] > e20.iloc[-1] * 0.99 and cl.iloc[-1] > s50.iloc[-1]
    return bool(touched and held and 38 <= r.iloc[-1] <= 58 and cl.iloc[-1] > cl.iloc[-2])


def mtf_score(df: pd.DataFrame, setup_present: bool, sr_clear: bool):
    """nse-trading-skills 6-point confluence (long side)."""
    w = c.weekly(df)
    wcl = w["Close"]
    weekly_up = len(w) > 32 and wcl.iloc[-1] > c.sma(wcl, 30).iloc[-1] and c.sma(wcl, 10).iloc[-1] > c.sma(wcl, 30).iloc[-1]
    cl = df["Close"]
    _, _, hist = c.macd(cl)
    v = df["Volume"]
    up_days = cl.diff().iloc[-20:] > 0
    vol_ok = v.iloc[-20:][up_days].mean() > v.iloc[-20:][~up_days].mean() if (~up_days).any() and up_days.any() else False
    pts = {"weekly_trend_up": bool(weekly_up), "daily_setup": bool(setup_present),
           "rsi_above_50": bool(c.rsi(cl).iloc[-1] > 50), "macd_hist_pos": bool(hist.iloc[-1] > 0),
           "clear_sr_level": bool(sr_clear), "volume_confirms": bool(vol_ok)}
    return sum(pts.values()), pts


def analyse(sym, df, rs_rating, min_turnover_cr):
    df = df.dropna()
    if len(df) < 260:
        return None
    cl = df["Close"]
    p = float(cl.iloc[-1])
    turnover_cr = float((cl * df["Volume"]).iloc[-50:].mean() / 1e7)
    if turnover_cr < min_turnover_cr:
        return None
    tt, checks, h52 = trend_template(df, rs_rating)
    base = base_analysis(df)
    mom, mstats = momentum_signals(df)
    pb = pullback_signal(df) and tt >= 7
    a = float(c.atr(df).iloc[-1])
    piv = base["pivot"] or p
    dist = (p / piv - 1) * 100
    state = base["state"] or ("extended" if dist > 3 else "breakout zone" if dist >= 0 else "pre-breakout")
    # 52-week-high momentum (PRIMARY — the only family that beat NIFTY in both halves of the
    # 9-year backtest): close above the prior 252-bar high with RS >= 70.
    prior_hi = float(df["High"].iloc[-253:-1].max())
    high52 = p > prior_hi and (rs_rating or 0) >= 70
    near52 = not high52 and p >= 0.97 * prior_hi and (rs_rating or 0) >= 70
    setups = []
    if high52:
        setups.append("high52")
    if tt >= 7 and base["type"].startswith(("VCP", "flat")) and base["grade"] in ("A", "B", "C") and (
            state == "breakout zone" or (state == "pre-breakout" and dist >= -5)):
        setups.append("vcp" if base["type"].startswith("VCP") else "flat-base")
    if pb:
        setups.append("pullback")
    # Info-only flags: Stockbee bursts / EPs lost money on NSE after charges in the backtest
    flags = list(mom) + (["near-52w-high"] if near52 else [])
    # Yahoo NSE data sometimes misses split/bonus adjustments → fake 30%+ one-day jumps. Don't trade those.
    jump = float(cl.iloc[-260:].pct_change().abs().max())
    if jump > 0.30:
        flags.append(f"DATA? {jump * 100:.0f}% 1-day jump")
        setups = []          # verify on Kite/NSE before trusting any signal
    mtf, mtf_pts = mtf_score(df, bool(setups), base["pivot"] is not None)
    # composite 0-100
    score = tt / 8 * 35 + (rs_rating or 0) / 99 * 20 + base["score"] / 100 * 20 + mtf / 6 * 15
    score += 15 if "high52" in setups else 10 if setups else 0
    if state == "extended":
        score -= 10
    return {"symbol": sym, "close": round(p, 2), "score": round(score, 1), "setups": setups,
            "state": state, "pct_from_pivot": round(dist, 2), "pivot": round(piv, 2),
            "last_low": round(base["last_low"], 2) if base["last_low"] else None,
            "trend_template": f"{tt}/8", "tt_score": tt, "rs_rating": rs_rating,
            "base": base["type"], "base_grade": base["grade"], "contractions": base["contractions"],
            "dryup": base["dryup"], "mtf": f"{mtf}/6", "mtf_points": mtf_pts,
            "atr_pct": round(a / p * 100, 2), "turnover_cr": round(turnover_cr, 1),
            "pct_off_52w_high": round((p / h52 - 1) * 100, 1), "momentum": mom, "flags": flags,
            "prior_52w_high": round(prior_hi, 2), **mstats,
            "failed_checks": [k for k, v in checks.items() if not v]}


def run(symbols, top, setup_filter, min_turnover_cr, industries=None):
    data = c.history(symbols, period="2y")
    data = {s: c.completed(df) if c.now_ist().hour < 16 else df for s, df in data.items()}
    raw = {s: rs_raw(df["Close"]) for s, df in data.items()}
    ser = pd.Series(raw).dropna()
    ranks = (ser.rank(pct=True) * 98 + 1).round().astype(int).to_dict()
    rows = []
    for s, df in data.items():
        r = analyse(s, df, ranks.get(s), min_turnover_cr)
        if r is None:
            continue
        if industries is not None:
            r["industry"] = industries.get(s, "")
        rows.append(r)
    if setup_filter:
        rows = [r for r in rows if any(x.startswith(setup_filter) for x in r["setups"])]
    rows.sort(key=lambda r: ("high52" in r["setups"], bool(r["setups"]), r["score"]), reverse=True)
    return rows[:top], len(data)


def show(rows, n_scanned, market):
    print(f"\nSETUP SCAN — {n_scanned} stocks scanned, {c.now_ist():%d-%b-%Y %H:%M} IST ({c.market_status()})")
    if market:
        print(f"Market: {market['label']} → {market['action']}, risk/trade {market['risk_per_trade_pct']}% "
              f"(as of {market['as_of']})")
    print("=" * 118)
    print(f"{'#':>2} {'SYMBOL':<12}{'CLOSE':>9} {'SCORE':>5} {'SETUP':<19}{'STATE':<13}{'PIVOT':>9}"
          f"{'Δpiv':>7} {'TT':>4} {'RS':>3} {'BASE':<15}{'DRY':>5} {'MTF':>4} {'ATR%':>5} {'₹cr/d':>6}  FLAGS")
    for i, r in enumerate(rows, 1):
        print(f"{i:>2} {r['symbol']:<12}{r['close']:>9,.1f} {r['score']:>5.0f} {','.join(r['setups']) or '-':<19}"
              f"{r['state']:<13}{r['pivot']:>9,.1f}{r['pct_from_pivot']:>+6.1f}% {r['trend_template']:>4} "
              f"{r['rs_rating'] or 0:>3} {(r['base'] + ' ' + r['base_grade'])[:14]:<15}"
              f"{r['dryup'] if r['dryup'] is not None else '-':>5} {r['mtf']:>4} {r['atr_pct']:>5.1f} {r['turnover_cr']:>6.0f}  {','.join(r.get('flags', []))}")
    print("\nTT = Minervini trend template (8/8 = Stage 2) · RS = relative strength rating 1–99 · "
          "DRY = volume in last contraction ÷ pre-base volume (<0.6 strong) · MTF = confluence 0–6")
    print("SETUP priority (9-yr backtest): high52 = primary system · vcp/flat-base = secondary · pullback = weak.")
    print("FLAGS burst/ep/range-exp are info only — Stockbee bursts LOST money on NSE after charges.")
    print("Next: run plan.py SYMBOL on the top names, and check the chart + results date before acting.")
    print("Educational screen, not a buy list. Data: Yahoo Finance EOD.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=None)
    ap.add_argument("--symbols")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--setup", choices=["high52", "vcp", "flat", "pullback"])
    ap.add_argument("--min-turnover-cr", type=float, default=10.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cfg = c.load_config()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
        # RS rating needs a peer set: rank against the default universe too
        u = c.universe(cfg["universe"])
        peers = list(dict.fromkeys(syms + list(u["Symbol"])))
        rows, n = run(peers, 10_000, a.setup, a.min_turnover_cr, dict(zip(u["Symbol"], u["Industry"])))
        rows = [r for r in rows if r["symbol"] in syms]
        n = len(syms)
    else:
        u = c.universe(a.universe or cfg["universe"])
        rows, n = run(list(u["Symbol"]), a.top, a.setup, a.min_turnover_cr, dict(zip(u["Symbol"], u["Industry"])))
    c.save_state("scan_latest", {"generated": c.now_ist().isoformat(), "rows": rows})
    if a.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        show(rows, n, c.load_state("market"))
