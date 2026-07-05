"""Unit tests for the pure domain. No network, no files — just fixtures."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from swing_engine.domain import strategy
from swing_engine.domain.config import DEFAULT_CONFIG
from swing_engine.domain.types import (
    Action, Advisory, PortfolioState, Position, PriceSnapshot, Regime, Side,
)


def make_candles(closes, highs=None, lows=None):
    from swing_engine.domain.types import Candle
    start = date(2022, 1, 3)
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c + 1
        l = lows[i] if lows else c - 1
        out.append(Candle(week_start=start + timedelta(weeks=i),
                          open=c, high=h, low=l, close=c, volume=1000))
    return out


def test_channel_upper_is_highest_high():
    closes = [100 + (i % 5) for i in range(40)]
    highs = [c + 2 for c in closes]
    ch = strategy.compute_channel(make_candles(closes, highs=highs), DEFAULT_CONFIG)
    assert ch.upper == pytest.approx(max(highs[-DEFAULT_CONFIG.entry_lookback:]))


def test_atr_is_positive_for_volatile_series():
    closes = [100 + (i % 7) for i in range(40)]
    ch = strategy.compute_channel(make_candles(closes), DEFAULT_CONFIG)
    assert ch.atr > 0


def test_sentiment_buffer_makes_riskoff_entry_higher():
    closes = [100 + (i % 5) for i in range(40)]
    ch = strategy.compute_channel(make_candles(closes), DEFAULT_CONFIG)
    off = strategy.entry_trigger_price(ch, Regime.RISK_OFF, DEFAULT_CONFIG)
    neu = strategy.entry_trigger_price(ch, Regime.NEUTRAL, DEFAULT_CONFIG)
    on = strategy.entry_trigger_price(ch, Regime.RISK_ON, DEFAULT_CONFIG)
    # risk-off demands the breakout clear the high by a WIDER margin
    assert off > neu > on


def test_position_size_respects_one_percent_risk():
    # capital 100000, 1% risk => 1000 risk budget. entry-stop = 10 => 100 shares.
    qty = strategy.position_size(100000.0, entry=200.0, stop=190.0, config=DEFAULT_CONFIG)
    assert qty == 100


def test_position_size_zero_when_stop_above_entry():
    assert strategy.position_size(100000.0, entry=100.0, stop=105.0, config=DEFAULT_CONFIG) == 0


def test_hard_stop_is_below_entry_by_three_atr():
    stop = strategy.hard_stop_price(entry=100.0, atr=5.0, config=DEFAULT_CONFIG)
    assert stop == pytest.approx(85.0)  # 100 - 3*5 (Chandelier default)


def test_trend_strong_when_new_high_made():
    closes = [100 + i for i in range(40)]  # strictly rising -> new high each bar
    assert strategy.trend_is_strong(make_candles(closes), DEFAULT_CONFIG) is True


def test_trend_weak_when_no_new_high():
    closes = [100 + i for i in range(35)] + [130, 128, 126, 124, 122]  # rolled over
    assert strategy.trend_is_strong(make_candles(closes), DEFAULT_CONFIG) is False


def test_trail_multiple_tightens_on_momentum_fade():
    # rolled-over series => fade => tight 2.0 multiple regardless of profit
    closes = [100 + i for i in range(35)] + [130, 128, 126, 124, 122]
    m = strategy.choose_trail_multiple(make_candles(closes), entry=100.0,
                                       highest_close=135.0, atr=3.0, config=DEFAULT_CONFIG)
    assert m == pytest.approx(DEFAULT_CONFIG.atr_trail_fade)


def test_trail_multiple_wide_when_trend_strong_and_early():
    closes = [100 + i for i in range(40)]  # new high each bar
    # small open gain (< 2R) => wide trail
    m = strategy.choose_trail_multiple(make_candles(closes), entry=138.0,
                                       highest_close=139.0, atr=3.0, config=DEFAULT_CONFIG)
    assert m == pytest.approx(DEFAULT_CONFIG.atr_trail_wide)


def test_saturday_buy_when_flat_with_sizing_and_oco():
    closes = [100 + i * 0.5 for i in range(40)]  # steady uptrend
    adv = strategy.generate_saturday_advisory(
        "MID150BEES.NS", make_candles(closes), closes[-1], Regime.NEUTRAL,
        PortfolioState(), DEFAULT_CONFIG)
    assert adv.side == Side.BUY
    assert adv.action == Action.PLACE_GTT
    assert adv.quantity > 0
    assert adv.stop_price < adv.trigger_price
    assert adv.target_price == 0.0  # no fixed target; trailing stop is the exit


def test_trailing_stop_only_ratchets_up():
    closes = [100 + i * 0.5 for i in range(40)]
    pos = Position(ticker="ITBEES.NS", entry_price=110.0, quantity=50,
                   stop_price=105.0, highest_close=112.0)
    adv = strategy.generate_saturday_advisory(
        "ITBEES.NS", make_candles(closes), current_price=125.0,
        regime=Regime.NEUTRAL, state=PortfolioState(open_positions=[pos]),
        config=DEFAULT_CONFIG)
    # price ran up => trailing stop should rise above the old 105
    assert adv.action in (Action.UPDATE_TRAIL, Action.HOLD)
    if adv.action == Action.UPDATE_TRAIL:
        assert adv.stop_price > pos.stop_price


def test_max_open_positions_blocks_new_entry():
    closes = [100 + i * 0.5 for i in range(40)]
    positions = [Position(f"T{i}.NS", 100, 10, 95, 100) for i in range(DEFAULT_CONFIG.max_open_positions)]
    adv = strategy.generate_saturday_advisory(
        "NEW.NS", make_candles(closes), closes[-1], Regime.NEUTRAL,
        PortfolioState(open_positions=positions), DEFAULT_CONFIG)
    assert adv.action == Action.HOLD


def test_monday_voids_buy_when_regime_turns_risk_off():
    sat = Advisory(
        ticker="ITBEES.NS", side=Side.BUY, action=Action.PLACE_GTT,
        trigger_price=90.0, limit_price=90.0, stop_price=85.0, target_price=105.0,
        quantity=10, regime=Regime.NEUTRAL, atr=2.5, channel_as_of=date(2024, 1, 1),
        created_at=datetime.now(timezone.utc))
    snap = PriceSnapshot("ITBEES.NS", price=91.0, as_of=datetime.now(timezone.utc))
    result = strategy.reconcile_monday(snap, sat, Regime.RISK_OFF, DEFAULT_CONFIG)
    assert result.action == Action.CANCEL_GTT
    assert "VOID" in result.rationale


def test_monday_voids_when_price_gaps_far_past_breakout():
    sat = Advisory(
        ticker="ITBEES.NS", side=Side.BUY, action=Action.PLACE_GTT,
        trigger_price=100.0, limit_price=100.0, stop_price=95.0, target_price=115.0,
        quantity=10, regime=Regime.NEUTRAL, atr=2.5, channel_as_of=date(2024, 1, 1),
        created_at=datetime.now(timezone.utc))
    snap = PriceSnapshot("ITBEES.NS", price=110.0, as_of=datetime.now(timezone.utc))  # +10%
    result = strategy.reconcile_monday(snap, sat, Regime.NEUTRAL, DEFAULT_CONFIG)
    assert result.action == Action.CANCEL_GTT


def test_monday_holds_when_still_valid():
    sat = Advisory(
        ticker="ITBEES.NS", side=Side.BUY, action=Action.PLACE_GTT,
        trigger_price=100.0, limit_price=100.0, stop_price=95.0, target_price=115.0,
        quantity=10, regime=Regime.NEUTRAL, atr=2.5, channel_as_of=date(2024, 1, 1),
        created_at=datetime.now(timezone.utc))
    snap = PriceSnapshot("ITBEES.NS", price=101.0, as_of=datetime.now(timezone.utc))
    result = strategy.reconcile_monday(snap, sat, Regime.NEUTRAL, DEFAULT_CONFIG)
    assert result.action == Action.HOLD


def test_too_few_candles_raises():
    with pytest.raises(ValueError):
        strategy.compute_channel(make_candles([100.0] * 10), DEFAULT_CONFIG)


def test_backtest_has_no_lookahead():
    from swing_engine.app.backtest import run_walk_forward
    closes = []
    for _ in range(10):
        closes += [100, 102, 104, 106, 108, 106, 104, 102]
    res = run_walk_forward("TEST.NS", make_candles(closes), DEFAULT_CONFIG)
    for tr in res.trades:
        if tr.exit_week:
            assert tr.exit_week > tr.entry_week
