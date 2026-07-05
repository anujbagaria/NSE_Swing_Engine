# NSE ETF swing engine — Donchian trend-following (advisory)

A weekly trend-following **advisory** engine for a fixed universe of five liquid
NSE ETFs. It never touches your broker account — it computes signals and tells
*you* what GTT orders to place, cancel, or exit on Zerodha Kite.

> Personal, educational project. **Not investment advice.**
> Placing, modifying, and cancelling every order is a manual human step.
> Past performance does not indicate future results.

## The strategy

- **Entry**: buy when the weekly price breaks the 20-week Donchian high, plus a sentiment-driven ATR confirmation buffer (risk-off demands a wider break).
- **Initial stop**: `entry − 3·ATR` (Chandelier default). Position size is set so the distance from entry to stop equals exactly **1% of capital** at risk.
- **Exit (sole exit — no fixed target)**: an adaptive Chandelier trailing stop, `highest_close − k·ATR`, where `k` adapts:
  - `k = 3.0` while the trend is strong (still making new highs) — let it run
  - `k = 2.5` once deep in profit (open gain ≥ 2R) — standard tighten
  - `k = 2.0` when momentum fades (no new high) — protect open profit
  - the stop only ever **ratchets upward**; a touch triggers a mechanical exit
- **Re-entry**: only on a fresh Donchian breakout — reuses the entry logic, no bespoke re-entry rule (avoids whipsaw-specific parameters).
- **Sentiment** shifts only the entry buffer and never the channel, stop, or sizing — the price structure stays a coherent, auditable statistical object.

All parameters are fixed, defensible priors from the trend-following literature (LeBeau's Chandelier Exit, Donchian channels), **not grid-searched** over ~156 weekly bars — grid-searching that little data overfits. Tune in `src/swing_engine/domain/config.py`.

## Two scheduled runs per week (IST)
a. Saturday full | 08:00 Sat | Recompute channels on completed weekly candles, get a smoothed sentiment regime, emit fresh advisories (entries with sized qty + stop; trailing-stop updates for holdings), commit state. |
b. Monday delta | 07:00 Mon | Re-check the fresh regime and price vs Saturday's advice. Void an entry that turned risk-off or gapped far past the breakout. Channels are NOT recomputed. |

Running on Saturday means only **completed** weekly candles are used, sidestepping partial-candle drift and IST/UTC edge cases.

## Architecture — ports & adapters (unchanged, still excellent)

The domain (pure math, zero I/O) depends only on interfaces (`ports.py`).
Adapters implement them. Swap yfinance or Gemini without touching the strategy.

```
src/swing_engine/
  domain/        # pure, unit-tested, no network
    types.py       Candle, Channel, Advisory, Position, PortfolioState, Regime
    config.py      ALL tunable parameters
    strategy.py    Donchian + ATR + sizing + adaptive Chandelier trail (pure)
    ports.py       the interfaces adapters implement
  adapters/      # all I/O quarantined here
    market_data_yf.py    yfinance -> Candle/PriceSnapshot
    sentiment_gemini.py  Gemini -> Regime (pinned model, temp 0, EWMA)
    state_git.py         atomic JSON state committed to git
    notifier_discord.py  human-readable advisories to Discord
  app/
    orchestrator.py   wires ports, runs the two cycles, handles failure
    main.py           entrypoint the scheduler calls
    backtest.py       walk-forward harness (same pure fns the engine trades)
    run_backtest.py   CLI to backtest on REAL yfinance data
```

## Setup

1. `pip install -e .`
2. Set repo **secrets** (Settings → Secrets → Actions): `GEMINI_API_KEY`, `DISCORD_WEBHOOK`. Never commit these.
3. Enable the GitHub Actions workflow (`.github/workflows/swing-engine.yml`).
4. **Before trusting any signal**, run the real-data backtest: `python -m swing_engine.app.run_backtest`

## Tests

`pytest` — 18 pure-domain tests, no network. The backtest test asserts no look-ahead. The synthetic backtests prove risk mechanics only; **real-data validation via `run_backtest` is mandatory before live use.**

## Risk disclosures

- Advisory only; you execute manually. The engine holds no broker credentials.
- Synthetic backtests use Brownian motion (no momentum) and cannot show a trend edge — they validate risk control, not profitability.
- Kite allows max 50 GTTs/customer; corporate actions (dividends > 5%, splits) auto-cancel GTTs before the ex-date — the Saturday run re-advises.
- Not a registered investment adviser. For educational use.
