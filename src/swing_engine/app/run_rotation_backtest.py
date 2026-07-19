"""CLI: run the dual-momentum rotation backtest on REAL yfinance data.

    python -m swing_engine.app.run_rotation_backtest

Fetches weekly candles for the configured universe, aligns them to a common
weekly index, and runs the portfolio-level walk-forward. Reports the mandate's
metrics against an equal-weight buy-and-hold-the-universe benchmark, NET of a
round-trip cost so results aren't flattered by ignoring friction.

As always: past performance is not indicative of future results. This is a single
macro regime (whatever the last ~5y held); it is not a substitute for testing
across a full bull AND bear cycle.
"""
from __future__ import annotations

import sys

from ..adapters.market_data_yf import YFinanceMarketData
from ..domain.config import DEFAULT_CONFIG
from .universe_loader import load_universe
from .rotation_backtest import run_rotation_backtest


def _align(candles_by_ticker):
    """Trim all series to a common length from the right (most recent), so week t
    lines up across tickers. Simple, robust alignment for equal-cadence weekly data."""
    n = min(len(v) for v in candles_by_ticker.values())
    return {k: v[-n:] for k, v in candles_by_ticker.items()}


def main(argv: list[str] | None = None) -> int:
    config = DEFAULT_CONFIG
    market = YFinanceMarketData(config)

    print("Fetching weekly candles for the universe...")
    universe_tickers, warning = load_universe(".")
    if warning:
        print(f"  NOTE: {warning}")
    raw = {}
    for ticker in universe_tickers:
        try:
            raw[ticker] = market.fetch_weekly_candles(ticker)
            print(f"  {ticker}: {len(raw[ticker])} weeks")
        except Exception as exc:  # noqa: BLE001
            print(f"  {ticker}: ERROR {exc}")

    if len(raw) < config.top_n + 1:
        print("Not enough tickers with data to run rotation. Aborting.")
        return 1

    universe = _align(raw)
    for cost in (0.0, 0.002, 0.005):
        r = run_rotation_backtest(universe, config, round_trip_cost=cost)
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        ret_verdict = "BEATS" if r.total_return > r.buy_hold_return else "lags "
        # The ALPHA verdict: risk-adjusted, per the mandate.
        sharpe_verdict = "BEATS" if r.sharpe > r.buy_hold_sharpe else "lags "
        print(f"\n--- round-trip cost {cost:.1%} ---")
        print(f"  weeks={r.weeks}  rotations={r.n_rotations}  avg_holdings={r.avg_holdings:.2f}")
        print(f"  TOTAL RETURN   strategy {r.total_return:+.1%}   buy&hold {r.buy_hold_return:+.1%}"
              f"   [{ret_verdict} b&h]")
        print(f"  RISK-ADJUSTED  Sharpe {r.sharpe:.2f}      buy&hold {r.buy_hold_sharpe:.2f}"
              f"     [{sharpe_verdict} b&h]  <-- the alpha claim")
        print(f"  DRAWDOWN       strategy {r.max_drawdown:.1%}    buy&hold {r.buy_hold_max_drawdown:.1%}")
        print(f"  CAGR={r.cagr:+.1%}   profit_factor={pf}")

    print("\nRead the RISK-ADJUSTED line first: Sharpe vs buy&hold's OWN Sharpe is")
    print("the real alpha test. Beating total return while taking more risk is not alpha.")
    print("Single macro regime, in-sample. Costs matter; note how metrics decay with cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
