#!/usr/bin/env python3
"""Survivorship-free NSE price data from the exchange's own daily bhavcopy archive.

  bhav.py download --from 2015-06-01      # fetch daily files into data/cache/bhav/ (resumable)
  bhav.py build                           # parse → split/bonus-adjusted OHLCV panel incl. delisted stocks
  bhav.py universe --size 200             # point-in-time top-N universe, rebalanced semi-annually

Why: Yahoo only has stocks that still trade, and "today's NIFTY 200" is a list of survivors.
Bhavcopy lists every EQ/BE/BZ stock that traded on each day, including later delistings.

Adjustment: bonuses and face-value splits come from the exchange's corporate-action (Bc) lists in
the daily PR archive, each checked against the actual overnight price move. A backstop catches
unlisted ones (a ≥ 30% overnight drop that matches a common ratio). Dividends are not adjusted.
Symbol renames are stitched by ISIN.

Universe proxy: NIFTY 200 is chosen by free-float market cap, which bhavcopy lacks. We rank by
6-month average traded value instead (needs ≥ 6 months of trading history), refreshed at the end
of Feb and Aug, mirroring NSE's semi-annual reconstitution. It is a proxy, not the real index list.
"""
from __future__ import annotations

import argparse
import io
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd
import urllib.request

import common as c

BHAV = c.CACHE / "bhav"
BHAV.mkdir(parents=True, exist_ok=True)
BC = BHAV / "bc"
BC.mkdir(exist_ok=True)
PANEL = c.CACHE / "bhav_panel.pkl"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SERIES = ("EQ", "BE", "BZ")
UDIFF_FROM = date(2024, 7, 8)


def url(d: date) -> str:
    if d >= UDIFF_FROM:
        return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    return (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/"
            f"{d.strftime('%b').upper()}/cm{d:%d}{d.strftime('%b').upper()}{d.year}bhav.csv.zip")


def pr_url(d: date) -> str:
    return f"https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"


def _get(u: str):
    """bytes, or None on 404. Raises IOError after retries on other errors."""
    for attempt in range(4):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 + 3 * attempt)
        except Exception:
            time.sleep(2 + 3 * attempt)
    raise IOError(u)


def fetch(d: date) -> str:
    out, miss = BHAV / f"{d:%Y%m%d}.zip", BHAV / f"{d:%Y%m%d}.none"
    if out.exists() or miss.exists():
        return "cached"
    try:
        blob = _get(url(d))
    except IOError:
        return "fail"
    if blob is None:
        if d < date.today() - timedelta(days=3):
            miss.touch()  # holiday / weekend: remember, don't re-ask
        return "holiday"
    zipfile.ZipFile(io.BytesIO(blob))  # validate
    out.write_bytes(blob)
    return "ok"


def fetch_bc(d: date) -> str:
    """Corporate-action (book closure) list from that day's PR archive; keeps only the Bc csv."""
    out, miss = BC / f"{d:%Y%m%d}.csv", BC / f"{d:%Y%m%d}.none"
    if out.exists() or miss.exists():
        return "cached"
    try:
        blob = _get(pr_url(d))
    except IOError:
        return "fail"
    if blob is None:
        miss.touch()
        return "missing"
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().startswith("bc")]
        if not names:
            miss.touch()
            return "missing"
        out.write_bytes(z.read(names[0]))
    return "ok"


def _pool(fn, items, workers, label):
    stats: dict[str, int] = {}
    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(fn, items), 1):
            stats[res] = stats.get(res, 0) + 1
            if i % 250 == 0:
                print(f"{label} {i}/{len(items)} {stats}", flush=True)
    print(f"{label} done {stats}", flush=True)


def _day(stem: str) -> date:
    return date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))


def download(a):
    d0, d1 = date.fromisoformat(a.start), date.today()
    days = [d0 + timedelta(i) for i in range((d1 - d0).days + 1)]  # weekends too: budget/Muhurat sessions
    _pool(fetch, days, a.workers, "bhavcopy")
    trading = sorted(d for d in (_day(p.stem) for p in BHAV.glob("*.zip")) if d >= d0)
    _pool(fetch_bc, sorted(set(trading[:: a.ca_every] + trading[-5:])), a.workers, "corp-actions")


def parse(p) -> pd.DataFrame:
    with zipfile.ZipFile(p) as z:
        df = pd.read_csv(z.open(z.namelist()[0]))
    df.columns = [x.strip() for x in df.columns]
    if "TckrSymb" in df.columns:
        df = df.rename(columns={"TckrSymb": "sym", "SctySrs": "ser", "OpnPric": "o", "HghPric": "h",
                                "LwPric": "l", "ClsPric": "c", "PrvsClsgPric": "prev",
                                "TtlTradgVol": "v", "TtlTrfVal": "val", "ISIN": "isin"})
    else:
        df = df.rename(columns={"SYMBOL": "sym", "SERIES": "ser", "OPEN": "o", "HIGH": "h", "LOW": "l",
                                "CLOSE": "c", "PREVCLOSE": "prev", "TOTTRDQTY": "v", "TOTTRDVAL": "val",
                                "ISIN": "isin"})
    df["ser"] = df["ser"].astype(str).str.strip()
    df["isin"] = df["isin"].astype(str).str.strip()
    df["sym"] = df["sym"].astype(str).str.strip()
    # company shares only: INE… ISINs (ETFs/mutual-fund units are INF…, gold bonds IN00…)
    df = df[df["ser"].isin(SERIES) & df["isin"].str.startswith("INE")]
    df = df[["sym", "ser", "isin", "o", "h", "l", "c", "prev", "v", "val"]].copy()
    df["date"] = pd.Timestamp(p.stem)
    # one row per symbol per day, prefer EQ
    df["rank"] = df["ser"].map({"EQ": 0, "BE": 1, "BZ": 2})
    return df.sort_values("rank").drop_duplicates("sym").drop(columns="rank")


