"""The strategy engine: pure functions, deterministic, zero side effects.

REVISION: Donchian trend-following core with a full risk framework.

Given inputs (candles, price, regime, state) it returns advisories. It never
logs, never calls Discord, never touches Git — every rule here is unit-testable
with hand-built Candle fixtures. Sentiment shifts only the breakout confirmation
buffer; it never redefines the channel, the stop, or the sizing.

Risk model (the core addition over the original Bollinger engine):
  - quantity is sized so that (entry - stop) * qty == risk_per_trade * capital
  - every entry carries a hard ATR stop, so a position is never naked
  - exits are modelled as Kite OCO GTTs (stop + target in one order)
  - a trailing stop ratchets upward as the trend runs, never downward
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from .config import StrategyConfig
from .types import (
    Action,
    Advisory,
    Candle,
    Channel,
    PortfolioState,
    Position,
    PriceSnapshot,
    Regime,
    Side,
)


def _true_ranges(candles: list[Candle]) -> list[float]:
    """Wilder true range per bar: max(H-L, |H-prevC|, |L-prevC|)."""
    trs: list[float] = []
    prev_close = candles[0].close
    for c in candles:
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
        prev_close = c.close
    return trs


def compute_channel(candles: list[Candle], config: StrategyConfig) -> Channel:
    """Donchian channel + ATR on completed weekly candles.

    upper = highest HIGH over the entry lookback (breakout-to-buy level)
    lower = lowest LOW over the exit lookback (structural exit level)
    atr    = mean true range over atr_window
    Sentiment does NOT enter here — the channel is pure price structure.
    """
    if len(candles) < config.min_candles_required:
        raise ValueError(
            f"Need >= {config.min_candles_required} candles, got {len(candles)}"
        )
    entry_win = candles[-config.entry_lookback:]
    exit_win = candles[-config.exit_lookback:]
    atr_win = candles[-(config.atr_window + 1):]  # +1 so TR has a prev close

    upper = max(c.high for c in entry_win)
    lower = min(c.low for c in exit_win)
    atr = statistics.fmean(_true_ranges(atr_win))

    return Channel(
        upper=upper,
        lower=lower,
        atr=atr,
        entry_lookback=config.entry_lookback,
        exit_lookback=config.exit_lookback,
        as_of_week=candles[-1].week_start,
    )


def entry_trigger_price(channel: Channel, regime: Regime, config: StrategyConfig) -> float:
    """Breakout price to BUY, after the sentiment confirmation buffer.
    Risk-off requires the price to clear the channel high by a wider ATR margin."""
    buf = config.buffer_for(regime)
    return channel.upper + buf * channel.atr


def hard_stop_price(entry: float, atr: float, config: StrategyConfig) -> float:
    """Protective stop = entry - atr_stop_multiple * ATR. Never below zero."""
    return max(0.0, entry - config.atr_stop_multiple * atr)


def trend_is_strong(candles: list[Candle], config: StrategyConfig) -> bool:
    """Structural momentum test: has price made a NEW entry-lookback high in the
    most recent bar? This is self-scaling (no hard-coded % or candle count) and
    directly encodes 'the breakout is still extending'."""
    if len(candles) < config.entry_lookback:
        return False
    window = candles[-config.entry_lookback:]
    latest_high = window[-1].high
    prior_high = max(c.high for c in window[:-1]) if len(window) > 1 else window[-1].high
    return latest_high >= prior_high


def choose_trail_multiple(
    candles: list[Candle], entry: float, highest_close: float, atr: float,
    config: StrategyConfig,
) -> float:
    """Three-tier adaptive Chandelier multiple (all literature-grounded):
      - momentum fading (no new high)          -> tight  (2.0*ATR)
      - deep in profit (open gain >= 2R)        -> medium (2.5*ATR)
      - otherwise (trend intact, early)         -> wide   (3.0*ATR)
    Fade takes priority: protecting open profit dominates once momentum dies.
    """
    if not trend_is_strong(candles, config):
        return config.atr_trail_fade
    r = config.atr_stop_multiple * atr  # initial risk per share ~ 3*ATR
    open_gain = highest_close - entry
    if r > 0 and open_gain >= config.deep_profit_r * r:
        return config.atr_trail_deep_profit
    return config.atr_trail_wide


def position_size(capital: float, entry: float, stop: float, config: StrategyConfig) -> int:
    """Fixed-fractional sizing: qty so that risk == risk_per_trade * capital.

    risk_amount = capital * risk_per_trade
    per_share_risk = entry - stop
    qty = floor(risk_amount / per_share_risk)
    Returns 0 if the stop is non-positive or per-share risk is zero.
    """
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return 0
    risk_amount = capital * config.risk_per_trade
    return int(risk_amount // per_share_risk)


def trail_stop_price(highest_close: float, atr: float, multiple: float) -> float:
    """Chandelier trailing stop = highest_close - multiple * ATR."""
    return max(0.0, highest_close - multiple * atr)


def generate_saturday_advisory(
    ticker: str,
    candles: list[Candle],
    current_price: float,
    regime: Regime,
    state: PortfolioState,
    config: StrategyConfig,
) -> Advisory:
    """Full-cycle logic for one ticker on the Saturday run. Pure.

    Three cases:
      1. Holding -> ratchet the trailing stop upward if the trend advanced;
         otherwise HOLD. Never loosens a stop.
      2. Flat, breakout setup exists -> BUY advisory with sized qty + OCO exit.
      3. Flat, no setup -> HOLD.
    """
    channel = compute_channel(candles, config)
    now = datetime.now(timezone.utc)
    pos = state.position_for(ticker)

    # --- Case 1: already holding -> manage the exit (adaptive Chandelier trail) ---
    if pos is not None:
        new_high = max(pos.highest_close, current_price)
        mult = choose_trail_multiple(candles, pos.entry_price, new_high, channel.atr, config)
        trail = trail_stop_price(new_high, channel.atr, mult)
        # Only ever raise the stop, never lower it.
        proposed_stop = max(pos.stop_price, trail)
        if proposed_stop > pos.stop_price + 1e-9:
            return Advisory(
                ticker=ticker, side=Side.EXIT, action=Action.UPDATE_TRAIL,
                trigger_price=proposed_stop, limit_price=proposed_stop,
                stop_price=proposed_stop, target_price=0.0, quantity=pos.quantity,
                regime=regime, atr=channel.atr, channel_as_of=channel.as_of_week,
                created_at=now,
                rationale=(
                    f"Raise trailing stop to {proposed_stop:.2f} "
                    f"(Chandelier: peak {new_high:.2f} - {mult}*ATR)."
                ),
            )
        return Advisory(
            ticker=ticker, side=Side.EXIT, action=Action.HOLD,
            trigger_price=pos.stop_price, limit_price=pos.stop_price,
            stop_price=pos.stop_price, target_price=0.0, quantity=pos.quantity,
            regime=regime, atr=channel.atr, channel_as_of=channel.as_of_week,
            created_at=now,
            rationale=f"Holding; stop unchanged at {pos.stop_price:.2f}.",
        )

    # --- Cases 2/3: flat. Respect the concurrent-position cap. ---
    if len(state.open_positions) >= config.max_open_positions:
        return Advisory(
            ticker=ticker, side=Side.BUY, action=Action.HOLD,
            trigger_price=0.0, limit_price=0.0, stop_price=0.0, target_price=0.0,
            quantity=0, regime=regime, atr=channel.atr,
            channel_as_of=channel.as_of_week, created_at=now,
            rationale="Flat but at max open positions; no new entry.",
        )

    entry = entry_trigger_price(channel, regime, config)
    stop = hard_stop_price(entry, channel.atr, config)
    qty = position_size(state.capital, entry, stop, config)

    if qty <= 0:
        return Advisory(
            ticker=ticker, side=Side.BUY, action=Action.HOLD,
            trigger_price=entry, limit_price=entry, stop_price=stop,
            target_price=0.0, quantity=0, regime=regime, atr=channel.atr,
            channel_as_of=channel.as_of_week, created_at=now,
            rationale="Setup exists but sizing rounds to 0 shares; skip.",
        )

    return Advisory(
        ticker=ticker, side=Side.BUY, action=Action.PLACE_GTT,
        trigger_price=entry, limit_price=entry, stop_price=stop,
        target_price=0.0, quantity=qty, regime=regime, atr=channel.atr,
        channel_as_of=channel.as_of_week, created_at=now,
        rationale=(
            f"Buy breakout GTT @ {entry:.2f} (20wk high + {config.buffer_for(regime)}*ATR, "
            f"regime={regime.value}). Qty {qty} sized to 1% risk; on fill place a "
            f"single sell-GTT stop at {stop:.2f} (no fixed target - trailing stop is the exit)."
        ),
    )


def reconcile_monday(
    snapshot: PriceSnapshot,
    saturday_advisory: Advisory,
    fresh_regime: Regime,
    config: StrategyConfig,
) -> Advisory:
    """Monday 7 AM delta check for one ticker. Recomputes no channel — Saturday's
    levels stand. Voids a pending BUY if the regime turned risk-off over the
    weekend, or the price already gapped far past the breakout (chasing risk)."""
    now = datetime.now(timezone.utc)
    is_buy = saturday_advisory.side == Side.BUY and saturday_advisory.action == Action.PLACE_GTT

    regime_turned_adverse = is_buy and fresh_regime == Regime.RISK_OFF
    tol = config.monday_price_tolerance
    # For a breakout the risk is a gap UP far beyond the trigger (buying extended).
    price_extended = (
        is_buy and snapshot.price > saturday_advisory.trigger_price * (1 + max(tol, 0.03))
    )

    if regime_turned_adverse or price_extended:
        why = "regime turned risk-off" if regime_turned_adverse else "price gapped well past breakout"
        return Advisory(
            ticker=saturday_advisory.ticker, side=saturday_advisory.side,
            action=Action.CANCEL_GTT, trigger_price=saturday_advisory.trigger_price,
            limit_price=saturday_advisory.limit_price, stop_price=saturday_advisory.stop_price,
            target_price=saturday_advisory.target_price, quantity=saturday_advisory.quantity,
            regime=fresh_regime, atr=saturday_advisory.atr,
            channel_as_of=saturday_advisory.channel_as_of, created_at=now,
            rationale=(
                f"VOID Saturday advisory ({why}). Cancel the entry GTT if placed; "
                f"do not chase."
            ),
        )

    return Advisory(
        ticker=saturday_advisory.ticker, side=saturday_advisory.side,
        action=Action.HOLD, trigger_price=saturday_advisory.trigger_price,
        limit_price=saturday_advisory.limit_price, stop_price=saturday_advisory.stop_price,
        target_price=saturday_advisory.target_price, quantity=saturday_advisory.quantity,
        regime=fresh_regime, atr=saturday_advisory.atr,
        channel_as_of=saturday_advisory.channel_as_of, created_at=now,
        rationale="Saturday advisory still valid; no change.",
    )
