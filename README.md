# NSE ETF Swing-Trading Engine

A weekly **advisory** engine for a hand-picked set of liquid NSE ETFs. It studies
the market every weekend and messages you which ETFs look strongest to hold — but
it **never places a trade itself**. You execute every order manually on Zerodha
Kite. The engine advises; you decide and act.

> **Not investment advice.** This is a personal, educational tool. Past
> performance does not indicate future results. You are responsible for every
> order you place.

---

## 1. What this is, in one sentence

A smart **swing-trading** engine that ranks a basket of Indian ETFs by recent
strength every week and alerts you which two to hold — while you stay in full
control of every actual trade.

"Swing" means holding positions for **weeks to months** (not intraday, not
buy-and-forget). That is why the engine runs **once a week**, not every minute:
it rides multi-week trends, keeps trading costs low, and suits a human who checks
alerts on weekends rather than watching screens all day.

**The golden rule: the engine holds no broker credentials and cannot move your
money.** It computes signals and sends messages. Every buy, sell, and stop-loss
is placed by you on Kite (manually or via GTT).

---

## 2. The strategy: dual-momentum sector rotation

The engine's logic is **cross-sectional momentum** (also called relative-strength
rotation). Different sectors — banking, IT, gold, silver, midcaps — take turns
leading the market. The engine's job is to ride whichever is currently leading and
rotate into a new leader when leadership changes.

### How it decides, every week — four steps

1. **Score** — For each ETF, compute its **momentum**: the percentage price change
   over the last **26 weeks** (~6 months).
   `momentum = (price_today - price_26_weeks_ago) / price_26_weeks_ago`
   A 26-week window (not 1 week) is deliberate: one week is mostly random noise;
   six months reveals a real trend.

2. **Rank** — Sort all ETFs from highest momentum to lowest.

3. **Cash gate** — Before holding an ETF, its momentum must clear a **cash hurdle**
   (default 0% - it must actually be rising). An ETF that ranks high but is still
   *falling* is rejected, and that slot becomes **cash**. This is the capital-
   preservation circuit breaker: in a market-wide fall where nothing is rising,
   the engine holds cash instead of "the prettiest loser." The gate is per-slot,
   so you can end up half-invested, half-cash.

4. **Hold the top 2 that pass** - Buy/hold the two highest-ranked ETFs that clear
   the gate. Fewer than two passing -> the rest is cash.

### When it actually trades - the "lazy" rotation rule

The engine rotates **only when the *membership* of the top 2 changes** - not when
their order merely reshuffles.