def stitch_renames(raw: pd.DataFrame) -> pd.DataFrame:
    """Old symbol → new symbol when the same ISIN continues under a new ticker."""
    span = raw.groupby("sym")["date"].agg(["min", "max"])
    by_isin = raw.groupby("isin")["sym"].unique()
    mapping = {}
    for syms in by_isin:
        if len(syms) < 2:
            continue
        order = sorted(syms, key=lambda s: span.at[s, "min"])
        for old, new in zip(order, order[1:]):
            if span.at[old, "max"] < span.at[new, "min"]:  # no overlap → rename
                mapping[old] = new
    def final(s):
        seen = set()
        while s in mapping and s not in seen:
            seen.add(s)
            s = mapping[s]
        return s
    if mapping:
        raw["sym"] = raw["sym"].map(lambda s: final(s) if s in mapping else s)
        raw = raw.sort_values(["sym", "date", "val"]).drop_duplicates(["sym", "date"], keep="last")
    print(f"renames stitched: {len(mapping)}")
    return raw, {k: final(k) for k in mapping}


_BONUS = re.compile(r"BONUS\D*?(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
_SPLIT = re.compile(r"SPLT\b|SPLIT|SUB-?DIV")  # not SPLDV / SPL (special dividend)
_NUM = re.compile(r"(\d+(?:\.\d+)?)")
# common split/bonus price ratios (old price / new price), used by the backstop detector
_COMMON = np.array([1.25, 4 / 3, 1.5, 5 / 3, 2, 2.5, 3, 4, 5, 6, 8, 10, 20])


def ca_factor(purpose: str) -> float | None:
    """Price divisor for a bonus or face-value split, e.g. 'BONUS 1:1' → 2, 'FV SPLT FRM RS 10 TO RS 2' → 5."""
    u = purpose.upper()
    if "DEMERGER" in u or "DE-MERGER" in u:
        return -2.0   # demerger: value moves to new shares; adjust by the ex-date price change
    f = 1.0
    m = _BONUS.search(u)
    if m:
        a_, b_ = float(m.group(1)), float(m.group(2))
        if b_ > 0:
            f *= (a_ + b_) / b_
    m = _SPLIT.search(u)
    if m:
        nums = [float(x) for x in _NUM.findall(u[m.start():])]
        if len(nums) >= 2 and nums[1] > 0 and nums[0] > nums[1]:
            f *= nums[0] / nums[1]
        else:
            return -1.0   # split with the ratio cut off (the PURPOSE field is truncated): infer from price
    return f if f > 1.0001 else None


def corp_actions(mapping: dict) -> dict:
    """{symbol: {ex_date: factor}} from all downloaded Bc files (EQ/BE rows, deduplicated)."""
    rows = []
    for p in sorted(BC.glob("*.csv")):
        try:
            df = pd.read_csv(p, dtype=str, on_bad_lines="skip", encoding_errors="replace")
        except Exception:
            continue
        df.columns = [x.strip().upper() for x in df.columns]
        if not {"SYMBOL", "EX_DT", "PURPOSE"} <= set(df.columns):
            continue
        rows.append(df[["SYMBOL", "EX_DT", "PURPOSE"]])
    ca = pd.concat(rows).dropna()
    ca = ca.apply(lambda col: col.str.strip()).drop_duplicates()
    ex1 = pd.to_datetime(ca["EX_DT"], format="%d/%m/%Y", errors="coerce")
    ca["ex"] = ex1.fillna(pd.to_datetime(ca["EX_DT"], format="%Y-%m-%d", errors="coerce"))  # newer files: ISO
    ca["f"] = ca["PURPOSE"].map(ca_factor)
    ca = ca.dropna(subset=["ex", "f"])
    # one PURPOSE can be split across rows ("BONUS 4:1" + "FV SPLT ..."): product per symbol & ex-date
    out: dict = {}
    for (sym, ex), g in ca.groupby(["SYMBOL", "ex"]):
        sym = mapping.get(sym, sym)
        fs = g.drop_duplicates("PURPOSE")["f"]
        out.setdefault(sym, {})[ex] = (-2.0 if (fs == -2).any() else -1.0 if (fs < 0).any()
                                        else float(np.prod(fs)))
    return out


STATS = {"ca_applied": 0, "ca_rejected": 0, "heuristic": 0}


def adjust(g: pd.DataFrame, events: dict) -> pd.DataFrame:
    """Back-adjust for bonuses/splits. Uses the exchange's corporate-action list, checked against the
    actual overnight price change; a backstop catches unlisted ones (≥ 30% drop matching a common ratio)."""
    g = g.sort_values("date")
    dates = pd.DatetimeIndex(g["date"].to_numpy())
    c_ = g["c"].to_numpy(float)
    o_ = g["o"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        qo = np.r_[np.nan, c_[:-1] / o_[1:]]   # prev close / today's open
        qc = np.r_[np.nan, c_[:-1] / c_[1:]]   # prev close / today's close

    def err(i, f):  # how far the actual overnight/day move is from a factor f (log units)
        e = [abs(np.log(x / f)) for x in (qo[i], qc[i]) if np.isfinite(x) and x > 0]
        return min(e) if e else np.inf

    def nearest(i):
        k = min(_COMMON, key=lambda k: err(i, k))
        return k, err(i, k)

    ratio = np.ones(len(g))
    for ex, f in events.items():
        i = dates.searchsorted(ex)
        if i <= 0 or i >= len(g):
            continue
        if f == -2:                     # demerger: prev close / ex-date open (NSE's special pre-open)
            f = qo[i] if np.isfinite(qo[i]) else 1.0
            ok = 1.02 < f < 20
            STATS["demerger"] = STATS.get("demerger", 0) + int(ok)
        elif f < 0:                     # truncated split text: take the common ratio the price shows
            f, e = nearest(i)
            ok = e < 0.06
        else:
            ok = err(i, f) < 0.15       # the price really moved by ~f
        if ok:
            ratio[i] = f
            STATS["ca_applied"] += 1
        else:
            STATS["ca_rejected"] += 1
    big = np.where((ratio == 1) & ((qo >= 1.42) | (qc >= 1.42)))[0]
    for i in big:
        k, e = nearest(i)
        if e < 0.06 and min(qo[i], qc[i]) >= 1.2:   # both open and close confirm a large drop
            ratio[i] = k
            STATS["heuristic"] += 1
    f = np.cumprod(np.r_[ratio[1:], 1.0][::-1])[::-1]   # divisor for each day = product of later events
    return pd.DataFrame({"Open": o_ / f, "High": g["h"].to_numpy(float) / f,
                         "Low": g["l"].to_numpy(float) / f, "Close": c_ / f,
                         "Volume": g["v"].to_numpy(float) * f, "Value": g["val"].to_numpy(float)},
                        index=dates)


def build(a):
    files = sorted(BHAV.glob("*.zip"))
    print(f"parsing {len(files)} files…", flush=True)
    with ThreadPoolExecutor(8) as ex:
        raw = pd.concat(ex.map(parse, files), ignore_index=True)
    raw, mapping = stitch_renames(raw)
    ca = corp_actions(mapping)
    print(f"corporate actions (bonus/split) parsed: {sum(len(v) for v in ca.values())} for {len(ca)} symbols")
    panel = {s: adjust(g, ca.get(s, {})) for s, g in raw.groupby("sym")}
    print(f"adjustments: {STATS}")
    pd.to_pickle(panel, PANEL)
    last = raw["date"].max()
    alive = sum(1 for d in panel.values() if d.index[-1] == last)
    print(f"panel: {len(panel)} symbols ({alive} trading on {last:%Y-%m-%d}, {len(panel) - alive} stopped) → {PANEL}")


def load_panel() -> dict:
    return pd.read_pickle(PANEL)


def pit_universe(panel: dict, size: int = 200, min_days: int = 120) -> pd.Series:
    """Series indexed by rebalance date → frozenset of members, effective the next trading day."""
    val = pd.DataFrame({s: d["Value"] for s, d in panel.items()})
    val = val.sort_index()
    avg = val.rolling(126, min_periods=min_days).mean()
    dates = val.index
    reb = [dates[dates <= pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)].max()
           for y in range(dates[0].year, dates[-1].year + 1) for m in (2, 8)]
    reb = sorted({r for r in reb if pd.notna(r) and r >= dates[min_days]})
    out = {}
    for r in reb:
        row = avg.loc[r].dropna()
        live = val.loc[r].dropna().index            # must still be trading on the rebalance day
        out[r] = frozenset(row[row.index.isin(live)].nlargest(size).index)
    return pd.Series(out)


def universe_cmd(a):
    u = pit_universe(load_panel(), a.size)
    cur = set(c.universe("nifty200")["Symbol"])
    for r, m in u.items():
        print(f"{r:%Y-%m-%d}: {len(m)} members, {len(m & cur)} also in today's NIFTY 200")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download"); d.add_argument("--from", dest="start", default="2015-06-01")
    d.add_argument("--workers", type=int, default=4)
    d.add_argument("--ca-every", type=int, default=3, help="fetch the corporate-action list every Nth trading day")
    sub.add_parser("build")
    u = sub.add_parser("universe"); u.add_argument("--size", type=int, default=200)
    a = ap.parse_args()
    {"download": download, "build": build, "universe": universe_cmd}[a.cmd](a)
