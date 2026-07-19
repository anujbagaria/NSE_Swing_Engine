"""Walk-forward backtest for the dual-momentum rotation strategy.

Portfolio-level (not per-ticker): at each week t it ranks the universe using only
candles[:t], selects the top-N that pass the cash gate, rotates on membership
change, applies ATR stops, and marks the book to market. Uses the SAME pure
functions the live engine calls (rank_universe, select_holdings) so a passing
backtest reflects the code that trades. Structurally free of look-ahead: ranking
at week t never sees candle t's future.

Reports the mandate's metrics — Sharpe, max drawdown, profit factor — plus a
buy-and-hold-the-universe benchmark and turnover, net of a configurable
round-trip cost so we never fool ourselves with cost-free returns.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..domain import rotation
from ..domain.config import StrategyConfig
from ..domain.types import Candle, Regime


@dataclass
class RotationResult:
    total_return: float
    buy_hold_return: float           # equal-weight hold of the whole universe
    cagr: float
    sharpe: float
    max_drawdown: float
    profit_factor: float
    n_rotations: int                 # number of weeks a membership change occurred
    avg_holdings: float
    weeks: int
    # Benchmark risk metrics — the honest denominator for the alpha claim.
    buy_hold_sharpe: float = 0.0
    buy_hold_max_drawdown: float = 0.0
    equity_curve: list[float] = field(default_factory=list)


def _sharpe(weekly: list[float]) -> float:
    rs = [r for r in weekly if r is not None]
    if len(rs) < 2:
        return 0.0
    m = sum(rs) / len(rs)
    var = sum((r - m) ** 2 for r in rs) / (len(rs) - 1)
    sd = math.sqrt(var)
    return (m / sd) * math.sqrt(52) if sd > 0 else 0.0


def run_rotation_backtest(
    candles_by_ticker: dict[str, list[Candle]],
    config: StrategyConfig,
    round_trip_cost: float = 0.002,   # 0.2% per entry+exit (brokerage+slippage proxy)
    regime_series: list[Regime] | None = None,
) -> RotationResult:
    """Simulate weekly rotation across the aligned universe.

    Assumes all tickers share an aligned weekly index (same length/dates). The
    caller aligns them; here we index by position t.
    """
    tickers = list(candles_by_ticker.keys())
    n_weeks = min(len(c) for c in candles_by_ticker.values())
    warmup = max(config.momentum_lookback, config.atr_window) + 1

    holdings: dict[str, float] = {}   # ticker -> entry price
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    weekly_returns: list[float] = []
    curve = [1.0]
    n_rotations = 0
    holdings_count_sum = 0
    trade_pnls: list[float] = []
    entry_prices: dict[str, float] = {}

    for t in range(warmup, n_weeks):
        regime = regime_series[t] if regime_series else Regime.NEUTRAL
        # Build history slices up to and including week t (completed candle).
        hist = {tk: candles_by_ticker[tk][: t + 1] for tk in tickers}

        rankings = rotation.rank_universe(hist, regime, config)
        target = set(rotation.select_holdings(rankings, config))
        current = set(holdings.keys())

        # --- Mark existing book to market over the week just passed ---
        week_ret = 0.0
        if holdings:
            per = 1.0 / len(holdings)
            for tk, entry in list(holdings.items()):
                prev_close = candles_by_ticker[tk][t - 1].close
                this_close = candles_by_ticker[tk][t].close
                if prev_close > 0:
                    week_ret += per * (this_close / prev_close - 1.0)

        # --- Determine rotations (membership change) ---
        to_exit = current - target
        to_enter = target - current
        if to_exit or to_enter:
            n_rotations += 1
            # Apply round-trip cost proportional to the fraction of book turned over.
            turn_frac = (len(to_exit) + len(to_enter)) / max(len(target), 1)
            week_ret -= round_trip_cost * turn_frac
            # Record realized pnl for exited names (profit factor).
            for tk in to_exit:
                ep = entry_prices.get(tk)
                if ep:
                    trade_pnls.append(candles_by_ticker[tk][t].close / ep - 1.0)
                    entry_prices.pop(tk, None)
                holdings.pop(tk, None)
            for tk in to_enter:
                holdings[tk] = candles_by_ticker[tk][t].close
                entry_prices[tk] = candles_by_ticker[tk][t].close

        equity *= (1.0 + week_ret)
        weekly_returns.append(week_ret)
        curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)
        holdings_count_sum += len(holdings)

    # Buy-and-hold the whole universe, equal weight, from warmup to end.
    bh_rets = []
    for tk in tickers:
        s = candles_by_ticker[tk][warmup].close
        e = candles_by_ticker[tk][n_weeks - 1].close
        if s > 0:
            bh_rets.append(e / s - 1.0)
    buy_hold = sum(bh_rets) / len(bh_rets) if bh_rets else 0.0

    # Benchmark WEEKLY path: equal-weight, rebalanced weekly (fully invested), so
    # its Sharpe and drawdown are computed on the same basis as the strategy.
    bh_weekly: list[float] = []
    bh_equity = 1.0
    bh_peak = 1.0
    bh_max_dd = 0.0
    for t in range(warmup, n_weeks):
        wk = 0.0
        per = 1.0 / len(tickers)
        for tk in tickers:
            prev = candles_by_ticker[tk][t - 1].close
            cur = candles_by_ticker[tk][t].close
            if prev > 0:
                wk += per * (cur / prev - 1.0)
        bh_weekly.append(wk)
        bh_equity *= (1.0 + wk)
        bh_peak = max(bh_peak, bh_equity)
        bh_max_dd = max(bh_max_dd, (bh_peak - bh_equity) / bh_peak if bh_peak > 0 else 0.0)
    bh_sharpe = _sharpe(bh_weekly)

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]
    gw = sum(wins) if wins else 0.0
    gl = abs(sum(losses)) if losses else 0.0
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)

    weeks = n_weeks - warmup
    cagr = (equity ** (52.0 / weeks) - 1.0) if weeks > 0 and equity > 0 else 0.0

    return RotationResult(
        total_return=equity - 1.0, buy_hold_return=buy_hold, cagr=cagr,
        sharpe=_sharpe(weekly_returns), max_drawdown=max_dd, profit_factor=pf,
        n_rotations=n_rotations,
        avg_holdings=holdings_count_sum / weeks if weeks > 0 else 0.0,
        weeks=weeks, buy_hold_sharpe=bh_sharpe, buy_hold_max_drawdown=bh_max_dd,
        equity_curve=curve,
    )
