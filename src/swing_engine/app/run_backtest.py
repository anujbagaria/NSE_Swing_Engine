"""CLI to run the walk-forward backtest on REAL yfinance data.

Usage:
    python -m swing_engine.app.run_backtest              # whole universe
    python -m swing_engine.app.run_backtest MID150BEES.NS

This is the test that matters. The synthetic backtests bundled in the test
suite only prove the risk mechanics (stops, sizing, no look-ahead); they use
Brownian motion, which has no momentum, so they CANNOT show a trend edge. Only
real ETF history — ideally spanning a full bull AND bear cycle — tells you
whether the strategy actually earns its keep versus simply buying and holding.
"""
from __future__ import annotations

import sys

from ..adapters.market_data_yf import YFinanceMarketData
from ..domain.config import DEFAULT_CONFIG
from .backtest import run_walk_forward


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    config = DEFAULT_CONFIG
    tickers = argv if argv else list(config.universe)
    market = YFinanceMarketData(config)

    print(f"{'ticker':16s}{'trades':>7}{'win':>6}{'strat':>9}{'buy&hold':>10}"
          f"{'maxDD':>8}{'sharpe':>8}{'PF':>7}")
    print("-" * 72)
    for ticker in tickers:
        try:
            candles = market.fetch_weekly_candles(ticker)
            r = run_walk_forward(ticker, candles, config)
            pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
            verdict = "BEATS" if r.total_return > r.buy_hold_return else "lags "
            print(f"{ticker:16s}{r.n_trades:>7}{r.win_rate:>6.0%}"
                  f"{r.total_return:>+9.1%}{r.buy_hold_return:>+10.1%}"
                  f"{r.max_drawdown:>8.1%}{r.sharpe:>8.2f}{pf:>7}  {verdict} b&h")
        except Exception as exc:  # noqa: BLE001
            print(f"{ticker:16s}  ERROR: {exc}")
    print("\nReminder: past performance is not indicative of future results.")
    print("Slippage, GTT partial fills, and regime shifts not in-sample all erode live edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
