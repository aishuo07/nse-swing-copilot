#!/usr/bin/env python3
"""Trade journal, performance stats and loss circuit breaker (paper or live).

  journal.py config --capital 500000 [--risk-pct 1] [--mode paper|live] [--max-positions 6]
  journal.py open SYMBOL --entry P --stop S --qty N [--target T] [--setup vcp] [--notes ".."]
  journal.py close SYMBOL --exit P [--date YYYY-MM-DD] [--mistake "chased;moved stop"]
  journal.py update SYMBOL --stop S          # record a trailed stop
  journal.py list                            # open positions with live P&L
  journal.py stats [--mode paper|live]       # win rate, avg R, expectancy, profit factor, by setup
  journal.py breaker                         # GREEN / AMBER / RED: can I trade today?

Logic: India `trading-discipline` (2% day / 5% week / 10% month, 3-loss streak),
tradermonty `drawdown-circuit-breaker`, `trade-performance-coach`, `weekly-performance-digest`.
"""
from __future__ import annotations

import argparse
import csv
import re
from datetime import date, timedelta

import common as c

FILE = c.DATA / "journal.csv"
COLS = ["id", "mode", "symbol", "setup", "entry_date", "entry", "stop", "initial_stop", "target", "qty",
        "exit_date", "exit", "charges", "pnl", "r_multiple", "mistakes", "notes"]


def load():
    if not FILE.exists():
        return []
    with FILE.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("entry", "stop", "initial_stop", "target", "exit", "charges", "pnl", "r_multiple"):
            r[k] = float(r[k]) if r.get(k) not in (None, "") else None
        r["qty"] = int(float(r["qty"]))
    return rows


def save(rows):
    with FILE.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLS})


def open_trades(mode=None):
    return [r for r in load() if r["exit"] is None and (mode is None or r["mode"] == mode)]


def closed(rows, mode=None):
    return [r for r in rows if r["exit"] is not None and (mode is None or r["mode"] == mode)]


def breaker(capital, cfg, as_of=None, mode=None):
    as_of = as_of or c.now_ist().date()
    rows = closed(load(), mode or cfg.get("mode"))
    def pnl_since(d0):
        return sum(r["pnl"] for r in rows if date.fromisoformat(r["exit_date"]) >= d0
                   and date.fromisoformat(r["exit_date"]) <= as_of)
    day = pnl_since(as_of)
    week = pnl_since(as_of - timedelta(days=as_of.weekday()))
    month = pnl_since(as_of.replace(day=1))
    status, reasons = "GREEN", []
    lim = {"day": cfg["daily_loss_pct"], "week": cfg["weekly_loss_pct"], "month": cfg["monthly_loss_pct"]}
    for name, v in (("day", day), ("week", week), ("month", month)):
        loss_pct = -v / capital * 100
        if loss_pct >= lim[name]:
            status = "RED"
            reasons.append(f"{name} loss {loss_pct:.1f}% ≥ {lim[name]}% limit")
        elif loss_pct >= lim[name] / 2 and status != "RED":
            status = "AMBER"
            reasons.append(f"{name} loss {loss_pct:.1f}% (half of {lim[name]}% limit)")
    streak = 0
    for r in sorted(rows, key=lambda r: (r["exit_date"], r["id"]), reverse=True):
        if r["pnl"] < 0:
            streak += 1
        elif r["pnl"] > 0:
            break
    if streak >= cfg["stop_streak"]:
        last = max(rows, key=lambda r: (r["exit_date"], r["id"]))
        if last["exit_date"] == as_of.isoformat():
            status = "RED"
            reasons.append(f"{streak} losses in a row today — stop for the day (tilt)")
        elif status != "RED":
            status = "AMBER"
            reasons.append(f"{streak}-loss streak open — next trade: best setup only, half size")
    if streak >= 5 and status != "RED":
        reasons.append("5+ loss streak → trade at half size until edge returns")
    # tradermonty trade-performance-coach: half risk for the next 2 trades after a rule violation
    recent = sorted(rows, key=lambda r: (r["exit_date"], r["id"]), reverse=True)[:2]
    if any((r.get("mistakes") or "").strip() for r in recent) and status != "RED":
        status = "AMBER" if status == "GREEN" else status
        reasons.append("Rule broken in one of the last 2 trades → half risk until 2 clean trades")
    return {"status": status, "reasons": reasons, "pnl_day": day, "pnl_week": week,
            "pnl_month": month, "loss_streak": streak}


