#!/usr/bin/env python3
"""Market posture for Indian equities: should I be buying at all, and how much?

Combines:
  * O'Neil distribution days + follow-through day on NIFTY (volume from NIFTYBEES ETF, since
    Yahoo's ^NSEI volume is unreliable)            — India repo `market-regime`, tradermonty `ftd-detector`
  * Trend: NIFTY vs 50/200 DMA, 50 DMA slope, drawdown from peak
  * Breadth: % of NIFTY 200 stocks above 50/200 DMA, new highs vs new lows  — `market-breadth`
  * India VIX bands                                                          — India repo
  * Exposure ceiling / NEW_ENTRY_ALLOWED / REDUCE_ONLY / CASH_PRIORITY       — tradermonty `exposure-coach`

Usage:
  market.py                 # full posture report, saves data/state/market.json
  market.py --json
  market.py --no-breadth    # faster, skip the 200-stock breadth download
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

import common as c


def distribution_days(px: pd.DataFrame, vol: pd.Series, window=25, dd_pct=-0.2):
    chg = px["Close"].pct_change() * 100
    v = vol.reindex(px.index)
    vprev = v.shift()
    is_dd = (chg <= dd_pct) & (v > vprev) & v.notna() & vprev.notna()
    last = px.index[-window:]
    days = []
    for d in last:
        if not is_dd.get(d, False):
            continue
        close_d = px.loc[d, "Close"]
        later = px.loc[d:, "Close"].iloc[1:]
        if len(later) and later.max() >= close_d * 1.05:   # cleared by a 5% rally
            continue
        days.append(d)
    n = len(px)
    pos = {d: n - 1 - px.index.get_loc(d) for d in days}   # sessions ago
    return {"d25": len(days), "d15": sum(1 for d in days if pos[d] < 15),
            "d5": sum(1 for d in days if pos[d] < 5),
            "dates": [d.strftime("%d-%b") for d in days]}


def ftd_state(px: pd.DataFrame, vol: pd.Series):
    """Correction / rally-attempt / follow-through state machine on daily closes."""
    close = px["Close"]
    ret = close.pct_change()
    sigma = ret.iloc[-250:].std() * 100
    thresh = float(np.clip(1.5 * sigma, 1.0, 2.0))
    look = close.iloc[-250:]
    peak_i = look.idxmax()
    peak = look.max()
    dd = (close.iloc[-1] / peak - 1) * 100
    after = close.loc[peak_i:]
    low_i = after.idxmin()
    low = after.min()
    corr_depth = (low / peak - 1) * 100
    state = {"ftd_threshold_pct": round(thresh, 2), "peak": round(peak, 1),
             "peak_date": peak_i.strftime("%d-%b-%Y"), "drawdown_pct": round(dd, 2),
             "correction_low": round(low, 1), "correction_low_date": low_i.strftime("%d-%b-%Y")}
    if corr_depth > -5:
        state.update(phase="no correction", ftd_date=None)
        return state
    # rally attempt: first up day after the low
    seg = close.loc[low_i:]
    v = vol.reindex(seg.index)
    day1 = None
    for i in range(1, len(seg)):
        if seg.iloc[i] > seg.iloc[i - 1]:
            day1 = i
            break
    if day1 is None:
        state.update(phase="correction", ftd_date=None)
        return state
    ftd = None
    for i in range(day1 + 3, len(seg)):          # day 4 onwards
        g = (seg.iloc[i] / seg.iloc[i - 1] - 1) * 100
        if g >= thresh and pd.notna(v.iloc[i]) and pd.notna(v.iloc[i - 1]) and v.iloc[i] > v.iloc[i - 1]:
            ftd = i
            break
    if ftd is None:
        state.update(phase="rally attempt", ftd_date=None,
                     rally_day=len(seg) - day1)
        return state
    failed = seg.iloc[ftd:].min() < low
    sessions_since = len(seg) - 1 - ftd
    state.update(phase="follow-through failed" if failed else "follow-through confirmed",
                 ftd_date=seg.index[ftd].strftime("%d-%b-%Y"), sessions_since_ftd=sessions_since)
    return state


def breadth(uni_name="nifty200"):
    u = c.universe(uni_name)
    data = c.history(list(u["Symbol"]), period="2y")
    a50 = a200 = hi = lo = n = 0
    for s, df in data.items():
        if len(df) < 210:
            continue
        df = c.completed(df)
        cl = df["Close"]
        n += 1
        a50 += cl.iloc[-1] > c.sma(cl, 50).iloc[-1]
        a200 += cl.iloc[-1] > c.sma(cl, 200).iloc[-1]
        h52 = df["High"].iloc[-252:].max()
        l52 = df["Low"].iloc[-252:].min()
        hi += df["High"].iloc[-1] >= h52
        lo += df["Low"].iloc[-1] <= l52
    if n == 0:
        return None
    return {"universe": uni_name, "stocks": n, "pct_above_50dma": round(100 * a50 / n, 1),
            "pct_above_200dma": round(100 * a200 / n, 1), "new_52w_highs": int(hi), "new_52w_lows": int(lo)}


def vix_band(v):
    if v is None:
        return "unknown"
    return ("calm" if v < 13 else "normal" if v < 17 else "elevated" if v < 22
            else "stressed" if v < 30 else "panic")


def posture(no_breadth=False):
    d = c.history(["NIFTY", "NIFTYBEES", "INDIAVIX"], period="2y")
    if "NIFTY" not in d:
        c.die("could not fetch NIFTY data")
    nifty = c.completed(d["NIFTY"])
    vol = c.completed(d["NIFTYBEES"])["Volume"] if "NIFTYBEES" in d else pd.Series(dtype=float)
    vix = float(d["INDIAVIX"]["Close"].iloc[-1]) if "INDIAVIX" in d else None
    cl = nifty["Close"]
    s50, s200 = c.sma(cl, 50), c.sma(cl, 200)
    last = float(cl.iloc[-1])
    trend = {
        "close": round(last, 1), "date": cl.index[-1].strftime("%d-%b-%Y"),
        "vs_50dma_pct": round((last / s50.iloc[-1] - 1) * 100, 2),
        "vs_200dma_pct": round((last / s200.iloc[-1] - 1) * 100, 2),
        "dma50_rising": bool(s50.iloc[-1] > s50.iloc[-11]),
        "ret_1m_pct": round((last / cl.iloc[-22] - 1) * 100, 2),
    }
    dist = distribution_days(nifty, vol)
    ftd = ftd_state(nifty, vol)
    br = None if no_breadth else breadth()

    # ---- risk score (0 good … 100 bad): distribution 35, trend 30, drawdown 20, VIX 15
    s_dist = min(dist["d25"], 7) / 7 * 35
    s_trend = (0 if trend["vs_50dma_pct"] > 0 else 12) + (0 if trend["vs_200dma_pct"] > 0 else 12) \
        + (0 if trend["dma50_rising"] else 6)
    ddp = -ftd["drawdown_pct"]
    s_dd = 0 if ddp < 3 else 7 if ddp < 5 else 13 if ddp < 10 else 20
    s_vix = {"calm": 0, "normal": 3, "elevated": 8, "stressed": 12, "panic": 15}.get(vix_band(vix), 5)
    score = s_dist + s_trend + s_dd + s_vix
    if ftd["phase"] == "follow-through confirmed" and ftd.get("sessions_since_ftd", 99) <= 10:
        score -= 10
    score = int(max(0, min(100, round(score))))

    # ---- label → exposure (India repo `market-regime` table)
    if ftd["phase"] in ("correction", "follow-through failed"):
        label, ceil, newpos, risk = "Correction", (10 if ddp >= 10 else 20), 0, 0.0
    elif ftd["phase"] == "rally attempt":
        label, ceil, newpos, risk = "Rally attempt (no follow-through yet)", 25, 1, 0.25
    elif ftd["phase"] == "follow-through confirmed" and ftd.get("sessions_since_ftd", 99) <= 10:
        label = "Confirmed uptrend (fresh FTD)" if score < 60 else "Under pressure (fresh FTD)"
        ceil, newpos, risk = (50, 3, 0.5) if score < 60 else (30, 2, 0.25)
    elif (dist["d25"] >= 5 or dist["d15"] >= 4 or trend["vs_50dma_pct"] < 0
          or trend["vs_200dma_pct"] < 0 or score >= 50):
        label = "Uptrend under pressure"
        ceil, newpos, risk = (40, 1, 0.5) if score >= 50 else (60, 2, 0.75)
    else:
        label = "Confirmed uptrend"
        ceil, newpos, risk = (100, 5, 1.0) if score < 25 else (80, 4, 1.0)

    notes = []
    if vix is not None and vix >= 22:
        ceil = min(ceil, 50)
        notes.append(f"India VIX {vix:.1f} ≥ 22 → exposure capped at 50%")
    if br:
        if br["pct_above_200dma"] < 40:
            ceil = min(ceil, 40)
            notes.append(f"Weak breadth: only {br['pct_above_200dma']}% above 200 DMA → cap 40%")
        if br["new_52w_lows"] > br["new_52w_highs"] * 2 and br["new_52w_lows"] >= 10:
            notes.append("New lows dominate new highs — leadership is thin")
        if br["pct_above_50dma"] > 85:
            notes.append("Breadth very overbought (>85% above 50 DMA) — don't chase extended names")
    if ceil >= 50 and newpos > 0:
        action = "NEW_ENTRY_ALLOWED"
    elif newpos > 0:
        action = "REDUCE_ONLY (pilot positions only)"
    else:
        action = "CASH_PRIORITY"

    watch = [f"NIFTY 50 DMA {s50.iloc[-1]:,.0f}", f"200 DMA {s200.iloc[-1]:,.0f}"]
    if ftd["phase"] == "no correction":
        watch.append(f"Correction trigger (−5% from peak) {ftd['peak'] * 0.95:,.0f}")
    elif ftd["phase"] == "rally attempt":
        watch.append(f"FTD needs a ≥{ftd['ftd_threshold_pct']}% up day on higher volume; "
                     f"rally fails below {ftd['correction_low']:,.0f}")

    filt = bool(trend["vs_50dma_pct"] > 0 and trend["vs_200dma_pct"] > 0)
    return {"as_of": trend["date"], "market_status": c.market_status(), "label": label,
            "system_filter_on": filt,
            "risk_score": score, "exposure_ceiling_pct": ceil, "max_new_positions": newpos,
            "risk_per_trade_pct": risk, "action": action, "trend": trend,
            "distribution": dist, "ftd": ftd, "vix": None if vix is None else round(vix, 2),
            "vix_band": vix_band(vix), "breadth": br, "notes": notes, "watch": watch,
            "volume_source": "NIFTYBEES ETF" if len(vol) else "none (price-only)"}


def show(p):
    t, dd, f = p["trend"], p["distribution"], p["ftd"]
    print(f"\nMARKET POSTURE — NIFTY close {t['close']:,} on {p['as_of']}  ({p['market_status']})")
    print("=" * 78)
    print(f"Regime: {p['label']}   |   risk score {p['risk_score']}/100")
    print(f"Action: {p['action']}   |   exposure ceiling {p['exposure_ceiling_pct']}%   |   "
          f"max new positions {p['max_new_positions']}   |   risk/trade {p['risk_per_trade_pct']}%")
    print(f"System filter (backtested: NIFTY above 50 AND 200 DMA): "
          + ("ON → new momentum/breakout entries allowed" if p.get("system_filter_on")
             else "OFF → the tested system takes NO new entries; manage open trades only"))
    print("-" * 78)
    print(f"Trend:  vs 50DMA {c.pct(t['vs_50dma_pct'])}, vs 200DMA {c.pct(t['vs_200dma_pct'])}, "
          f"50DMA {'rising' if t['dma50_rising'] else 'falling'}, 1M {c.pct(t['ret_1m_pct'])}")
    print(f"Distribution days (vol: {p['volume_source']}): d5 {dd['d5']} | d15 {dd['d15']} | d25 {dd['d25']}"
          + (f"  [{', '.join(dd['dates'])}]" if dd['dates'] else ""))
    print(f"Cycle: {f['phase']}; drawdown from peak {f['drawdown_pct']}% (peak {f['peak']:,} on {f['peak_date']})"
          + (f"; FTD {f['ftd_date']}" if f.get("ftd_date") else ""))
    print(f"India VIX: {p['vix']} ({p['vix_band']})")
    if p["breadth"]:
        b = p["breadth"]
        print(f"Breadth ({b['universe']}, {b['stocks']} stocks): {b['pct_above_50dma']}% > 50DMA, "
              f"{b['pct_above_200dma']}% > 200DMA, 52w highs {b['new_52w_highs']} / lows {b['new_52w_lows']}")
    for n in p["notes"]:
        print(f"  ! {n}")
    print("Watch: " + " | ".join(p["watch"]))
    print("\nEducational analysis, not investment advice. Data: Yahoo Finance EOD.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-breadth", action="store_true")
    a = ap.parse_args()
    p = posture(a.no_breadth)
    c.save_state("market", p)
    print(json.dumps(p, indent=2, default=str)) if a.json else show(p)
