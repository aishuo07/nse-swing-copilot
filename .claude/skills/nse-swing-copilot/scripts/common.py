"""Shared helpers for the nse-swing-copilot skill: config, data, indicators, charges, ticks.

Data comes from Yahoo Finance via yfinance (NSE symbols get a .NS suffix). It is fine for
end-of-day swing analysis; intraday quotes can be delayed ~15 min. Always label prices as EOD/delayed.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[4]          # project folder (…/trader)
DATA = ROOT / "data"
CACHE = DATA / "cache"
STATE = DATA / "state"
for _d in (DATA, CACHE, STATE):
    _d.mkdir(parents=True, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))

INDEX_MAP = {
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX", "VIX": "^INDIAVIX", "MIDCAP": "^NSEMDCP50",
}

DEFAULT_CONFIG = {
    "capital": None,            # total trading capital in ₹ — must be set by the user
    "risk_pct": 1.0,            # base risk per trade, % of capital (regime scales this)
    "max_position_pct": 20.0,   # max capital in one stock
    "max_open_risk_pct": 6.0,   # sum of open stop-risk across all positions
    "max_positions": 6,
    "daily_loss_pct": 2.0,
    "weekly_loss_pct": 5.0,
    "monthly_loss_pct": 10.0,
    "stop_streak": 3,
    "mode": "paper",            # paper | live — start in paper
    "universe": "nifty200",
}


def now_ist() -> datetime:
    return datetime.now(IST)


def load_config() -> dict:
    p = DATA / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    (DATA / "config.json").write_text(json.dumps(cfg, indent=2))


def save_state(name: str, obj) -> Path:
    p = STATE / f"{name}.json"
    p.write_text(json.dumps(obj, indent=2, default=str))
    return p


def load_state(name: str):
    p = STATE / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- symbols & universe
def yf_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if s in INDEX_MAP:
        return INDEX_MAP[s]
    if s.startswith("^") or s.endswith(".NS") or s.endswith(".BO"):
        return s
    return s + ".NS"


def plain(sym: str) -> str:
    return sym.replace(".NS", "").replace(".BO", "")


UNIVERSE_URLS = {
    "nifty50": "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "nifty100": "https://niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "nifty200": "https://niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "nifty500": "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "midcap150": "https://niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "smallcap250": "https://niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
}


def universe(name: str = "nifty200") -> pd.DataFrame:
    """Return DataFrame[Symbol, Company Name, Industry]; cached for 7 days."""
    name = name.lower()
    if name not in UNIVERSE_URLS:
        die(f"unknown universe {name}; choose from {', '.join(UNIVERSE_URLS)}")
    p = CACHE / f"universe_{name}.csv"
    if not p.exists() or time.time() - p.stat().st_mtime > 7 * 86400:
        import urllib.request
        req = urllib.request.Request(UNIVERSE_URLS[name], headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                p.write_bytes(r.read())
        except Exception as e:  # keep stale copy if download fails
            if not p.exists():
                die(f"could not download {name} constituents: {e}")
    df = pd.read_csv(p)
    df = df[df.get("Series", "EQ") == "EQ"] if "Series" in df else df
    return df[["Symbol", "Company Name", "Industry"]].reset_index(drop=True)


# ---------------------------------------------------------------- price data
def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[~df.index.duplicated(keep="last")]
    return df


def history(symbols, period: str = "2y", max_age_hours: float = 6, refresh: bool = False) -> dict:
    """Daily OHLCV (split/dividend adjusted) for one or many NSE symbols. Returns {sym: DataFrame}."""
    import yfinance as yf
    if isinstance(symbols, str):
        symbols = [symbols]
    out, need = {}, []
    for s in symbols:
        p = CACHE / f"{yf_symbol(s)}_{period}.pkl"
        if not refresh and p.exists() and time.time() - p.stat().st_mtime < max_age_hours * 3600:
            out[s] = pd.read_pickle(p)
        else:
            need.append(s)
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        tick = [yf_symbol(s) for s in chunk]
        raw = yf.download(tick, period=period, interval="1d", group_by="ticker", auto_adjust=True,
                          threads=True, progress=False)
        for s, t in zip(chunk, tick):
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = _clean(df)
            except Exception:
                continue
            if len(df) == 0:
                continue
            df.to_pickle(CACHE / f"{t}_{period}.pkl")
            out[s] = df
    return out


def one(symbol: str, period: str = "2y", refresh: bool = False) -> pd.DataFrame:
    d = history(symbol, period=period, refresh=refresh)
    if symbol not in d or len(d[symbol]) < 30:
        die(f"no price data for {symbol} ({yf_symbol(symbol)}). Check the NSE symbol.")
    return d[symbol]


def completed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop today's still-forming bar during market hours (volume comparisons need full sessions)."""
    n = now_ist()
    if len(df) and df.index[-1].date() == n.date() and n.weekday() < 5 \
            and (n.hour * 60 + n.minute) < 15 * 60 + 45:
        return df.iloc[:-1]
    return df


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min",
                                     "Close": "last", "Volume": "sum"}).dropna()


