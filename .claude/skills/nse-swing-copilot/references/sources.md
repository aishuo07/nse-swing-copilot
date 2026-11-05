# Where each rule came from

This skill re-implements logic from three open-source MIT-licensed projects, adapted to NSE +
Zerodha. The VCP/flat-base detector in `scan.py` is ported directly from the India repo's
`breakout-planner/scripts/breakout.py` (MIT, © 2026 Shubham Kumar), with attribution in the code.

## How the source repos were tested (4-Nov-2026)

| Repo | What was run | Result | What changed here |
|---|---|---|---|
| claude-trading-skills-india | Full test suite | 378/378 pass | — |
| | `charges.py` vs ours | identical (₹180.27 on the sample trade) | none needed |
| | `regime.py status` on live NIFTY | Confirmed uptrend 80% (ours: under pressure 40%) | kept our stricter 200-DMA rule: the backtest showed the 50+200 DMA filter improves the best system |
| | `breakout.py check` HAL, MOTHERSON | found HAL VCP 3T A (pivot 4,926.1); MOTHERSON loose D. Our old detector got both wrong | **ported their base detector** |
| | `backtest.py strategies` | single-symbol only; has slippage, 52w-high, ema-pullback | added slippage + those strategies to our portfolio backtester |
| tradermonty/claude-trading-skills | `stockbee-momentum-burst-screener` on NSE data (offline JSON) | worked; 5 A- bursts found | backtested bursts: **lose money on NSE after costs** → demoted to info flags |
| | pytest: position-sizer, trade-performance-coach | 73/73, 79/79 pass | added "half risk for 2 trades after a rule break" |
| | pytest: drawdown-circuit-breaker, breakout-trade-planner, pre-trade-gate | failures only from a missing US market-calendar package | n/a (US calendar) |
| | VCP, CANSLIM, FTD, market-top skills | need a paid FMP API (US data) | logic re-implemented on Yahoo NSE data |
| nse-trading-skills | Prompt-only (no code) | — | rules used for stops, trailing, R:R, MTF score |


| Rule / component | Source |
|---|---|
| Distribution days (−0.2% on higher vol, 25-session window, cleared by +5%), FTD with NIFTY-tuned threshold (1.5σ clamped 1–2%), NIFTYBEES as volume proxy, regime → exposure table, VIX bands | Shubhamkumartrichy/claude-trading-skills-india `market-regime` (itself from O'Neil/IBD) |
| Exposure ceiling + NEW_ENTRY_ALLOWED / REDUCE_ONLY / CASH_PRIORITY | tradermonty/claude-trading-skills `exposure-coach` |
| Breadth (% above 50/200 DMA, new highs/lows), breadth caps | India `market-breadth`, tradermonty `market-breadth-analyzer` / `market-top-detector` |
| Minervini trend template, VCP (each pullback ≤0.85× previous, last ≤15%, first ≤50%), flat base ≤15%, pivot, volume dry-up <0.6, grade A–D, trigger = pivot+0.2%, stop = last low −1% capped at 8% | India `breakout-planner`, tradermonty `vcp-screener` / `breakout-trade-planner` |
| RS rating 1–99 (IBD weighting 40/20/20/20) | India `momentum-scanner`, tradermonty `canslim-screener` |
| 4% momentum burst, range expansion, episodic pivot (gap ≥4% on ≥3× volume) | tradermonty `stockbee-momentum-burst-screener` / `stockbee-episodic-pivot-analyzer`, India `momentum-scanner` |
| 6-point multi-timeframe confluence | Bhala-Srinivash/nse-trading-skills `multi-timeframe-analysis` |
| Fixed-fractional sizing, 20% single-stock cap, 6% total open risk, leverage-adjusted risk | nse-trading-skills `position-sizing` |
| Structure vs ATR stops, buffers 0.5% large-cap / 1% mid-small, ≥1 ATR from entry | nse-trading-skills `stop-loss-strategies` |
| Hybrid trail: hold <1R, breakeven at 1R, ATR trail at 2R, structure trail at 3R+ | nse-trading-skills `trailing-stops` |
| R:R minimums, expectancy | nse-trading-skills `risk-reward-ratio` |
| Zerodha charges (STT 0.1% delivery, exchange 0.00307%, stamp 0.015%, DP ₹13+GST, Apr-2026 F&O STT) | India `trading-costs` (verify at zerodha.com/charges) |
| Circuit breaker 2% day / 5% week / 10% month, 3-loss streak, pre-trade gate | India `trading-discipline`, tradermonty `drawdown-circuit-breaker` / `pre-trade-discipline-gate` |
| Performance by setup, cost of mistakes | tradermonty `trade-performance-coach` / `weekly-performance-digest` |
| Order-safety stance (no autonomous orders, SEBI retail-algo/static-IP rules) | India `broker-integration` |

## Does the NIFTY filter help? (tested 4-Nov-2026, NIFTY 200, ₹1L, high52)

| Market rule | 9 yrs CAGR · PF · DD | last 3 yrs CAGR · PF |
|---|---|---|
| **NIFTY > 50 & 200 DMA (kept)** | **13.0% · 1.71 · −24%** | 1.2% · 1.07 |
| no filter | 9.5% · 1.41 · −27% | 9.2% · 1.39 |
| filter + RS ≥ 90 leaders allowed when OFF (half risk, max 2) | 12.5% · 1.70 · −27% | −3.2% · 0.75 |
| filter + RS ≥ 95 leaders allowed when OFF | 9.8% · 1.53 · −26% | −3.0% · 0.76 |

The "strong stock in a weak market" override lost money in the last 3 years and didn't beat the
filter over 9, so it was rejected (`--market leaders90/95` stays in the backtester for re-tests).
The filter lagged no-filter recently (2024–26 choppy market); re-check every few months.

## Survivorship-free retest (5-Nov-2026)

Prompted by Reddit feedback. The Yahoo tests above only knew today's NIFTY 200, which is a list of survivors.
`bhav.py` rebuilds prices from NSE's own daily bhavcopy archive (Jun 2015 → Nov 2026, 2,833 sessions,
3,291 company shares, including 680 that stopped trading). It adjusts for bonuses, splits and demergers using
the exchange's corporate-action lists plus a price check. The universe is point-in-time: the top 200 by
6-month traded value, refreshed every Feb and Aug, never using future knowledge. Over 9 years it held 478
different stocks; 281 of them are not in today's NIFTY 200 and 42 stopped trading.

