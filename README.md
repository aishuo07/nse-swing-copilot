# NSE Swing Copilot

**A calm, disciplined trading buddy for Indian markets. It does the homework every evening and
pings your phone when it's time to act.**

Most of us don't lose money in the market for lack of tips. We lose it by buying when we're excited,
skipping the stop-loss "just this once", and holding losers while selling winners too early. NSE Swing
Copilot exists to help with exactly that.

It's a [Claude Code](https://claude.com/claude-code) skill for **swing trading NSE stocks with a Zerodha
account**. It doesn't promise to predict the market. It follows **one simple system that held up over
9 years of NSE data**, real Zerodha charges included, and it keeps you honest about risk.

## A normal day with Copilot

- 🌆 **4:30 PM:** it checks whether the market is healthy enough to buy, scans 200 stocks, and sends you
  tomorrow's plan. Most days the answer is "nothing to do", and that's a feature.
- 🌅 **9:16 AM:** if there's a trade, your phone buzzes: **🟢 BUY NOW: 6 × GAIL, max ₹190.90, stop-loss ₹179.78.**
  If the stock opened too high, it tells you to skip. No chasing.
- 📈 **During the day:** it watches your open trades. Stop-loss hit? **🔴 SELL NOW.**
- 🔔 **3:15 PM:** if a stock is closing below its trend line, you get a heads-up to exit before the bell.
- 🪞 **Every weekend:** an honest report card. Win rate, profit per trade, and whether *you* followed the plan.

**You stay in control.** Copilot never places, changes or cancels an order. It tells you what, when,
how many and where the stop goes. You tap the buttons in Kite.

## What makes it different

- **Tested before trusted.** Every setup was backtested on NSE data with real charges, and the ones that
  lost money were dropped, even popular ones. The receipts are below.
- **Protects your capital first.** Position size comes from how much you're willing to lose, not from a hunch.
  A built-in circuit breaker pauses you after a bad day, week or month.
- **Made for India.** STT, stamp duty, DP charges, tick sizes, T+1, NSE holidays, results dates and Kite GTT orders.
- **Talk to it like a friend.** In English or Hinglish: *"aaj kuch kharidna chahiye?"*, *"plan a trade in GAIL"*,
  *"review my trades"*.
- **Alerts where you are:** the free ntfy phone app, WhatsApp, Telegram, or your Mac.
- **Your data stays on your machine.**

> **Please read:** this is an independent, open-source learning project, **not affiliated with or endorsed
> by Zerodha** (Zerodha and Kite are trademarks of Zerodha Broking Ltd., named here only to say which broker
> it works with). It is **not SEBI-registered investment advice**. Backtests don't guarantee future returns,
> and every trade is your own decision. Start with paper trading or money you can afford to lose.

## Under the hood

| Command | What you get |
|---|---|
| `./zt market` | Market health: NIFTY trend, distribution days, follow-through day, breadth, India VIX, and the 50/200-DMA **system filter** |
| `./zt scan` | NIFTY 200/500 setups ranked by what actually worked: **52-week-high momentum** first, then VCP/flat-base breakouts. Includes a `DATA?` flag for suspicious split/bonus data |
| `./zt plan plan SYM` | A go/no-go verdict, position size, exact Zerodha charges, gap risk, a results-date warning, and ready-to-place Kite LIMIT and GTT tickets |
| `./zt plan manage SYM …` | When to move your stop to breakeven, trail it, or exit |
| `./zt journal …` | A paper/live journal with R-multiples, stats by setup, **rule-adherence score**, the cost of mistakes, and a loss circuit breaker |
| `./zt alerts …` | The daily alert routine described above |
| `./zt backtest` | Portfolio backtester with Zerodha charges and slippage. Test any idea yourself |

## The evidence (NIFTY 200, Nov 2017 → Nov 2026, Zerodha charges + 10 bps slippage)

| System (with the NIFTY > 50 & 200 DMA filter) | CAGR | Max DD | Profit factor (1st / 2nd half) |
|---|---|---|---|
| **high52**: close above the prior 52-week high, RS ≥ 70, stop 2.5×ATR, breakeven at +1R, exit on a close below the 50 DMA | **13.0%** | −24% | 1.81 / 1.66 |
| VCP / flat-base breakout | 3.9% | −19% | 1.18 / 1.68 |
| Pullback to 20 EMA | 4.9% | −29% | 1.11 / 1.16 |
| Stockbee 4% momentum burst (5-day hold) | **−3.8%** | −42% | 0.97 / 0.88 |
| NIFTY buy & hold | 9.6% | −38% | — |

**The honest part** (because you deserve the full picture):
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

Your capital, journal, signals, alert logs and the ntfy topic live in `data/` on your machine and are git-ignored, so they're never uploaded.

## Credits

This builds on three open-source projects, and each rule is traced to its source in
[`references/sources.md`](.claude/skills/nse-swing-copilot/references/sources.md):
- [Shubhamkumartrichy/claude-trading-skills-india](https://github.com/Shubhamkumartrichy/claude-trading-skills-india): regime, charges, VCP detector (ported, MIT)
- [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills): exposure coach, Stockbee screens, circuit breaker, performance coach
- [Bhala-Srinivash/nse-trading-skills](https://github.com/Bhala-Srinivash/nse-trading-skills): sizing, stops, trailing, multi-timeframe confluence

## Help make it better 🙏

This is a work in progress, built in the open, and I'd genuinely love your input, whether you've traded
for 10 years or 10 days. Please [open an issue](https://github.com/aishuo07/nse-swing-copilot/issues/new/choose)
or start a [discussion](https://github.com/aishuo07/nse-swing-copilot/discussions). These help the most:
1. **Backtest flaws:** look-ahead, survivorship, cost assumptions, fill assumptions.
2. **Better data:** a free source with delisted NSE stocks, to fix survivorship bias.
3. **Strategy ideas, with evidence.** Anything that beats high52 out of sample after charges.
4. **Indian-market correctness:** charges, holidays, tick sizes, T+1, ASM/GSM, tax.
5. **Live results:** run it on paper for a month and share `./zt journal stats`.

## License

MIT. See [LICENSE](LICENSE).
