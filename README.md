# NSE Swing Copilot: a backtested NSE swing-trading skill for Claude Code

A [Claude Code](https://claude.com/claude-code) skill and set of Python scripts for **cash-equity swing trading
on NSE with a Zerodha account**. It doesn't predict the market. It runs one rules-based system that was
backtested on 9 years of NSE data with real Zerodha charges, and it enforces risk rules so you don't blow up.

> **It never places orders.** It tells you what to buy and sell, when, how many, and where the stop-loss is.
> You place every order yourself in Kite.
> Independent open-source project, **not affiliated with or endorsed by Zerodha**. Zerodha and Kite are
> trademarks of Zerodha Broking Ltd., used here only to say which broker it works with.
> Educational and analytical tool only. **Not SEBI-registered investment advice.** Past backtest results
> don't guarantee future returns.

## What it does

| Command | What you get |
|---|---|
| `./zt market` | NIFTY regime: distribution days, follow-through day, breadth, India VIX, 50/200-DMA **system filter**, exposure ceiling, risk per trade |
| `./zt scan` | NIFTY 200/500 setups ranked by what actually worked: **52-week-high momentum** first, then Minervini VCP/flat-base breakouts. Includes trend template /8, RS 1–99, base grade, multi-timeframe score, and a `DATA?` flag for bad split/bonus data |
| `./zt plan plan SYM` | PASS/WARN/BLOCK gate, position size from rupee risk, exact Zerodha charges, gap risk, results-date warning, ready-to-place Kite LIMIT and GTT tickets |
| `./zt plan manage SYM …` | Stop to move to breakeven, trailing stop, exit signals |
| `./zt journal …` | Paper/live journal, R-multiples, stats by setup, **rule adherence**, cost of mistakes, and a loss circuit breaker (2% day / 5% week / 10% month) |
| `./zt alerts …` | Phone and desktop alerts: 16:30 plan → 09:16 **BUY NOW / SKIP** → market-hours **SELL NOW** when a stop is hit → 15:15 **SELL before close** |
| `./zt backtest` | Portfolio backtester with Zerodha charges and slippage |

## The evidence (NIFTY 200, Nov 2017 → Nov 2026, Zerodha charges + 10 bps slippage)

| System (with the NIFTY > 50 & 200 DMA filter) | CAGR | Max DD | Profit factor (1st / 2nd half) |
|---|---|---|---|
| **high52**: close above the prior 52-week high, RS ≥ 70, stop 2.5×ATR, breakeven at +1R, exit on a close below the 50 DMA | **13.0%** | −24% | 1.81 / 1.66 |
| VCP / flat-base breakout | 3.9% | −19% | 1.18 / 1.68 |
| Pullback to 20 EMA | 4.9% | −29% | 1.11 / 1.16 |
| Stockbee 4% momentum burst (5-day hold) | **−3.8%** | −42% | 0.97 / 0.88 |
| NIFTY buy & hold | 9.6% | −38% | — |

The fine print:
- **The win rate is about 30%.** Profits come from a few +4R to +10R trends, so you sit through many small losses.
- **Out of sample** (Midcap 150, never used while designing the system), high52 returned 8.2% CAGR with PF 1.45. The edge holds, but it's smaller.
- **The last 3 years were weak** (2024–26: 1.2% CAGR with the filter vs 9.2% without).
- **Small accounts do worse.** On ₹10k the same rules return ~6.9% CAGR, because the fixed ₹15.93 DP charge and whole-share rounding take a big share of each trade. Results converge at ≥ ₹50k.
- **Survivorship bias:** the tests use current index members only, so real results will be worse.
- **Tested and rejected** (details in `references/sources.md`): short-hold momentum bursts, letting RS ≥ 90/95 leaders trade while the filter is OFF, and 3-position concentration on small capital.

## Install

You need macOS or Linux, Python 3.11+, and Claude Code (optional, but it's the intended interface).

```bash
git clone https://github.com/aishuo07/nse-swing-copilot && cd nse-swing-copilot
./setup.sh
./zt journal config --capital 100000 --mode paper
./zt market
./zt scan --top 15
```

Open the folder in Claude Code and ask "evening routine", "can I buy today?", "plan a trade in GAIL" or
"review my trades". The skill in `.claude/skills/nse-swing-copilot/SKILL.md` drives the workflow.

### Alerts

```bash
./zt alerts phone      # push to the free ntfy app (iOS/Android); prints your private topic
./zt alerts install    # macOS launchd: weekdays 16:30 / 09:14 / 09:16 IST
./zt alerts test
```

WhatsApp (via CallMeBot) and Telegram are also supported: run `./zt alerts whatsapp` or `./zt alerts telegram`.
Prices come from Yahoo 1-minute bars, which can lag, so **always place a GTT stop-loss in Kite.** The alerts are a backup.

After you act on an alert, log the fill:

```bash
./zt alerts took GAIL --price 188.5
./zt alerts sold GAIL --price 199
```

### Zerodha Kite (optional, read-only)

```bash
claude mcp add --transport http kite https://mcp.kite.trade/mcp
```

With this, Claude can reconcile the journal with your real holdings and GTTs. The skill forbids calling order tools.

## Your data stays local

Capital, journal, signals, alert logs and the ntfy topic are stored in `data/`, which is git-ignored.

## Credits

This builds on three open-source projects, and each rule is traced to its source in
[`references/sources.md`](.claude/skills/nse-swing-copilot/references/sources.md):
- [Shubhamkumartrichy/claude-trading-skills-india](https://github.com/Shubhamkumartrichy/claude-trading-skills-india): regime, charges, VCP detector (ported, MIT)
- [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills): exposure coach, Stockbee screens, circuit breaker, performance coach
- [Bhala-Srinivash/nse-trading-skills](https://github.com/Bhala-Srinivash/nse-trading-skills): sizing, stops, trailing, multi-timeframe confluence

## Feedback wanted

Please [open an issue](https://github.com/aishuo07/nse-swing-copilot/issues/new/choose). The most useful feedback:
1. **Backtest flaws:** look-ahead, survivorship, cost assumptions, fill assumptions.
2. **Better data:** a free source with delisted NSE stocks, to fix survivorship bias.
3. **Strategy ideas, with evidence.** Anything that beats high52 out of sample after charges.
4. **Indian-market correctness:** charges, holidays, tick sizes, T+1, ASM/GSM, tax.
5. **Live results:** run it on paper for a month and share `./zt journal stats`.

## License

MIT. See [LICENSE](LICENSE).