def stats(rows):
    if not rows:
        return None
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    gw = sum(r["pnl"] for r in wins)
    gl = -sum(r["pnl"] for r in losses)
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for r in sorted(rows, key=lambda r: r["exit_date"]):
        eq += r["pnl"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"trades": len(rows), "win_rate": len(wins) / len(rows) * 100,
            "avg_win": gw / len(wins) if wins else 0, "avg_loss": -gl / len(losses) if losses else 0,
            "profit_factor": gw / gl if gl else float("inf"), "net": gw - gl,
            "avg_r": sum(rs) / len(rs) if rs else None, "expectancy_r": sum(rs) / len(rs) if rs else None,
            "max_drawdown": mdd}


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    g = sp.add_parser("config")
    g.add_argument("--capital", type=float)
    g.add_argument("--risk-pct", type=float)
    g.add_argument("--mode", choices=["paper", "live"])
    g.add_argument("--max-positions", type=int)
    g.add_argument("--universe")
    o = sp.add_parser("open")
    o.add_argument("symbol", type=str.upper)
    o.add_argument("--entry", type=float, required=True)
    o.add_argument("--stop", type=float, required=True)
    o.add_argument("--qty", type=int, required=True)
    o.add_argument("--target", type=float)
    o.add_argument("--setup", default="")
    o.add_argument("--date")
    o.add_argument("--notes", default="")
    o.add_argument("--mode", choices=["paper", "live"])
    o.add_argument("--mistake", default="")
    x = sp.add_parser("close")
    x.add_argument("symbol", type=str.upper)
    x.add_argument("--exit", type=float, required=True)
    x.add_argument("--date")
    x.add_argument("--mistake", default="")
    u = sp.add_parser("update")
    u.add_argument("symbol", type=str.upper)
    u.add_argument("--stop", type=float, required=True)
    sp.add_parser("list")
    s = sp.add_parser("stats")
    s.add_argument("--mode", choices=["paper", "live"])
    sp.add_parser("breaker")
    a = ap.parse_args()
    cfg = c.load_config()
    rows = load()

    if a.cmd == "config":
        for k in ("capital", "risk_pct", "mode", "max_positions", "universe"):
            v = getattr(a, k, None)
            if v is not None:
                cfg[k] = v
        c.save_config(cfg)
        print("config:", {k: cfg[k] for k in ("capital", "risk_pct", "mode", "max_positions", "universe",
                                                "daily_loss_pct", "weekly_loss_pct", "monthly_loss_pct")})
    elif a.cmd == "open":
        if a.stop >= a.entry:
            c.die("stop must be below entry (long trades only)")
        rid = max([int(r["id"]) for r in rows], default=0) + 1
        rows.append({"id": rid, "mode": a.mode or cfg["mode"], "symbol": a.symbol, "setup": a.setup,
                     "entry_date": a.date or c.now_ist().date().isoformat(), "entry": a.entry,
                     "stop": a.stop, "initial_stop": a.stop, "target": a.target, "qty": a.qty,
                     "exit": None, "notes": a.notes, "mistakes": a.mistake})
        save(rows)
        risk = (a.entry - a.stop) * a.qty
        print(f"opened #{rid} [{a.mode or cfg['mode']}] {a.symbol} {a.qty} @ {c.inr(a.entry, 2)}, "
              f"stop {c.inr(a.stop, 2)} → risk {c.inr(risk)}"
              + (f" ({risk / cfg['capital'] * 100:.2f}% of capital)" if cfg["capital"] else ""))
    elif a.cmd == "close":
        op = [r for r in rows if r["symbol"] == a.symbol and r["exit"] is None]
        if not op:
            c.die(f"no open trade for {a.symbol}")
        r = op[0]
        ch = c.charges("delivery", r["entry"], a.exit, r["qty"])
        r.update(exit=a.exit, exit_date=a.date or c.now_ist().date().isoformat(),
                 charges=round(ch["total"], 2), pnl=round(ch["net_pnl"], 2),
                 mistakes="; ".join(filter(None, [(r.get("mistakes") or "").strip(), a.mistake.strip()])))
        R = (r["entry"] - r["initial_stop"]) * r["qty"]
        r["r_multiple"] = round(r["pnl"] / R, 2) if R > 0 else None
        save(rows)
        print(f"closed {a.symbol}: net {c.inr(r['pnl'])} after {c.inr(ch['total'])} charges = {r['r_multiple']}R")
    elif a.cmd == "update":
        op = [r for r in rows if r["symbol"] == a.symbol and r["exit"] is None]
        if not op:
            c.die(f"no open trade for {a.symbol}")
        if a.stop < op[0]["stop"]:
            c.die(f"refusing to LOWER the stop ({op[0]['stop']} → {a.stop}). Stops only ratchet up.")
        op[0]["stop"] = a.stop
        save(rows)
        print(f"{a.symbol} stop raised to {c.inr(a.stop, 2)}")
    elif a.cmd == "list":
        op = open_trades()
        if not op:
            print("no open positions")
            return
        px = c.history([r["symbol"] for r in op], period="1mo", max_age_hours=0.25)
        tot_val = tot_pnl = tot_risk = 0
        print(f"{'SYMBOL':<12}{'MODE':<6}{'QTY':>6}{'ENTRY':>10}{'LAST':>10}{'STOP':>10}{'P&L net':>12}{'R':>6}  SINCE")
        for r in op:
            last = float(px[r["symbol"]]["Close"].iloc[-1]) if r["symbol"] in px else r["entry"]
            pnl = c.charges("delivery", r["entry"], last, r["qty"])["net_pnl"]
            R = (r["entry"] - r["initial_stop"]) * r["qty"]
            tot_val += last * r["qty"]
            tot_pnl += pnl
            tot_risk += max(0, (last - r["stop"]) * r["qty"])
            print(f"{r['symbol']:<12}{r['mode']:<6}{r['qty']:>6}{r['entry']:>10,.2f}{last:>10,.2f}{r['stop']:>10,.2f}"
                  f"{pnl:>12,.0f}{pnl / R if R else 0:>6.2f}  {r['entry_date']}")
        cap = cfg["capital"] or 1
        print(f"\nDeployed {c.inr(tot_val)} ({tot_val / cap * 100:.0f}% of capital) · unrealised {c.inr(tot_pnl)} · "
              f"open risk to stops {c.inr(tot_risk)} ({tot_risk / cap * 100:.1f}%)")
    elif a.cmd == "stats":
        cl = closed(rows, a.mode)
        st = stats(cl)
        if not st:
            print("no closed trades yet")
            return
        print(f"\nPERFORMANCE ({a.mode or 'all'}) — {st['trades']} closed trades")
        print(f"Win rate {st['win_rate']:.0f}% · avg win {c.inr(st['avg_win'])} · avg loss {c.inr(st['avg_loss'])} · "
              f"profit factor {st['profit_factor']:.2f}")
        print(f"Net {c.inr(st['net'])} · expectancy {st['expectancy_r']:.2f}R per trade · max drawdown {c.inr(st['max_drawdown'])}")
        by = {}
        for r in cl:
            by.setdefault(r["setup"] or "untagged", []).append(r)
        print("\nBy setup:")
        for k, v in by.items():
            s2 = stats(v)
            print(f"  {k:<14} {s2['trades']:>3} trades · win {s2['win_rate']:.0f}% · "
                  f"{(s2['expectancy_r'] or 0):+.2f}R · net {c.inr(s2['net'])}")
        clean = [r for r in cl if not (r.get("mistakes") or "").strip()]
        print(f"\nRule adherence: {len(clean)}/{len(cl)} trades with no rule break "
              f"({len(clean) / len(cl) * 100:.0f}%) — target 100%")
        slip = [float(m.group(1)) for r in cl if (m := re.search(r"([+-][\d.]+)% vs limit", str(r.get("notes") or "")))]
        if slip:
            print(f"Fill vs alert limit: avg {sum(slip) / len(slip):+.2f}% over {len(slip)} alerted buys (≤0 is good)")
        mist = {}
        for r in cl:
            for m in filter(None, (r.get("mistakes") or "").split(";")):
                mist.setdefault(m.strip(), []).append(r["pnl"])
        if mist:
            print("\nCost of mistakes:")
            for k, v in sorted(mist.items(), key=lambda kv: sum(kv[1])):
                print(f"  {k:<20} {len(v)}× · {c.inr(sum(v))}")
        if st["trades"] < 30:
            print(f"\nOnly {st['trades']} trades — need ~30+ before these numbers mean much.")
    elif a.cmd == "breaker":
        if not cfg["capital"]:
            c.die("set capital first: journal.py config --capital 500000")
        b = breaker(cfg["capital"], cfg)
        print(f"\nCIRCUIT BREAKER: {b['status']}  ({cfg['mode']} mode)")
        print(f"P&L today {c.inr(b['pnl_day'])} · this week {c.inr(b['pnl_week'])} · this month {c.inr(b['pnl_month'])} · "
              f"loss streak {b['loss_streak']}")
        for r in b["reasons"]:
            print(f"  ! {r}")
        if b["status"] == "GREEN":
            print("You may trade today, within the regime's limits.")


if __name__ == "__main__":
    main()
