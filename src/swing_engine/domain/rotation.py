"""Dual-momentum rotation strategy — pure functions, zero side effects.

This is a CROSS-SECTIONAL strategy: unlike the Donchian engine (which judged each
ETF against its own past), rotation ranks all ETFs against EACH OTHER, so the core
function is portfolio-level, not per-ticker.

The design (agreed with the operator, grounded in Antonacci Dual Momentum and
Jegadeesh & Titman cross-sectional momentum):

  1. Relative momentum : rank every ETF by its lookback-window return.
  2. Absolute momentum : an ETF may be held only if its OWN lookback return clears
     a cash hurdle (risk-free proxy). This is the drawdown circuit-breaker — if
     fewer than `top_n` ETFs pass, the remaining slots go to CASH.
  3. Hold top N (=2) that pass. Rotate ONLY when top-N membership changes
     (operator's low-turnover rule) — an unchanged top-N means no trade, and a
     position may be held for months.
  4. Each held position still carries an ATR hard stop, a faster safety net that
     acts between weekly runs.

Sentiment shifts only the cash hurdle (risk-off raises the bar to enter), never
the ranking itself — same clean separation the rest of the engine uses.

Every rule here is unit-testable with hand-built Candle lists. No logging, no I/O.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import StrategyConfig
from .types import (
    Action,
    Advisory,
    Candle,
    PortfolioState,
    Regime,
    Side,
)


@dataclass(frozen=True)
class Ranking:
    """One ETF's momentum assessment on a given week. Pure value object."""
    ticker: str
    momentum: float          # lookback-window total return (relative signal)
    passes_gate: bool        # absolute momentum: beats the cash hurdle?
    atr: float               # for stop sizing on entry
    last_close: float
    as_of_week: str


def lookback_return(candles: list[Candle], window: int) -> float:
    """Total return over the last `window` completed weeks: close_t / close_(t-w) - 1.
    Uses closes only; no look-ahead (caller passes completed candles)."""
    if len(candles) <= window:
        raise ValueError(f"need > {window} candles, got {len(candles)}")
    start = candles[-(window + 1)].close
    end = candles[-1].close
    if start <= 0:
        return 0.0
    return end / start - 1.0


def _true_ranges(candles: list[Candle]) -> list[float]:
    trs, prev = [], candles[0].close
    for c in candles:
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
        prev = c.close
    return trs


def atr(candles: list[Candle], window: int) -> float:
    return statistics.fmean(_true_ranges(candles[-(window + 1):]))


def cash_hurdle(regime: Regime, config: StrategyConfig) -> float:
    """Absolute-momentum threshold an ETF's lookback return must clear to be held.

    Base hurdle is the risk-free proxy over the window (config.cash_hurdle_return).
    Sentiment raises it when risk-off (demand stronger trends before committing),
    lowers it slightly when risk-on. Sentiment never touches the ranking, only
    this gate — preserving the clean math/sentiment split.
    """
    return config.cash_hurdle_return + config.regime_hurdle_shift[regime]


def rank_universe(
    candles_by_ticker: dict[str, list[Candle]],
    regime: Regime,
    config: StrategyConfig,
) -> list[Ranking]:
    """Rank all ETFs by lookback return (desc) and mark which clear the cash gate.
    Pure: deterministic given inputs. Tickers with insufficient history are skipped."""
    hurdle = cash_hurdle(regime, config)
    rankings: list[Ranking] = []
    for ticker, candles in candles_by_ticker.items():
        if len(candles) <= config.momentum_lookback:
            continue
        mom = lookback_return(candles, config.momentum_lookback)
        rankings.append(Ranking(
            ticker=ticker,
            momentum=mom,
            passes_gate=(mom > hurdle),
            atr=atr(candles, config.atr_window),
            last_close=candles[-1].close,
            as_of_week=candles[-1].week_start.isoformat(),
        ))
    rankings.sort(key=lambda r: r.momentum, reverse=True)
    return rankings


