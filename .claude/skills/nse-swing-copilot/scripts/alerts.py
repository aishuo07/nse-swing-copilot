#!/usr/bin/env python3
"""Buy/sell ALERTS for the nse-swing-copilot system. It never places orders — it tells you
exactly what to do and when, and you place the order in Kite.

Schedule (installed by `alerts.py install`, weekdays, IST):
  16:30  evening  — loss limits + market check + manage open trades + scan → tomorrow's BUY list
  09:16  morning  — "BUY NOW" for 52-week-high signals (or SKIP if it gapped above the limit)
  09:15–15:30 watch (every 60 s)
                  — "SELL NOW" when a stop is hit · "BUY NOW" when a breakout crosses its trigger ·
                    15:15 "SELL before close" when a 52w-high trade is closing below its 50 DMA

After you act, tell the system:
  alerts.py took SYMBOL --price 349.8      # logs the buy with the signal's stop/qty/setup
  alerts.py sold SYMBOL --price 371.2      # logs the sale
Other:
  alerts.py status | test | phone | whatsapp | telegram | install | uninstall | evening | morning | watch [--once]

Phone alerts: `alerts.py phone` (ntfy app push), `alerts.py whatsapp` (CallMeBot), or
`alerts.py telegram`. Each channel is on when its data/<name>.json exists. Desktop always on.
Prices: Yahoo 1-minute bars (near real time, can lag). Your GTT stop in Kite is the real
protection — these alerts are the backup and the timing guide.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

import common as c
import journal

HERE = Path(__file__).resolve().parent
PY = sys.executable
SIG = "signals"
LOG = c.DATA / "alerts.log"
REPORTS = c.DATA / "reports"
REPORTS.mkdir(exist_ok=True)
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL = "com.nseswingcopilot"


# ------------------------------------------------------------------ notifications
def notify(title: str, msg: str, urgent: bool = False):
    stamp = c.now_ist().strftime("%d-%b %H:%M:%S")
    with LOG.open("a") as f:
        f.write(f"[{stamp}] {'URGENT ' if urgent else ''}{title}: {msg}\n")
    print(f"[{stamp}] {title}: {msg}")
    safe = lambda s: s.replace("\\", "").replace('"', "'")
    try:
        subprocess.run(["osascript", "-e", f'display notification "{safe(msg)}" with title "{safe(title)}" '
                        f'sound name "{"Sosumi" if urgent else "Glass"}"'], timeout=10, check=False)
        if urgent:
            subprocess.Popen(["say", title.replace("🔴", "").replace("🟢", "")])
    except Exception:
        pass
    for name, fn in CHANNELS.items():
        cfg_path = c.DATA / f"{name}.json"
        if cfg_path.exists():
            try:
                fn(json.loads(cfg_path.read_text()), title, msg, urgent)
            except Exception as e:
                with LOG.open("a") as f:
                    f.write(f"[{stamp}] {name} failed: {e}\n")


def _telegram(cfg, title, msg, urgent):
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": f"{title}\n{msg}"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", data, timeout=15)


def _ntfy(cfg, title, msg, urgent):
    """Push to the free ntfy phone app (iOS/Android). Uses JSON publish so emoji titles work."""
    body = json.dumps({"topic": cfg["topic"], "title": title, "message": msg,
                       "priority": 5 if urgent else 3,
                       "tags": ["rotating_light"] if urgent else ["chart_with_upwards_trend"]}).encode()
    req = urllib.request.Request(cfg.get("server", "https://ntfy.sh"), body,
                                 {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def _whatsapp(cfg, title, msg, urgent):
    """WhatsApp to your own number via the free CallMeBot service (you register your number with it)."""
    q = urllib.parse.urlencode({"phone": cfg["phone"], "apikey": cfg["apikey"],
                                "text": f"*{title}*\n{msg}"})
    r = urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{q}", timeout=30)
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}")


CHANNELS = {"ntfy": _ntfy, "whatsapp": _whatsapp, "telegram": _telegram}


# ------------------------------------------------------------------ helpers
def live_prices(symbols):
    """Latest 1-minute price for today. Returns {sym: (price, bar_time)}; empty if no session today."""
    import yfinance as yf
    if not symbols:
        return {}
    tick = [c.yf_symbol(s) for s in symbols]
    raw = yf.download(tick, period="1d", interval="1m", progress=False, group_by="ticker", auto_adjust=False)
    out = {}
    for s, t in zip(symbols, tick):
        try:
            d = (raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw).dropna(subset=["Close"])
        except Exception:
            continue
        if len(d) and d.index[-1].date() == c.now_ist().date():
            out[s] = (float(d["Close"].iloc[-1]), d.index[-1].strftime("%H:%M"))
    return out


def sma50_with_price(sym, price):
    df = c.completed(c.one(sym))
    closes = list(df["Close"].iloc[-49:]) + [price]
    return sum(closes) / len(closes)


def load_sig():
    s = c.load_state(SIG) or {}
    s.setdefault("buys", [])
    s.setdefault("fired", {})            # {"YYYY-MM-DD": ["key", ...]} de-duplication
    return s


def fired(sig, key):
    today = c.now_ist().date().isoformat()
    return key in sig["fired"].get(today, [])


def mark(sig, key):
    today = c.now_ist().date().isoformat()
    sig["fired"] = {today: sig["fired"].get(today, []) + [key]}


def plan_json(sym, setup="auto"):
    r = subprocess.run([PY, str(HERE / "plan.py"), "plan", sym, "--json", "--setup", setup],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def manage_one(t):
    """Decide what to tell the user about an open trade at the evening review."""
    df = c.completed(c.one(t["symbol"]))
    cl = df["Close"]
    last = float(cl.iloc[-1])
    a14 = float(c.atr(df).iloc[-1])
    s50 = float(c.sma(cl, 50).iloc[-1])
    R = t["entry"] - t["initial_stop"]
    seg = df.loc[pd.Timestamp(t["entry_date"]):]
    hc = float(seg["Close"].max()) if len(seg) else last
    r_peak = (hc - t["entry"]) / R if R > 0 else 0
    new_stop = t["stop"]
    if r_peak >= 1:
        new_stop = max(new_stop, t["entry"] * 1.003)
    if t["setup"] != "high52" and r_peak >= 2:
        mkt = c.load_state("market") or {}
        mult = 2.0 if mkt.get("label") == "Confirmed uptrend" else 1.5
        new_stop = max(new_stop, hc - mult * a14)
    new_stop = c.round_tick(new_stop, down=True)
    msgs = []
    if t["setup"] == "high52" and last < s50:
        msgs.append(("🔴 SELL at 09:15 open", f"{t['symbol']} closed {last:.2f} below 50 DMA {s50:.2f} — "
                     f"sell all {t['qty']} at tomorrow's open (exit rule)", True))
    elif last <= t["stop"]:
        msgs.append(("🔴 SELL at 09:15 open", f"{t['symbol']} closed {last:.2f} at/below stop {t['stop']:.2f}", True))
    elif new_stop > t["stop"]:
        msgs.append(("Raise stop", f"{t['symbol']}: modify your Kite GTT stop {t['stop']:.2f} → {new_stop:.2f} "
                     f"(peak {r_peak:+.1f}R)", False))
    return new_stop, msgs, last, (last - t["entry"]) / R if R > 0 else 0


# ------------------------------------------------------------------ jobs
def evening(_a=None):
    import market as mk
    import scan as sc
    if not c.is_trading_day(c.now_ist().date()):
        print("not a trading day — no evening plan")
        return
    cfg = c.load_config()
    if not cfg["capital"]:
        notify("Setup needed", "Set capital: ./zt journal config --capital 100000")
        return
    today = c.now_ist().date()
    lines = [f"# Trading plan for next session — generated {c.now_ist():%d-%b-%Y %H:%M} IST", ""]
    sig = load_sig()
    # 1) open trades
    rows = journal.load()
    opens = [r for r in rows if r["exit"] is None]
    lines.append("## Open positions")
    if not opens:
        lines.append("None.")
    for t in opens:
        new_stop, msgs, last, rnow = manage_one(t)
        lines.append(f"- {t['symbol']} ({t['setup']}): {t['qty']} @ {t['entry']:.2f}, close {last:.2f} ({rnow:+.2f}R), "
                     f"stop {t['stop']:.2f}" + (f" → **raise to {new_stop:.2f}**" if new_stop > t["stop"] else ""))
        if new_stop > t["stop"]:
            t["stop"] = new_stop
        for title, m, urgent in msgs:
            notify(title, m, urgent)
            lines.append(f"  - **{title}**: {m}")
    journal.save(rows)
    # 2) loss limits + market
    br = journal.breaker(cfg["capital"], cfg)
    p = mk.posture()
    c.save_state("market", p)
    lines += ["", "## Market", f"- Regime (info): {p['label']} · sizing at {cfg['risk_pct']}% risk/trade",
              f"- System filter (NIFTY > 50 DMA): {'ON' if p['system_filter_on'] else 'OFF'} · "
              f"NIFTY {p['trend']['close']:,} (50 DMA {p['trend']['close'] / (1 + p['trend']['vs_50dma_pct'] / 100):,.0f}, "
              f"200 DMA {p['trend']['close'] / (1 + p['trend']['vs_200dma_pct'] / 100):,.0f})",
              f"- Loss limits: {br['status']} " + "; ".join(br["reasons"])]
    buys = []
    # tested system: up to max_positions; the regime only vetoes entries in a confirmed correction
    slots = 0 if p["max_new_positions"] == 0 else cfg["max_positions"] - len(opens)
    if br["status"] == "RED":
        reason = "loss limit hit (RED) — no new trades tomorrow"
    elif not p["system_filter_on"]:
        reason = "system filter OFF (NIFTY below its 50 DMA) — no new trades"
    elif slots <= 0:
        reason = ("market in correction — no new trades" if p["max_new_positions"] == 0
                  else "all position slots are full")
    else:
        reason = None
        u = c.universe(cfg["universe"])
        rows_s, _ = sc.run(list(u["Symbol"]), 40, None, 10.0, dict(zip(u["Symbol"], u["Industry"])))
        held = {t["symbol"] for t in opens}
        # cash: like the backtest, buy only what the money left covers (CNC, no leverage)
        cash = cfg["capital"] - sum(t["entry"] * t["qty"] for t in opens)
        # high52 only: breakouts made ~4% CAGR on survivorship-free data (references/sources.md)
        cands = [r for r in rows_s if r["symbol"] not in held and "high52" in r["setups"]]
        for r in cands:
            if len(buys) >= slots:
                break
            setup = "high52" if "high52" in r["setups"] else "breakout"
            pl = plan_json(r["symbol"], setup)
            if not pl or pl["gate"] == "BLOCK" or pl["qty"] <= 0:
                continue
            qty = min(pl["qty"], int(cash // pl["chase_limit"]))
            if qty <= 0:
                continue   # not enough cash left for even 1 share; a cheaper candidate may still fit
            if qty < pl["qty"]:
                pl["loss_at_stop_net"] = round(pl["loss_at_stop_net"] * qty / pl["qty"])
                pl["position_value"] = round(pl["position_value"] * qty / pl["qty"])
                pl["warnings"] = pl["warnings"] + [f"qty cut to {qty} — cash left ₹{cash:,.0f}"]
                pl["qty"] = qty
            cash -= qty * pl["chase_limit"]
            buys.append({"symbol": r["symbol"], "setup": "high52" if setup == "high52" else
                         ("vcp" if "vcp" in r["setups"] else "flat-base"),
                         "trigger": pl["trigger"], "limit": pl["chase_limit"], "stop": pl["stop"], "qty": pl["qty"],
                         "risk": pl["loss_at_stop_net"], "value": pl["position_value"],
                         "warnings": pl["warnings"], "created": today.isoformat(), "status": "pending"})
    sig["buys"] = buys
    sig["created"] = today.isoformat()
    c.save_state(SIG, sig)
    lines += ["", "## Tomorrow's buy signals"]
    if reason:
        lines.append(f"None — {reason}.")
        notify("Evening plan ready", f"No new buys tomorrow: {reason}. Open trades: {len(opens)}.")
    for b in buys:
        when = ("at 09:15 open — alert at 09:16" if b["setup"] == "high52"
                else f"only if price crosses {b['trigger']:.2f} during the day — alert when it happens")
        lines.append(f"- **{b['symbol']}** ({b['setup']}): BUY {b['qty']} {when}; max price {b['limit']:.2f}; "
                     f"stop-loss {b['stop']:.2f}; risk ≈ ₹{b['risk']:,}" + (f" · ⚠ {'; '.join(b['warnings'])}" if b["warnings"] else ""))
    if not reason and not buys:
        lines.append("None — no candidate passed the plan gate (setup quality, price vs capital, results date).")
        notify("Evening plan ready", f"No new buys tomorrow: no stock passed all checks. Open trades: {len(opens)}.")
    if buys:
        notify("Evening plan ready", f"{len(buys)} buy signal(s) for tomorrow: "
               + ", ".join(f"{b['symbol']} ({b['setup']})" for b in buys))
    fw = c.freshness_warnings(today)
    if fw:
        lines += ["", "## Maintenance"] + [f"- ⚠ {w}" for w in fw]
        notify("Maintenance needed", "; ".join(fw))
    lines += ["", "_Educational signals from a rules-based system, not investment advice. You place every order._"]
    rp = REPORTS / f"{today.isoformat()}.md"
    rp.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\nreport: {rp}")


def morning(_a=None):
    today = c.now_ist().date()
    if not c.is_trading_day(today):
        print("NSE closed today — signals stay pending for the next session")
        return
    sig = load_sig()
    pend = [b for b in sig["buys"] if b["status"] == "pending" and b["setup"] == "high52"]
    if not pend:
        return
    expected = c.prev_trading_day(today).isoformat()
    stale = [b for b in pend if b["created"] != expected]
    for b in stale:
        b["status"] = "expired"
    if stale:
        notify("Old signals ignored", f"{', '.join(b['symbol'] for b in stale)}: plan was from {stale[0]['created']}, "
               f"not last evening ({expected}) — evening job didn't run. No buy today.")
        pend = [b for b in pend if b not in stale]
        c.save_state(SIG, sig)
        if not pend:
            return
    px = live_prices([b["symbol"] for b in pend])
    if not px:
        print("no session data today (holiday?)")
        return
    for b in pend:
        if b["symbol"] not in px:
            continue
        price, tm = px[b["symbol"]]
        if price <= b["limit"]:
            notify(f"🟢 BUY NOW {b['symbol']}",
                   f"BUY {b['qty']} {b['symbol']} CNC LIMIT {b['limit']:.2f} (now {price:.2f} at {tm}). "
                   f"Then set GTT stop-loss {b['stop']:.2f}. Log: ./zt alerts took {b['symbol']} --price <fill>", True)
            b["status"] = "alerted"
        else:
            notify(f"SKIP {b['symbol']}", f"Opened {price:.2f}, above max {b['limit']:.2f} — gapped up, do NOT chase.")
            b["status"] = "skipped"
    c.save_state(SIG, sig)


def watch_once(sig, final_check: bool):
    rows = journal.load()
    opens = [r for r in rows if r["exit"] is None]
    pend = [b for b in sig["buys"] if b["status"] == "pending" and b["setup"] != "high52"]
    syms = list({t["symbol"] for t in opens} | {b["symbol"] for b in pend})
    if not syms:
        return True   # nothing to watch right now; keep looping in case a trade is logged later today
    px = live_prices(syms)
    if not px:
        return False
    for t in opens:
        if t["symbol"] not in px:
            continue
        price, tm = px[t["symbol"]]
        if price <= t["stop"] and not fired(sig, "stop:" + t["symbol"]):
            notify(f"🔴 SELL NOW {t['symbol']}",
                   f"Stop-loss {t['stop']:.2f} hit (now {price:.2f} at {tm}). SELL all {t['qty']} at market if your "
                   f"GTT didn't fire. Log: ./zt alerts sold {t['symbol']} --price <fill>", True)
            mark(sig, "stop:" + t["symbol"])
        if final_check and t["setup"] == "high52" and not fired(sig, "ma50:" + t["symbol"]):
            s50 = sma50_with_price(t["symbol"], price)
            if price < s50:
                notify(f"🔴 SELL BEFORE 15:30 {t['symbol']}",
                       f"Trading {price:.2f}, below 50 DMA {s50:.2f} into the close — exit rule: SELL all {t['qty']} "
                       f"now (before 15:30). Log: ./zt alerts sold {t['symbol']} --price <fill>", True)
            mark(sig, "ma50:" + t["symbol"])
    for b in pend:
        if b["symbol"] not in px:
            continue
        price, tm = px[b["symbol"]]
        if price >= b["trigger"] and not fired(sig, "brk:" + b["symbol"]):
            if price <= b["limit"]:
                notify(f"🟢 BUY NOW {b['symbol']}",
                       f"Breakout: {price:.2f} crossed {b['trigger']:.2f} at {tm}. BUY {b['qty']} CNC LIMIT "
                       f"{b['limit']:.2f}, then GTT stop-loss {b['stop']:.2f}. Log: ./zt alerts took {b['symbol']} --price <fill>",
                       True)
                b["status"] = "alerted"
            else:
                notify(f"SKIP {b['symbol']}", f"Already {price:.2f}, above max {b['limit']:.2f} — don't chase.")
                b["status"] = "skipped"
            mark(sig, "brk:" + b["symbol"])
    c.save_state(SIG, sig)
    return True


def watch(a):
    while True:
        n = c.now_ist()
        t = n.hour * 60 + n.minute
        if not c.is_trading_day(n.date()) or t > 15 * 60 + 30:
            break
        if t >= 9 * 60 + 15:
            sig = load_sig()
            ok = watch_once(sig, final_check=t >= 15 * 60 + 15)
            if not ok and t > 9 * 60 + 30:
                print("no live data for today — holiday? exiting")
                break
        if a.once:
            break
        time.sleep(60)


# ------------------------------------------------------------------ user actions
def took(a):
    sig = load_sig()
    b = next((x for x in sig["buys"] if x["symbol"] == a.symbol), None)
    stop = a.stop or (b and b["stop"])
    qty = a.qty or (b and b["qty"])
    setup = (b and b["setup"]) or "manual"
    if not stop or not qty:
        c.die("no signal found for this symbol — pass --stop and --qty")
    notes, mistakes = "", []
    if b:
        diff = (a.price / b["limit"] - 1) * 100
        notes = f"alert {b['created']} limit {b['limit']} stop {b['stop']} qty {b['qty']}; fill {a.price} ({diff:+.2f}% vs limit)"
        if a.price > b["limit"] * 1.001:
            mistakes.append("chased (paid above alert max)")
            print(f"⚠ You paid {a.price}, above the alert's max {b['limit']} — logged as a rule break.")
        if b["status"] in ("skipped", "expired"):
            mistakes.append("took a skipped/expired signal")
        if qty > b["qty"]:
            mistakes.append("oversized")
    else:
        mistakes.append("off-system trade (no alert)")
        print("⚠ No alert for this stock — logged as an off-system trade (rule break).")
    subprocess.run([PY, str(HERE / "journal.py"), "open", a.symbol, "--entry", str(a.price), "--stop", str(stop),
                    "--qty", str(qty), "--setup", setup, "--notes", notes, "--mistake", "; ".join(mistakes)], check=False)
    if b:
        b["status"] = "taken"
        c.save_state(SIG, sig)
    print(f"Now set a GTT stop-loss in Kite: SELL {qty} {a.symbol} trigger {stop}. I'll watch it during market hours.")


def sold(a):
    subprocess.run([PY, str(HERE / "journal.py"), "close", a.symbol, "--exit", str(a.price)]
                   + (["--mistake", a.mistake] if a.mistake else []), check=False)
    print("Remember to cancel any remaining GTT for this stock in Kite.")


def status(_a=None):
    sig = load_sig()
    print(f"Signals from {sig.get('created', '—')}:")
    for b in sig["buys"]:
        print(f"  {b['symbol']:<12}{b['setup']:<10}{b['status']:<9} qty {b['qty']} · trigger {b['trigger']} · "
              f"max {b['limit']} · SL {b['stop']}")
    if not sig["buys"]:
        print("  none")
    op = journal.open_trades()
    print(f"Open positions watched: {', '.join(t['symbol'] + ' SL ' + str(t['stop']) for t in op) or 'none'}")
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    print("Scheduler:", ", ".join(j for j in ("evening", "morning", "watch") if f"{LABEL}.{j}" in r) or "not installed")
    if LOG.exists():
        print("\nLast alerts:\n" + "".join(LOG.read_text().splitlines(True)[-8:]))


# ------------------------------------------------------------------ scheduler (macOS launchd)
JOBS = {"evening": (16, 30), "morning": (9, 16), "watch": (9, 14)}


def install(_a=None):
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    logs = c.DATA / "logs"
    logs.mkdir(exist_ok=True)
    for job, (h, m) in JOBS.items():
        cal = "".join(f"<dict><key>Weekday</key><integer>{d}</integer><key>Hour</key><integer>{h}</integer>"
                      f"<key>Minute</key><integer>{m}</integer></dict>" for d in range(1, 6))
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}.{job}</string>
<key>ProgramArguments</key><array><string>/usr/bin/caffeinate</string><string>-i</string><string>{PY}</string><string>{HERE / 'alerts.py'}</string><string>{job}</string></array>
<key>WorkingDirectory</key><string>{HERE}</string>
<key>StartCalendarInterval</key><array>{cal}</array>
<key>StandardOutPath</key><string>{logs / (job + '.log')}</string>
<key>StandardErrorPath</key><string>{logs / (job + '.err')}</string>
</dict></plist>"""
        p = PLIST_DIR / f"{LABEL}.{job}.plist"
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        p.write_text(plist)
        subprocess.run(["launchctl", "load", str(p)], check=True)
        print(f"installed {job} — weekdays {h:02d}:{m:02d} IST")
    print("Keep the Mac awake (or plugged in) during market hours; missed jobs run when it wakes.")