`./zt backtest --data bhav --pit --years 9 --capital 100000` (high52, 6 positions, 1% risk):

| Test | NIFTY > 50 & 200 DMA | NIFTY > 50 DMA | no filter |
|---|---|---|---|
| Yahoo, today's NIFTY 200 (old headline) | 13.0% · PF 1.71 · −24% | 11.0% · 1.43 · −27% | 9.5% |
| Bhavcopy, today's NIFTY 200 | 8.0% · 1.48 · −26% | 9.9% · 1.44 · −26% | 8.2% |
| **Bhavcopy, point-in-time top 200** | **4.3% · 1.26 · −29%** | **10.6% · 1.58 · −28%** | 1.0% |
| … 1st half / 2nd half | 4.7% / 4.4% | 10.0% / 11.9% | −3.1% / 6.8% |
| … top 150 / top 300 | 5.3% / 2.0% | 8.7% / 7.0% | |
| … 8 positions | 7.0% | 12.9% | |
| … ₹10k capital | 1.4% | 9.4% | |
| … last 3 years | 1.1% | 8.8% | |
| NIFTY buy & hold (price only) | 9.6% · −38% | | |

Findings:
- **Survivorship bias was large.** With 50 & 200 DMA, the headline fell from 13.0% to 4.3%. Trades in
  stocks still in today's NIFTY 200 averaged +0.18R; trades in stocks that later dropped out averaged −0.02R.
- **The old 13% was also path-dependent.** The same stocks on bhavcopy data gave 8.0%. Tiny price differences
  filled the 6 slots differently in Nov 2020, and the bhav run missed ATGL (+41R) and ADANIENSOL (+26R).
  Two trades were worth about 5% CAGR.
- **The 50-DMA-only filter held up best** in every survivorship-free variant (7–13%), in both halves and on
  ₹10k. But it was found by looking at these results, so treat it as a hypothesis for paper or live
  testing, not a proven edge. Even at its best it is roughly level with NIFTY buy & hold (≈ 11% with
  dividends) with a somewhat smaller drawdown.
- Other setups on survivorship-free data, with 50 & 200 DMA: breakout 4.1%, pullback −1.1%, burst −11.7%.
- Delisting haircut (0–100%) changed nothing: the 50 DMA exit was always out before a stock stopped trading.
- The data panel was checked against Yahoo for today's NIFTY 200. The remaining gaps are Yahoo's: unadjusted
  demergers, year-boundary glitches (TRENT, CONCOR, MOTILALOFS) and dividend adjustment.
- Universe caveat: NIFTY 200 is chosen by free-float market cap, which bhavcopy lacks. Traded value is a
  proxy that tilts toward speculative names (YESBANK, IDEA, SUZLON). Rebuilding real index membership from NSE
  index-change notices would be the next refinement.

