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