def uninstall(_a=None):
    for job in JOBS:
        p = PLIST_DIR / f"{LABEL}.{job}.plist"
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        p.unlink(missing_ok=True)
        print(f"removed {job}")


def telegram(_a=None):
    print("""Phone alerts via Telegram (free):
 1. In Telegram, open @BotFather → /newbot → copy the bot TOKEN it gives you.
 2. Send any message to your new bot, then open
    https://api.telegram.org/bot<TOKEN>/getUpdates in a browser and copy "chat":{"id": ...}.
 3. Create data/telegram.json yourself:  {"token": "<TOKEN>", "chat_id": "<ID>"}
 4. Run ./zt alerts test — you should get the message on your phone.""")


def phone(_a=None):
    """Set up push alerts in the free 'ntfy' phone app (no account needed)."""
    import secrets
    p = c.DATA / "ntfy.json"
    cfg = json.loads(p.read_text()) if p.exists() else {"server": "https://ntfy.sh",
                                                        "topic": "zt-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "x").lower()}
    p.write_text(json.dumps(cfg, indent=1))
    print(f"""Phone app alerts (ntfy — free, iPhone + Android):
 1. Install "ntfy" from the App Store / Play Store.
 2. Tap + (Subscribe to topic) and enter exactly:   {cfg['topic']}
    (server: {cfg['server']} — the default). Keep this topic name private: anyone who knows it can read the alerts.
 3. In the app's notification settings, allow notifications (iPhone: also allow Time Sensitive).
 4. Run ./zt alerts test — "Test alert" should pop up on the phone.
Urgent alerts (BUY NOW / SELL NOW) are sent at max priority.""")


def whatsapp(_a=None):
    print("""WhatsApp alerts (free CallMeBot service, sends to your own WhatsApp):
 1. Save +34 644 66 32 62 in your phone contacts (check the current number at callmebot.com/blog/free-api-whatsapp-messages).
 2. From WhatsApp send it:   I allow callmebot to send me messages
 3. It replies with your APIKEY (can take a few minutes).
 4. Create data/whatsapp.json yourself:  {"phone": "+91XXXXXXXXXX", "apikey": "<APIKEY>"}
 5. Run ./zt alerts test — you should get a WhatsApp message.
Note: CallMeBot is a free hobby service — delivery is usually seconds but can lag or rate-limit.
Keep the ntfy phone app as a second channel for SELL NOW alerts.""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("evening", "morning", "status", "install", "uninstall", "telegram", "phone", "whatsapp", "test"):
        sp.add_parser(n)
    w = sp.add_parser("watch")
    w.add_argument("--once", action="store_true")
    t = sp.add_parser("took")
    t.add_argument("symbol", type=str.upper)
    t.add_argument("--price", type=float, required=True)
    t.add_argument("--stop", type=float)
    t.add_argument("--qty", type=int)
    s = sp.add_parser("sold")
    s.add_argument("symbol", type=str.upper)
    s.add_argument("--price", type=float, required=True)
    s.add_argument("--mistake", default="")
    a = ap.parse_args()
    if a.cmd == "test":
        notify("🟢 Test alert", "NSE Swing Copilot alerts are working.", True)
        on = [n for n in CHANNELS if (c.DATA / f"{n}.json").exists()]
        print("Channels: desktop" + "".join(", " + n for n in on) + ("" if on else "  (run: ./zt alerts phone / whatsapp)"))
        print("Any channel failures are written to data/alerts.log")
    else:
        {"evening": evening, "morning": morning, "watch": watch, "took": took, "sold": sold, "status": status,
         "install": install, "uninstall": uninstall, "telegram": telegram, "phone": phone,
         "whatsapp": whatsapp}[a.cmd](a)