# ---------------------------------------------------------------- indicators
def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def true_range(df):
    pc = df["Close"].shift()
    return pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()],
                     axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df, n=14):
    up = df["High"].diff()
    dn = -df["Low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi


def macd(close, f=12, s=26, sig=9):
    m = ema(close, f) - ema(close, s)
    sg = m.ewm(span=sig, adjust=False).mean()
    return m, sg, m - sg


def zigzag(df: pd.DataFrame, pct: float):
    """Swing points using a % reversal threshold on highs/lows. Returns list of (idx, price, 'H'|'L')."""
    hi, lo = df["High"].values, df["Low"].values
    pts, trend = [], 0
    last_hi_i, last_lo_i = 0, 0
    for i in range(1, len(df)):
        if hi[i] > hi[last_hi_i]:
            last_hi_i = i
        if lo[i] < lo[last_lo_i]:
            last_lo_i = i
        if trend >= 0 and lo[i] <= hi[last_hi_i] * (1 - pct):
            if trend == 1 or not pts:
                pts.append((last_hi_i, hi[last_hi_i], "H"))
            trend, last_lo_i = -1, i
        elif trend <= 0 and hi[i] >= lo[last_lo_i] * (1 + pct):
            if trend == -1 or not pts:
                pts.append((last_lo_i, lo[last_lo_i], "L"))
            trend, last_hi_i = 1, i
    return pts


# ---------------------------------------------------------------- NSE tick size
def tick_size(price: float) -> float:
    """NSE equity tick slabs (effective 15-Apr-2025). NSE reassigns slabs monthly — verify if Kite rejects."""
    if price < 250:
        return 0.01
    if price < 1000:
        return 0.05
    if price < 5000:
        return 0.10
    if price < 10000:
        return 0.50
    if price < 20000:
        return 1.0
    return 5.0


def round_tick(price: float, up: bool = False, down: bool = False) -> float:
    t = tick_size(price)
    q = price / t
    q = math.ceil(q - 1e-9) if up else math.floor(q + 1e-9) if down else round(q)
    return round(q * t, 2)


# ---------------------------------------------------------------- Zerodha charges
# Statutory rates per Shubhamkumartrichy/claude-trading-skills-india (as of Oct 2026, incl. the
# Apr-2026 F&O STT hike) and https://zerodha.com/charges/. Verify there if Zerodha changes pricing.
STT = {"delivery": (0.001, 0.001), "intraday": (0.0, 0.00025),
       "futures": (0.0, 0.0005), "options": (0.0, 0.0015)}
EXCH = {"delivery": 0.0000307, "intraday": 0.0000307, "futures": 0.0000183, "options": 0.0003553}
STAMP = {"delivery": 0.00015, "intraday": 0.00003, "futures": 0.00002, "options": 0.00003}
SEBI = 10 / 1e7
IPFT = 0.01 / 1e7
GST = 0.18
DP_ZERODHA = 13.0   # ₹3.5 CDSL + ₹9.5 Zerodha per scrip per sell day (+GST)


def zerodha_brokerage(segment: str, order_value: float) -> float:
    if segment == "delivery":
        return 0.0
    if segment == "options":
        return 20.0
    return min(0.0003 * order_value, 20.0)


def charges(segment: str, buy: float, sell: float, qty: float) -> dict:
    bv, sv = buy * qty, sell * qty
    turnover = bv + sv
    brk = zerodha_brokerage(segment, bv) + zerodha_brokerage(segment, sv)
    stt = bv * STT[segment][0] + sv * STT[segment][1]
    exch = turnover * EXCH[segment]
    sebi = turnover * SEBI
    ipft = turnover * IPFT
    stamp = bv * STAMP[segment]
    gst = GST * (brk + exch + sebi + ipft)
    dp = DP_ZERODHA * (1 + GST) if segment == "delivery" else 0.0
    total = brk + stt + exch + sebi + ipft + stamp + gst + dp
    gross = sv - bv
    return {"segment": segment, "buy_value": bv, "sell_value": sv, "brokerage": brk, "stt": stt,
            "exchange": exch, "sebi": sebi, "stamp": stamp, "gst": gst, "dp": dp,
            "total": total, "gross_pnl": gross, "net_pnl": gross - total,
            "breakeven_move_pct": 100 * total / bv if bv else 0}


# ---------------------------------------------------------------- formatting
def inr(x: float, dec: int = 0) -> str:
    """Indian digit grouping: 1234567 -> ₹12,34,567."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    neg = x < 0
    x = abs(x)
    s = f"{x:.{dec}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "₹" + whole + (("." + frac) if frac else "")


def pct(x: float, dec: int = 1) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{dec}f}%"


# ---------------------------------------------------------------- calendar + data freshness
# NSE equity trading holidays (weekday closures). Source: India repo references/holidays-2026.md,
# verified against NSE circulars Oct 2026. NSE publishes next year's list in December: add it here
# and bump HOLIDAYS_THROUGH. Exchanges can add holidays at short notice.
NSE_HOLIDAYS = {date.fromisoformat(d) for d in (
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
    "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
    "2026-11-24", "2026-12-25")}
HOLIDAYS_THROUGH = date(2026, 12, 31)
RATES_AS_OF = date(2026, 10, 31)       # STT / exchange / stamp / DP / brokerage constants below


def is_trading_day(d) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def prev_trading_day(d):
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d):
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def freshness_warnings(today=None) -> list[str]:
    today = today or now_ist().date()
    w = []
    if today > HOLIDAYS_THROUGH:
        w.append(f"NSE holiday list ends {HOLIDAYS_THROUGH:%d-%b-%Y} — add the new year's holidays to common.py")
    elif (HOLIDAYS_THROUGH - today).days <= 45:
        w.append(f"NSE holiday list ends {HOLIDAYS_THROUGH:%d-%b-%Y} — update it when NSE publishes next year's list")
    if (today - RATES_AS_OF).days > 120:
        w.append(f"charge rates are from {RATES_AS_OF:%d-%b-%Y} — re-check zerodha.com/charges and update common.py")
    return w


def market_status() -> str:
    n = now_ist()
    if n.weekday() >= 5:
        return "weekend (NSE closed) — prices are last close"
    if n.date() in NSE_HOLIDAYS:
        return "NSE holiday — market closed, prices are last close"
    t = n.hour * 60 + n.minute
    if t < 9 * 60:
        return "before pre-open — prices are previous close"
    if t < 9 * 60 + 15:
        return "pre-open session"
    if t < 15 * 60 + 30:
        return "market OPEN — yfinance prices may be ~15 min delayed"
    return "after close — today's bar is final once Yahoo updates (~16:00 IST)"
