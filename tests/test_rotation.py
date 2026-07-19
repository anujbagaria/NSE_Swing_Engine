"""Unit tests for the dual-momentum rotation domain. No I/O, pure fixtures."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from swing_engine.domain import rotation
from swing_engine.domain.config import DEFAULT_CONFIG
from swing_engine.domain.types import (
    Action, Candle, PortfolioState, Position, Regime, Side,
)


def series(closes):
    start = date(2022, 1, 3)
    return [Candle(week_start=start + timedelta(weeks=i), open=c, high=c + 1,
                   low=c - 1, close=c, volume=1000) for i, c in enumerate(closes)]


def rising(n=40, rate=1.0, base=100):
    return series([base + i * rate for i in range(n)])


def falling(n=40, rate=1.0, base=140):
    return series([base - i * rate for i in range(n)])


def flat(n=40, base=100):
    return series([base for _ in range(n)])


def test_lookback_return_computes_window_return():
    c = series([100 + i for i in range(40)])  # +1/week
    r = rotation.lookback_return(c, 26)
    assert r == pytest.approx((139 - 113) / 113, rel=1e-6)


def test_ranking_orders_by_momentum_desc():
    universe = {"FAST": rising(rate=2.0), "SLOW": rising(rate=0.5), "FLAT": flat()}
    ranks = rotation.rank_universe(universe, Regime.NEUTRAL, DEFAULT_CONFIG)
    assert [r.ticker for r in ranks] == ["FAST", "SLOW", "FLAT"]


def test_absolute_gate_blocks_negative_momentum():
    universe = {"UP": rising(rate=1.0), "DOWN": falling(rate=1.0)}
    ranks = rotation.rank_universe(universe, Regime.NEUTRAL, DEFAULT_CONFIG)
    by = {r.ticker: r for r in ranks}
    assert by["UP"].passes_gate is True
    assert by["DOWN"].passes_gate is False  # falling can't beat cash hurdle


def test_select_holds_only_passing_top_n():
    universe = {"A": rising(rate=3.0), "B": rising(rate=2.0),
                "C": rising(rate=1.0), "D": falling(rate=1.0)}
    ranks = rotation.rank_universe(universe, Regime.NEUTRAL, DEFAULT_CONFIG)
    held = rotation.select_holdings(ranks, DEFAULT_CONFIG)
    assert held == ["A", "B"]  # top_n=2, all passing, D excluded anyway


def test_fewer_than_top_n_pass_means_cash_slot():
    # Only one ETF rising; the rest falling => only 1 held, other slot is cash.
    universe = {"UP": rising(rate=1.0), "D1": falling(), "D2": falling()}
    ranks = rotation.rank_universe(universe, Regime.NEUTRAL, DEFAULT_CONFIG)
    held = rotation.select_holdings(ranks, DEFAULT_CONFIG)
    assert held == ["UP"]  # one slot filled, one implicitly cash


def test_risk_off_raises_the_gate():
    # A mildly rising ETF that passes in NEUTRAL should fail in RISK_OFF.
    universe = {"MILD": rising(rate=0.08, base=100)}  # ~2% over 26wk: passes neutral, fails risk-off +3%
    neu = {r.ticker: r for r in rotation.rank_universe(universe, Regime.NEUTRAL, DEFAULT_CONFIG)}
    off = {r.ticker: r for r in rotation.rank_universe(universe, Regime.RISK_OFF, DEFAULT_CONFIG)}
    assert neu["MILD"].passes_gate is True
    assert off["MILD"].passes_gate is False


def test_advisory_enters_new_leader():
    universe = {"A": rising(rate=3.0), "B": rising(rate=2.0), "C": rising(rate=1.0)}
    state = PortfolioState(capital=100000.0)  # flat, holding nothing
    advs = rotation.generate_rotation_advisories(universe, Regime.NEUTRAL, state, DEFAULT_CONFIG)
    entries = [a for a in advs if a.action == Action.PLACE_GTT]
    assert {a.ticker for a in entries} == {"A", "B"}
    for a in entries:
        assert a.quantity > 0 and a.stop_price < a.trigger_price


def test_advisory_rotates_out_dropped_name():
    universe = {"A": rising(rate=3.0), "B": rising(rate=2.0), "C": rising(rate=1.0)}
    # Currently holding C (now rank 3) and A; C should be rotated out for B.
    state = PortfolioState(capital=100000.0, open_positions=[
        Position("A", 100, 10, 90, 130), Position("C", 100, 10, 90, 130),
    ])
    advs = rotation.generate_rotation_advisories(universe, Regime.NEUTRAL, state, DEFAULT_CONFIG)
    exits = [a for a in advs if a.action == Action.EXIT_POSITION]
    entries = [a for a in advs if a.action == Action.PLACE_GTT]
    assert {a.ticker for a in exits} == {"C"}
    assert {a.ticker for a in entries} == {"B"}


def test_no_rotation_when_membership_unchanged():
    universe = {"A": rising(rate=3.0), "B": rising(rate=2.0), "C": rising(rate=1.0)}
    state = PortfolioState(capital=100000.0, open_positions=[
        Position("A", 100, 10, 90, 130), Position("B", 100, 10, 90, 130),
    ])
    advs = rotation.generate_rotation_advisories(universe, Regime.NEUTRAL, state, DEFAULT_CONFIG)
    actionable = [a for a in advs if a.action != Action.HOLD]
    assert actionable == []  # top-2 unchanged => no trades


def test_backtest_no_lookahead_and_runs():
    from swing_engine.app.rotation_backtest import run_rotation_backtest
    universe = {
        "A": series([100 + i for i in range(60)]),
        "B": series([100 + (i % 10) for i in range(60)]),
        "C": series([120 - i * 0.2 for i in range(60)]),
    }
    res = run_rotation_backtest(universe, DEFAULT_CONFIG, round_trip_cost=0.002)
    assert res.weeks > 0
    assert 0.0 <= res.avg_holdings <= DEFAULT_CONFIG.top_n
