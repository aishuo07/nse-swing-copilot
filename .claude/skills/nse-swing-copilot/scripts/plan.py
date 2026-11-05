#!/usr/bin/env python3
"""Trade planning and management for Zerodha (Kite) — produces order tickets the USER places.

Subcommands
  plan SYMBOL [--entry P --stop S] [--capital X --risk-pct R] [--setup auto|high52|breakout|pullback]
      Entry trigger, chase limit, stop (structure vs ATR, capped at 8%), quantity from rupee risk
      (regime-scaled), Zerodha charges, 2R/3R targets net of costs, gap-through risk, results-date
      warning, pre-trade gate, and a Kite GTT ticket.
  manage SYMBOL --entry P --stop S --qty N [--high H]
      Hybrid trailing stop (nse-trading-skills): <1R hold · 1R breakeven · 2R ATR trail ·
      3R+ structure trail; tightened when the market regime is under pressure.
  charges --buy P --sell Q --qty N [--segment delivery|intraday|futures|options]

Logic sources: India `breakout-planner`, `position-sizing`, `stop-loss-strategies`,
`trailing-stops`, `trading-costs`; nse-trading-skills `risk-reward-ratio`, `trailing-stops`;
tradermonty `breakout-trade-planner`, `position-sizer`, `pre-trade-discipline-gate`.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date

import numpy as np
import pandas as pd

import common as c
from scan import base_analysis


def results_date(sym: str):
    try:
        import yfinance as yf
        cal = yf.Ticker(c.yf_symbol(sym)).calendar or {}
        d = cal.get("Earnings Date")
        if d:
            return d[0] if isinstance(d, list) else d
    except Exception:
        pass
    return None


def open_positions():
    import journal
    return journal.open_trades()


def size_position(capital, risk_pct, entry, stop, max_pos_pct, open_risk, max_open_risk_pct):
    per_share_risk = entry - stop
    est_cost_per_share = entry * 0.0025          # ~0.25% delivery round trip incl. STT/DP
    budget = capital * risk_pct / 100
    qty_risk = math.floor(budget / (per_share_risk + est_cost_per_share))
    qty_cap = math.floor(capital * max_pos_pct / 100 / entry)
    room = capital * max_open_risk_pct / 100 - open_risk
    qty_room = math.floor(max(room, 0) / (per_share_risk + est_cost_per_share))
    qty = max(0, min(qty_risk, qty_cap, qty_room))
    binding = min([("risk budget", qty_risk), ("position cap", qty_cap), ("portfolio heat", qty_room)],
                  key=lambda x: x[1])[0]
    return qty, binding, budget


def cmd_plan(a):
    cfg = c.load_config()
    capital = a.capital or cfg["capital"]
    if not capital:
        c.die("capital not set. Run: journal.py config --capital 500000  (or pass --capital)")
    mkt = c.load_state("market")
    import journal
    br = journal.breaker(capital, cfg)
    # tested system sizes at the configured risk; the regime's own risk figure is informational
    risk_pct = a.risk_pct if a.risk_pct is not None else cfg["risk_pct"]
    if br["status"] == "AMBER" and a.risk_pct is None:
        risk_pct = risk_pct / 2
    df = c.completed(c.one(a.symbol))
    cl = df["Close"]
    last = float(cl.iloc[-1])
    a14 = float(c.atr(df).iloc[-1])
    base = base_analysis(df)
    large = float((cl * df["Volume"]).iloc[-50:].mean() / 1e7) > 100
    buf = 0.005 if large else 0.01

    # ---- entry
    prior_hi = float(df["High"].iloc[-253:-1].max())
    if a.setup == "auto":
        a.setup = "high52" if last > prior_hi else "breakout"
    if a.entry:
        trigger = a.entry
        setup = "manual"
    elif a.setup == "high52":
        trigger = c.round_tick(last, up=True)
        setup = (f"52-week-high momentum (close {last:,.2f} > prior 52w high {prior_hi:,.2f}) — "
                 "buy next session, max 3% above close")
        a.max_chase = 3.0
    elif a.setup == "pullback":
        trigger = c.round_tick(float(df["High"].iloc[-1]) * 1.001, up=True)
        setup = "pullback (buy above yesterday's high)"
    else:
        trigger = c.round_tick(base["pivot"] * 1.002, up=True)
        setup = f"breakout of {base['type']} pivot {base['pivot']:,.2f}"
    chase = c.round_tick(trigger * (1 + a.max_chase / 100), down=True)

    # ---- stop: structure vs ATR, pick the one closer to entry that is still ≥1 ATR away
    swing_low = float(df["Low"].iloc[-10:].min()) if a.setup == "pullback" or not base["last_low"] else base["last_low"]
    structure = swing_low * (1 - buf)
    atr_stop = trigger - 2 * a14
    cands = [s for s in (structure, atr_stop) if trigger - s >= a14]
    stop = max(cands) if cands else min(structure, atr_stop)
    stop_note = "structure" if stop == structure else "2×ATR"
    if a.setup == "high52":
        stop, stop_note = trigger * (1 + a.max_chase / 100) - 2.5 * a14, "2.5×ATR (tested rule)"
    if a.stop:
        stop, stop_note = a.stop, "manual"
    capped = False
    if (chase - stop) / chase > 0.08 and not a.stop and a.setup != "high52":
        stop, capped = chase * 0.92, True
        stop_note += " → capped at 8% below chase limit"
    stop = c.round_tick(stop, down=True)
    if stop >= trigger:
        c.die("stop must be below entry")

    # ---- size at worst fill (chase limit)
    opens = open_positions()
    open_risk = sum(max(0.0, (t["entry"] - t["stop"]) * t["qty"]) for t in opens)
    qty, binding, budget = size_position(capital, risk_pct, chase, stop, cfg["max_position_pct"],
                                         open_risk, cfg["max_open_risk_pct"])
    risk_r = chase - stop
    t2, t3 = c.round_tick(chase + 2 * risk_r), c.round_tick(chase + 3 * risk_r)
    ch_stop = c.charges("delivery", chase, stop, max(qty, 1))
    ch_t2 = c.charges("delivery", chase, t2, max(qty, 1))
    ch_t3 = c.charges("delivery", chase, t3, max(qty, 1))
    loss_at_stop = -ch_stop["net_pnl"]
    rr_net = ch_t2["net_pnl"] / loss_at_stop if loss_at_stop > 0 else 0

    # ---- gap risk: worst overnight gap-down in last 120 sessions
    gaps = (df["Open"] / cl.shift() - 1).iloc[-120:]
    worst_gap = float(gaps.min())
    gap_fill = stop * (1 + worst_gap)
    gap_loss = (chase - gap_fill) * qty

    # ---- gate checks (tradermonty pre-trade-discipline-gate + India trading-discipline)
    warns, blocks = [], []
    rd = results_date(a.symbol)
    if rd:
        days = (pd.Timestamp(rd) - pd.Timestamp(date.today())).days
        if 0 <= days <= 7:
            warns.append(f"Results on {rd:%d-%b} ({days} days) — 5–15% gap risk. Wait for the number or halve size.")
        elif 7 < days <= 25:
            warns.append(f"Results on {rd:%d-%b} ({days} days) fall inside a normal swing holding period — "
                         "plan to be ≥1R in profit or reduce before the date.")
    if mkt:
        if mkt["max_new_positions"] == 0:
            blocks.append(f"Market regime '{mkt['label']}' allows no new positions")
        deployed = sum(t["entry"] * t["qty"] for t in opens)
        if (deployed + chase * qty) / capital * 100 > mkt["exposure_ceiling_pct"]:
            warns.append(f"Would take exposure above the regime ceiling ({mkt['exposure_ceiling_pct']}%)")
    else:
        warns.append("No market posture saved — run market.py first")
    if mkt and not mkt.get("system_filter_on", True) and not a.ignore_filter:
        blocks.append("System filter OFF (NIFTY below its 50 DMA) — in the survivorship-free 9-yr backtest, "
                      "entries in this state lost money. Use --ignore-filter only for a deliberate pilot.")
    if len(opens) >= cfg["max_positions"]:
        blocks.append(f"Already {len(opens)} open positions (max {cfg['max_positions']})")
    if qty == 0:
        blocks.append(f"Quantity rounds to 0 ({binding} binding) — skip or use a closer stop")
    if a.setup == "high52":
        if (chase - stop) / chase > 0.12:
            warns.append("Stop is >12% away (very volatile stock) — position will be small")
    elif rr_net < 1.5:
        warns.append(f"Net R:R to 2R target only {rr_net:.2f} after charges")
    pct_above = (last / trigger - 1) * 100
    if pct_above > a.max_chase:
        blocks.append(f"Price already {pct_above:.1f}% above trigger — extended, don't chase")
    if br["status"] == "RED":
        blocks.append("Circuit breaker RED: " + "; ".join(br["reasons"]))
    elif br["status"] == "AMBER":
        warns.append("Circuit breaker AMBER (risk halved): " + "; ".join(br["reasons"]))
    verdict = "BLOCK" if blocks else "PASS-WITH-WARNINGS" if warns else "PASS"

    out = {"symbol": a.symbol, "as_of": df.index[-1].strftime("%d-%b-%Y"), "last_close": last,
           "setup": setup, "trigger": trigger, "chase_limit": chase, "stop": stop, "stop_method": stop_note,
           "stop_pct": round((chase - stop) / chase * 100, 2), "atr14": round(a14, 2),
           "qty": qty, "binding": binding, "position_value": round(chase * qty),
           "pct_of_capital": round(chase * qty / capital * 100, 1), "risk_pct": risk_pct,
           "loss_at_stop_net": round(loss_at_stop), "target_2R": t2, "target_3R": t3,
           "profit_2R_net": round(ch_t2["net_pnl"]), "profit_3R_net": round(ch_t3["net_pnl"]),
           "rr_net_2R": round(rr_net, 2), "round_trip_charges": round(ch_t2["total"]),
           "worst_gap_120d_pct": round(worst_gap * 100, 2), "gap_through_loss": round(gap_loss),
           "results_date": str(rd) if rd else None, "gate": verdict, "blocks": blocks, "warnings": warns,
           "capital": capital, "regime": mkt["label"] if mkt else None}
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return
    print(f"\nTRADE PLAN — {a.symbol}  (EOD {out['as_of']}, close {c.inr(last, 2)})  regime: {out['regime']}")
    print("=" * 78)
    print(f"Setup:   {setup}")
    print(f"Entry:   buy trigger {c.inr(trigger, 2)}  ·  limit (max chase) {c.inr(chase, 2)}")
    print(f"Stop:    {c.inr(stop, 2)}  ({out['stop_pct']}% below worst fill; {stop_note}; ATR {a14:.2f})")
    print(f"Size:    {qty} shares = {c.inr(out['position_value'])} ({out['pct_of_capital']}% of {c.inr(capital)})"
          f"  — risk {risk_pct}% = {c.inr(budget)} budget; binding: {binding}")
    if a.setup == "high52":
        print("Exit:    no fixed target — breakeven stop after +1R close, SELL when a daily close is below the 50 DMA")
    print(f"Targets: {'(reference) ' if a.setup == 'high52' else ''}2R {c.inr(t2, 2)} → net +{c.inr(out['profit_2R_net'])}  ·  3R {c.inr(t3, 2)} → net +{c.inr(out['profit_3R_net'])}")
    print(f"Risk:    net loss at stop {c.inr(loss_at_stop)} · R:R after charges {rr_net:.2f} · "
          f"round-trip charges ≈ {c.inr(out['round_trip_charges'])}")
    print(f"Gap:     worst 120-day gap-down {out['worst_gap_120d_pct']}% → a gap through the stop could lose ≈ {c.inr(gap_loss)}")
    print(f"Results: {rd:%d-%b-%Y}" if rd else "Results: date not found — check NSE corporate announcements")
    print("-" * 78)
    print(f"GATE: {verdict}")
    for b in blocks:
        print(f"  ✗ {b}")
    for w in warns:
        print(f"  ! {w}")
    if qty > 0 and not blocks and a.setup == "high52":
        print("\nKITE ORDERS — you place these yourself in Kite (web/app):")
        print(f"  1) Next session: BUY {a.symbol} NSE · CNC · LIMIT {c.inr(chase, 2)} · qty {qty} "
              f"(skip if it opens above {c.inr(chase, 2)})")
        print(f"  2) After the buy fills: GTT single (sell) · qty {qty} · trigger {c.inr(stop, 2)} "
              f"limit {c.inr(c.round_tick(stop * 0.99, down=True), 2)}")
        print("     (T+1: shares show in holdings next day; on the buy day protect with a DAY SL order if needed)")
        print(f"  3) Log it: journal.py open {a.symbol} --entry <fill> --stop {stop} --qty {qty} --setup high52")
        print(f"  4) Each evening: plan.py manage {a.symbol} --system high52 … (exit on a close below the 50 DMA)")
    elif qty > 0 and not blocks:
        print("\nKITE ORDERS — you place these yourself in Kite (web/app):")
        print(f"  1) GTT single (buy): {a.symbol} NSE · CNC · qty {qty} · trigger {c.inr(trigger, 2)} · limit {c.inr(chase, 2)}")
        print(f"  2) After the buy fills: GTT OCO (sell) · qty {qty} · stop-loss trigger {c.inr(stop, 2)} "
              f"limit {c.inr(c.round_tick(stop * 0.99, down=True), 2)} · target trigger {c.inr(t3, 2)} limit {c.inr(t3, 2)}")
        print("     (T+1: shares show in holdings next day; on the buy day protect with a DAY SL order if needed)")
        print(f"  3) Log it: journal.py open {a.symbol} --entry <fill> --stop {stop} --qty {qty} --target {t3} --setup <name>")
    print("\nEducational plan, not investment advice. You decide and you place the order.")


def cmd_manage(a):
    df = c.completed(c.one(a.symbol))
    cl = df["Close"]
    last = float(cl.iloc[-1])
    a14 = float(c.atr(df).iloc[-1])
    R = a.entry - a.stop
    if R <= 0:
        c.die("entry must be above stop")
    entry_date = pd.Timestamp(a.since) if a.since else None
    seg = df.loc[entry_date:] if entry_date is not None else df.iloc[-20:]
    hi_close = max(float(seg["Close"].max()), a.high or 0)
    r_now = (last - a.entry) / R
    r_peak = (hi_close - a.entry) / R
    mkt = c.load_state("market")
    pressure = mkt and mkt["label"] not in ("Confirmed uptrend",)
    mult = 1.5 if pressure else 2.0
    stage, new_stop = "hold initial stop (<1R)", a.stop
    if r_peak >= 1:
        stage, new_stop = "breakeven (≥1R)", a.entry * 1.003
    if a.system == "high52":
        if r_peak >= 1:
            stage = "breakeven (≥1R); exit rule = daily close below 50 DMA"
    elif r_peak >= 2:
        stage = f"ATR trail (≥2R): highest close − {mult}×ATR"
        new_stop = max(new_stop, hi_close - mult * a14)
    if r_peak >= 3 and a.system != "high52":
        pts = c.zigzag(df.iloc[-60:], max(0.03, a14 / last))
        lows = [p[1] for p in pts if p[2] == "L"]
        if lows:
            stage = "structure trail (≥3R): below last higher low"
            new_stop = max(new_stop, lows[-1] * 0.995)
    new_stop = c.round_tick(max(new_stop, a.stop), down=True)     # ratchet: never lower
    s50 = c.sma(cl, 50).iloc[-1]
    alerts = []
    if a.system == "high52" and last < s50:
        alerts.append(f"EXIT SIGNAL: closed {c.inr(last, 2)} below the 50 DMA {c.inr(s50, 2)} — sell at next open")
    elif a.system == "high52":
        alerts.append(f"Hold: close is {((last / s50) - 1) * 100:.1f}% above the 50 DMA ({c.inr(s50, 2)})")
    elif last < s50 and df["Volume"].iloc[-1] > df["Volume"].iloc[-51:-1].mean() * 1.3:
        alerts.append("Closed below 50 DMA on heavy volume — consider exiting at next open")
    if last <= a.stop:
        alerts.append("Close is at/below your stop — exit at next open, no exceptions")
    if r_now >= 2 and a.qty >= 2 and a.system != "high52":
        alerts.append(f"Consider booking 1/3–1/2 ({a.qty // 3}–{a.qty // 2} shares) at ≥2R")
    unreal = c.charges("delivery", a.entry, last, a.qty)["net_pnl"]
    print(f"\nMANAGE — {a.symbol}  close {c.inr(last, 2)} (EOD {df.index[-1]:%d-%b})")
    print(f"Entry {c.inr(a.entry, 2)} · initial stop {c.inr(a.stop, 2)} · 1R = {c.inr(R, 2)} · qty {a.qty}")
    print(f"Now {r_now:+.2f}R (peak close {r_peak:+.2f}R) · unrealised net {c.inr(unreal)}")
    print(f"Stage: {stage}{' — tightened, regime under pressure' if pressure and r_peak >= 2 else ''}")
    print(f"→ NEW STOP: {c.inr(new_stop, 2)}" + ("  (unchanged)" if new_stop <= a.stop else
          f"  (raise from {c.inr(a.stop, 2)}; modify the GTT OCO stop leg in Kite)"))
    for x in alerts:
        print(f"  ! {x}")
    print("Rules: only ratchet up · judge on closing prices · if hit, exit next open.")


def cmd_charges(a):
    r = c.charges(a.segment, a.buy, a.sell, a.qty)
    print(f"\nZerodha {a.segment} · buy {c.inr(a.buy, 2)} sell {c.inr(a.sell, 2)} × {a.qty}")
    for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp"):
        print(f"  {k:<10}{c.inr(r[k], 2):>14}")
    print(f"  {'TOTAL':<10}{c.inr(r['total'], 2):>14}   (breakeven move {r['breakeven_move_pct']:.3f}%)")
    print(f"  gross {c.inr(r['gross_pnl'], 2)} → net {c.inr(r['net_pnl'], 2)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("plan")
    p.add_argument("symbol", type=str.upper)
    p.add_argument("--entry", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--capital", type=float)
    p.add_argument("--risk-pct", type=float)
    p.add_argument("--setup", choices=["auto", "high52", "breakout", "pullback"], default="auto",
                   help="auto = high52 if today is a new 52-week high, else breakout")
    p.add_argument("--max-chase", type=float, default=2.0, help="%% above trigger you will pay at most")
    p.add_argument("--json", action="store_true")
    p.add_argument("--ignore-filter", action="store_true", help="override the NIFTY 50 DMA filter")
    m = sp.add_parser("manage")
    m.add_argument("symbol", type=str.upper)
    m.add_argument("--entry", type=float, required=True)
    m.add_argument("--stop", type=float, required=True)
    m.add_argument("--qty", type=int, required=True)
    m.add_argument("--since", help="entry date YYYY-MM-DD")
    m.add_argument("--high", type=float)
    m.add_argument("--system", choices=["high52", "breakout"], default="breakout")
    ch = sp.add_parser("charges")
    ch.add_argument("--buy", type=float, required=True)
    ch.add_argument("--sell", type=float, required=True)
    ch.add_argument("--qty", type=float, required=True)
    ch.add_argument("--segment", default="delivery", choices=list(c.STT))
    a = ap.parse_args()
    {"plan": cmd_plan, "manage": cmd_manage, "charges": cmd_charges}[a.cmd](a)