- Held ETFs swap ranks (e.g. #1 <-> #2) but are still the same two -> **do nothing.**
- A genuinely new ETF enters the top 2 and one drops out -> **sell the one that
  left, buy the one that entered.**

This avoids churning (and paying brokerage) on harmless week-to-week reshuffling,
and lets a good position be held for months.

### Where sentiment fits

An AI sentiment reading (Gemini) produces a coarse mood: risk-off / neutral /
risk-on. It touches **only the cash hurdle** - risk-off raises the bar to enter
(+3%), risk-on lowers it slightly. Sentiment never picks the ETFs or changes the
ranking, so a wrong sentiment read can only nudge the entry bar, never hijack the
strategy.

### Why momentum and not "buy the dip"

We deliberately chose momentum ("hold what is already rising") over mean reversion
("buy the beaten-down, hoping it turns"). Predicting turns is extremely hard - a
falling ETF looks identical whether it is about to bounce or about to fall further
("catching a falling knife"). Broad sector baskets also *trend* more reliably than
they revert. And decisively: the original version of this project used a
mean-reversion strategy (Bollinger Bands) and, when backtested on these exact
ETFs, it badly underperformed.

---

## 3. The architecture: six parts

The code is organised so each concern is isolated. The pure decision-making
("domain") never does input/output; all I/O lives in "adapters." This makes the
logic easy to test and lets us swap a data source or notifier without touching the
strategy.

| Part | Job | Lives in |
|------|-----|----------|
| **Eyes** | Fetch weekly price data | `adapters/market_data_yf.py` |
| **Brain** | Score, rank, gate, rotate | `domain/rotation.py`, `domain/config.py` |
| **Memory** | Remember what you hold | `state/state.json`, `adapters/state_git.py` |
| **Mouth** | Send alerts | `adapters/notifier_*.py` |
| **Clock** | Run on schedule | `.github/workflows/swing-engine.yml` |
| **Conductor** | Wire it together, run the cycle | `app/orchestrator.py`, `app/main.py` |

### The Eyes - market data

Fetches weekly **candles** (Open, High, Low, Close, Volume) from Yahoo Finance via
the `yfinance` library. Three safety behaviours:

- **Only completed weeks** - the current, still-forming week is dropped, so a
  half-finished week can never produce a false signal.
- **Survives bad data** - if one ticker fails to fetch (typo, delisting, vendor
  hiccup), it is skipped and reported; the run continues with the healthy tickers.
  Only if *every* ticker fails does the run fail loudly.
- **Isolated** - swapping to another data source means changing only this file.

### The Brain - strategy

Pure functions in `domain/rotation.py` implement the four steps above. It imports
no network or file libraries, so every rule is unit-tested with hand-built data.
The same functions are used by both the live run and the backtest, so a passing
backtest reflects the code that actually trades.

### The Memory - state

The engine wakes fresh each week and would otherwise forget everything. `state.json`
is its notebook: your capital and current positions. This context lets it message
you only the **difference** each week instead of repeating stale advice. State is
committed to Git, giving a permanent, dated history of every change.

**Because the engine cannot see your Zerodha account, you keep the memory honest.**
After you trade, you sync your real holdings back in (see section 5).

### The Mouth - notifications

Two **independent** channels so a single outage never leaves you uninformed:

- **Discord** (primary) - instant mobile alerts.
- **Email/SMTP** (backup) - a completely separate delivery path.

A composite dispatcher sends to both; the run is considered failed **only if every
channel fails**. Messages contain each advisory (ticker, buy/exit, quantity, entry
trigger, computed stop, plain-English reason) plus a warning section listing any
tickers that failed to fetch.

### The Clock - scheduling

**GitHub Actions** runs the engine automatically - no server or always-on PC
needed. Two scheduled runs per week:

- **Saturday** - the full run (fetch, score, rank, gate, rotate, alert).
- **Monday pre-open** - a quick delta check for weekend gaps or sentiment flips
  that should cancel a pending entry before markets open.

Schedules are written in **UTC**, converted from IST. The Monday run is
time-sensitive (it must fire before the 9:15 AM IST open), so timezone accuracy
matters most there.

### The Conductor - orchestrator

`orchestrator.py` wires the parts together and runs the weekly cycle. It handles
locking (so two runs can't collide), aborts without saving on a genuine failure
(so a silent run is never mistaken for "no trades"), and sends a green heartbeat on
success so a missing run is noticeable.

---

## 4. The weekly cycle (the full loop)

```
Clock wakes the engine (Saturday)
      |
      v
Eyes fetch completed weekly candles for the universe
      |
      v
Brain scores by 26-week momentum -> ranks -> cash gate -> top 2
      |
      v
Brain compares target vs current holdings -> emits only the changes
      |
      v
Memory (state.json) records the decisions, committed to Git
      |
      v
Mouth alerts your phone (Discord + email backup)
      |
      v
YOU place the trades on Kite (manually / GTT)
      |
      v
YOU sync your holdings back into memory
      |
      v
Next week: the loop repeats, now aware of what you hold
```

A built-in safety rule: if a **held** ticker's data fails to fetch, the engine
does **not** advise selling it (a data outage must never masquerade as a sell
signal) - it flags the failure for you to check manually.

---

## 5. Operating the engine

### Set your capital (once)

Position sizes are computed from your real capital. Set it (default is a
Rs 1,00,000 placeholder):

```
python -m swing_engine.app.positions capital 250000
```

### Sync your holdings (after every trade)

Export your **holdings** CSV from Zerodha, then:

```
python -m swing_engine.app.positions sync holdings.csv
python -m swing_engine.app.positions list          # verify what the engine now remembers
```

The sync reads your holdings, matches **exactly** against your universe (a ticker
like `GOLDBEES.NS` matches the holding `GOLDBEES`; different fund houses such as
`SETFGOLD` are *different instruments* and are intentionally ignored and listed as
untracked), computes an ATR-based stop from your entry price, and writes it into
`state.json`. The computed stop is the engine's internal reference and may differ
from the exact GTT you placed on Kite.

After syncing, commit and push so the scheduled run sees it:

```
git add state/state.json && git commit -m "positions: sync" && git push
```

### Change which ETFs are watched

Edit `config/universe.json` - a plain list of tickers in Yahoo format
(`SYMBOL.NS`). Add only **liquid, established, non-speculative** ETFs. Every ETF
you add is one the engine can now rotate into when its sector heats up.

**After any universe change, re-run the backtest** (see section 7). A wider
universe changes the results, and a newly-listed ETF with short history can
distort a backtest by truncating the tested window.

---

## 6. Where to change things (quick map)

| I want to change... | Edit this |
|---------------------|-----------|
| Which ETFs are watched | `config/universe.json` |
| Momentum lookback (26 wks), top_n (2), cash hurdle (0%), stop multiple (3x ATR) | `src/swing_engine/domain/config.py` |
| Message wording/format | `src/swing_engine/adapters/notifier_discord.py`, `notifier_email.py` |
| Credentials (Discord webhook, email login, Gemini key) | **GitHub Secrets** (never in code) |
| Run schedule / times | `.github/workflows/swing-engine.yml` (cron lines, in UTC) |
| Current capital / positions | via `positions.py` (see section 5) |

---

## 7. Backtesting

Validate the strategy on historical data before trusting it. Backtests read the
same `config/universe.json` the live engine uses.

```
python -m swing_engine.app.run_rotation_backtest      # rotation vs buy-and-hold
python -m swing_engine.app.run_fullcycle_backtest      # longest available history
```

**How to read results honestly:**
- The key line is **risk-adjusted** - the strategy's Sharpe ratio vs buy-and-hold's
  *own* Sharpe. Beating total return while taking more risk is not true alpha.
- The **Sharpe ratio** = return per unit of risk (bumpiness). Higher is better;
  ~1 is decent, >2 is very good. It is only meaningful *relative* to the benchmark.
- Results are shown **net of trading costs** (0%, 0.2%, 0.5%). An edge that
  survives cost is credible; one that vanishes with cost is a mirage.
- Watch the reported **weeks** count. If a newly-added young ETF shrinks it to a
  small number, the test aligned everything to that short history and is not
  trustworthy - remove the young ticker and re-run.

**Known limitations (on the record):** backtests so far cover mostly a single
bull-market regime; behaviour through a severe crash (e.g. 2020) has not been
cleanly tested because several universe ETFs listed after 2020. Multiple momentum
windows (12/26/52) have not been compared - 26 is a deliberate prior, kept fixed
to avoid overfitting to past data.

---

## 8. Deployment & scheduling

The engine is deployed on GitHub and scheduled with GitHub Actions.

1. Push the repo to a **private** GitHub repository (it stores your trading state).
2. Add repository **Secrets** (Settings -> Secrets and variables -> Actions):
   `GEMINI_API_KEY`, `DISCORD_WEBHOOK`, and for email: `EMAIL_SMTP_HOST`,
   `EMAIL_SMTP_PORT`, `EMAIL_USERNAME`, `EMAIL_PASSWORD`, `EMAIL_TO`.
3. Set Workflow permissions to **Read and write** (so the bot can commit state).
4. The workflow `.github/workflows/swing-engine.yml` runs the two weekly jobs.
   Trigger a manual `saturday` run from the **Actions** tab to test end-to-end.

Notes on the free scheduler: runs can be delayed 5-15 min under load (harmless for
a weekend engine), and Actions pause after 60 days of repo inactivity (weekly runs
keep it alive; the green heartbeat reveals a missed run).

---

## 9. Risk & discipline

- **Advisory only.** You place and cancel every order. The engine never trades.
- **Capital preservation first.** The cash gate moves you to cash when nothing is
  rising. Expect this to sometimes lag a rising market - that is the trade-off.
- **Drawdowns are normal.** Backtests indicate ~20% peak-to-trough is normal for
  this strategy even in good periods. The moment to worry is if live behaviour
  deviates from the backtest's character, not when it matches it.
- **Paper-trade first.** Treat the first several weeks of advisories as paper:
  record them, track what they would have done, place no real orders. This also
  gives the sentiment layer its first honest (non-hindsight) evaluation.
- **Keep the memory honest.** If you skip the holdings sync after trading, the
  engine's view drifts from your real account and its advice degrades.

---

## 10. Project layout

```
src/swing_engine/
  domain/            # pure logic - no network, no files
    rotation.py        the momentum-rotation strategy (score, rank, gate, rotate)
    config.py          ALL tunable parameters (lookback, top_n, hurdle, stops)
    types.py           data objects (Candle, Advisory, Position, PortfolioState)
    ports.py           interfaces the adapters implement
    strategy.py        earlier Donchian strategy (kept for backtest comparison)
  adapters/          # all input/output, isolated
    market_data_yf.py    Yahoo Finance -> candles (the Eyes)
    sentiment_gemini.py  Gemini -> risk-off / neutral / risk-on
    state_git.py         state.json read/write, committed to Git (the Memory)
    notifier_discord.py  Discord alerts (primary Mouth)
    notifier_email.py    SMTP email alerts (backup Mouth)
    notifier_composite.py fan-out to all channels; fail only if all fail
  app/               # wiring, entry points, tools
    orchestrator.py      runs the weekly cycle (the Conductor)
    main.py              entry point the scheduler calls
    universe_loader.py   reads config/universe.json
    positions.py         sync holdings/capital from Zerodha exports
    rotation_backtest.py / run_rotation_backtest.py / run_fullcycle_backtest.py
config/
  universe.json      the ETFs to watch (edit this to add/remove)
state/
  state.json         the engine's memory (capital + current positions)
.github/workflows/
  swing-engine.yml   the schedule (Saturday full run, Monday delta check)
tests/               unit tests for the pure logic and the sync/isolation rules
```

---

## 11. Glossary

- **Candle** - one week of price as five numbers: Open, High, Low, Close, Volume.
- **Momentum** - percentage price change over the lookback window (26 weeks here).
- **Cross-sectional momentum / rotation** - ranking assets against each other and
  holding the strongest, rotating as leadership changes.
- **Cash gate (absolute momentum)** - the rule that holds cash instead of an asset
  that is ranked high but not actually rising.
- **Rotation trigger** - trades only when the *membership* of the top 2 changes.
- **State / stateful** - the engine's saved memory of the current situation.
- **ATR (Average True Range)** - a measure of an asset's typical weekly movement,
  used to place the stop-loss a sensible distance below entry.
- **Sharpe ratio** - return per unit of risk; higher is better, judged against the
  benchmark's own Sharpe.
- **Look-ahead bias** - accidentally using future data in a backtest; avoided by
  only ever deciding on completed candles.
- **Overfitting / data snooping** - tuning a strategy to fit past noise so it looks
  great on history but fails live; avoided by using fixed, pre-chosen parameters.