**Applied 5-Nov-2026 (user's choice):** the live system now uses high52 only, the NIFTY > 50 DMA filter,
the configured 1% risk and up to `max_positions` slots, matching the backtest. The regime overlay only
vetoes entries in a confirmed correction. These rules are frozen for the live test: no changes before
6 months or 30 trades, whichever comes later.

### Risk per trade (survivorship-free, high52 + NIFTY > 50 DMA, 9 yrs)

| Risk / per-stock cap | ₹1L CAGR · max DD | ₹10k CAGR · max DD |
|---|---|---|
| 1% / 20% | 10.6% · −28% | 9.4% · −30% |
| **2% / 33% (live from 5-Nov-2026, user's choice)** | **16.3% · −39%** | **14.9% · −37%** |
| 3% / 40% | 14.5% · −44% | |
| 4% / 50% | 11.7% · −47% | |

Returns peak at about 2%; beyond that, losing streaks (30% win rate) and fewer, larger positions cut
returns. With 2% risk the loss limits were doubled to keep their meaning (day 4%, week 10%, month 20%) and
portfolio heat raised to 12% (6 × 2%). The 3-loss-streak half-size rule is not in the backtest, so live
results may be somewhat lower.

## Intraday strategies tested (5-Nov-2026) → none adopted

Scripts: `research/intraday/`. Zerodha MIS charges plus 5 bps slippage per side.

**5-min bars, last 59 sessions, NIFTY 200** (₹200 risk per trade, net R per trade):
- ORB15, all stocks: −0.13R. ORB15 on stocks in play: −0.01R.
- Zarattini/Aziz ORB5 on stocks in play: −1.0 to −1.2R. Its ~0.2% stop can't absorb Indian costs and slippage.
- VWAP pullback −0.53R · VWAP 2σ reversion −0.90R · EMA 9/21 cross −0.51R.
- Previous-day high/low break −0.25R · narrow-CPR breakout −0.24R.

**Daily bars, 9 years, survivorship-free bhavcopy, point-in-time top 200** (open → close):
- Gap-and-go −0.79% per trade. NR7 breakout −0.17%. Previous-day breakout −0.60%. Narrow CPR −0.25%.
- Buying the day after a ≥4% fall: −0.02%.
- **Gap fade (|gap| ≥ 2%, fade at the open, exit at the close): +0.38% per trade, t = 12.9, positive in 8 of 10
  years.** Longs +0.49%, shorts +0.27%. Strongest in less liquid names and on gaps ≥5%.
  - But 2022 was −0.06% and 2026 −0.04%, and the last 60 days on 5-min data were negative at every entry time
    (09:15 −0.31%, 09:20 −0.03%).
  - It needs a fill at the 09:15 auction price. Status: research only, paper-test before any money.

**NIFTY first-hour → last-hour momentum** (Gao et al. 2018), 724 days of hourly data: −0.02% per day net.
No edge on NIFTY recently.

**Forward paper test started 5-Nov-2026** (`scripts/gapfade.py`, rules frozen as above: PIT top-200 by
126-day value, ≥ ₹10 cr/day, |gap| ≥ 2%, top 5 by gap, exit at close; split/bonus/demerger ex-dates skipped).
Logged two ways: 09:15 auction → close (the backtest's assumption) and 09:20 → 15:15 (realistic by hand).
Pass bar, set in advance: 09:20 version ≥ +0.15% net per trade with t ≥ 2 after 60 trading days; else drop.

## Reddit feedback applied (Nov 2026)

| Feedback | Change |
|---|---|
| Rates/holidays go stale | `NSE_HOLIDAYS`, `HOLIDAYS_THROUGH`, `RATES_AS_OF` in `common.py`; `freshness_warnings()` alerts 45 days before the list ends and when rates are >120 days old |
| Close the loop between plan and outcome | `alerts took` stores alert limit/stop/qty vs fill; chasing, oversizing, skipped/expired signals and off-system trades are logged as mistakes; `journal stats` shows adherence and fill vs limit |
| yfinance split/bonus quirks | `DATA?` flag and no setup when a stock moved >30% in one day in the last year |
| SEBI RA rules on relaying entry/SL/targets | signals are for the user's own use; don't publish or share them |

## Regime → exposure table

| Label | Exposure ceiling | New positions | Risk/trade |
|---|---|---|---|
| Confirmed uptrend (score < 25 / ≥ 25) | 100% / 80% | 5 / 4 | 1% |
| Confirmed uptrend, fresh FTD (≤10 sessions) | 50% | 3 | 0.5% |
| Uptrend under pressure (score < 50 / ≥ 50) | 60% / 40% | 2 / 1 | 0.75% / 0.5% |
| Rally attempt (no FTD) | 25% | 1 pilot | 0.25% |
| Correction / failed FTD | 20% (10% if ≥10% deep) | 0 | 0 |

Overrides: India VIX ≥ 22 → ceiling ≤ 50%. Under 40% of NIFTY 200 above their 200 DMA → ≤ 40%.

## Not included (deliberately)

- **F&O / intraday:** SEBI data shows most retail participants lose money. The Apr-2026 STT hike makes it
  costlier still. The India repo's `fno-options-analysis` and `options-strategies` are the place to start
  if the user insists, after the cash system is profitable.
- **Automated order placement:** SEBI's retail-algo framework (from 1 Apr 2026) requires broker-registered
  apps and a static IP, and autonomous orders remove the human check this skill is built around.