def select_holdings(rankings: list[Ranking], config: StrategyConfig) -> list[str]:
    """The target book: up to top_n tickers that BOTH rank highest AND pass the
    absolute-momentum gate. Fewer than top_n passing => remaining slots are cash
    (they simply don't appear in the returned list)."""
    passing = [r for r in rankings if r.passes_gate]
    return [r.ticker for r in passing[: config.top_n]]


def generate_rotation_advisories(
    candles_by_ticker: dict[str, list[Candle]],
    regime: Regime,
    state: PortfolioState,
    config: StrategyConfig,
) -> list[Advisory]:
    """Portfolio-level weekly logic. Compares the current book to the target book
    and emits only the DELTA (enter new, exit dropped, hold kept). Rotation
    triggers solely from a change in top-N membership — unchanged => empty/hold.
    """
    now = datetime.now(timezone.utc)
    rankings = rank_universe(candles_by_ticker, regime, config)
    rank_by_ticker = {r.ticker: r for r in rankings}
    target = select_holdings(rankings, config)
    current = [p.ticker for p in state.open_positions]

    advisories: list[Advisory] = []

    # --- Exits: held but no longer in the target book ---
    for ticker in current:
        if ticker not in target:
            pos = state.position_for(ticker)
            r = rank_by_ticker.get(ticker)
            advisories.append(Advisory(
                ticker=ticker, side=Side.EXIT, action=Action.EXIT_POSITION,
                trigger_price=0.0, limit_price=0.0, stop_price=0.0, target_price=0.0,
                quantity=(pos.quantity if pos else 0), regime=regime,
                atr=(r.atr if r else 0.0),
                channel_as_of=(rankings[0].as_of_week if rankings else now.date().isoformat()),
                created_at=now,
                rationale=(
                    f"Rotate OUT: {ticker} left the top {config.top_n} "
                    f"(momentum {r.momentum:+.1%} no longer leads)." if r
                    else f"Rotate OUT: {ticker} dropped from the book."
                ),
            ))

    # --- Entries: in the target book but not yet held ---
    per_slot_capital = state.capital / config.top_n
    for ticker in target:
        if ticker not in current:
            r = rank_by_ticker[ticker]
            stop = max(0.0, r.last_close - config.atr_stop_multiple * r.atr)
            per_share_risk = r.last_close - stop
            # Size to the lesser of: 1%-risk sizing, or equal-weight slot capital.
            risk_qty = int((config.risk_per_trade * state.capital) // per_share_risk) if per_share_risk > 0 else 0
            slot_qty = int(per_slot_capital // r.last_close) if r.last_close > 0 else 0
            qty = min(risk_qty, slot_qty) if risk_qty > 0 else slot_qty
            advisories.append(Advisory(
                ticker=ticker, side=Side.BUY, action=Action.PLACE_GTT,
                trigger_price=r.last_close, limit_price=r.last_close,
                stop_price=stop, target_price=0.0, quantity=qty, regime=regime,
                atr=r.atr, channel_as_of=r.as_of_week, created_at=now,
                rationale=(
                    f"Rotate IN: {ticker} entered top {config.top_n} "
                    f"(momentum {r.momentum:+.1%}, passed cash gate). "
                    f"Buy ~{qty} @ {r.last_close:.2f}; place sell-GTT stop {stop:.2f} "
                    f"({config.atr_stop_multiple}xATR)."
                ),
            ))

    # --- Holds: in both books (report for transparency; no action) ---
    for ticker in target:
        if ticker in current:
            r = rank_by_ticker[ticker]
            advisories.append(Advisory(
                ticker=ticker, side=Side.BUY, action=Action.HOLD,
                trigger_price=r.last_close, limit_price=r.last_close,
                stop_price=0.0, target_price=0.0, quantity=0, regime=regime,
                atr=r.atr, channel_as_of=r.as_of_week, created_at=now,
                rationale=f"Hold {ticker}: still top {config.top_n} (momentum {r.momentum:+.1%}).",
            ))

    return advisories
