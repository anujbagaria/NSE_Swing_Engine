"""Full-cycle rotation backtest: 2018 onward, INCLUDING the 2020 crash.

This tests the operator's actual thesis (ride India's bulls, preserve cash when
none exist, recover with the market through shocks) rather than a crash-avoidance
thesis they explicitly disowned. It measures alpha-over-benchmark across a full
bull-AND-bear cycle, risk-adjusted.

IMPORTANT — look-ahead honesty (mandate: guard against data look-ahead bias):
The sentiment gate is held NEUTRAL for the entire historical run. An LLM asked
for "2020 sentiment" already knows how 2020 resolved, so any historical sentiment
tag is contaminated by hindsight. Running price+momentum+stops with sentiment
neutral gives an unbiased read on the mechanical strategy. Sentiment's real value
can only be judged going forward, in live paper-trading. This is enforced here by
passing a NEUTRAL regime series, not by trusting a promise.

    python -m swing_engine.app.run_fullcycle_backtest
"""
from __future__ import annotations

from ..adapters.market_data_yf import YFinanceMarketData
from ..domain.config import DEFAULT_CONFIG
from .universe_loader import load_universe
from ..domain.types import Regime
from .rotation_backtest import run_rotation_backtest


def _align(candles_by_ticker):
    n = min(len(v) for v in candles_by_ticker.values())
    return {k: v[-n:] for k, v in candles_by_ticker.items()}


def main(argv=None) -> int:
    config = DEFAULT_CONFIG
    # Ask the adapter for the longest weekly history available (period='max').
    market = YFinanceMarketData(config, period_override="max")

    print("Fetching MAX weekly history (targeting 2018 onward incl. 2020 crash)...")
    universe_tickers, warning = load_universe(".")
    if warning:
        print(f"  NOTE: {warning}")
    raw = {}
    for ticker in universe_tickers:
        try:
            raw[ticker] = market.fetch_weekly_candles(ticker)
            first = raw[ticker][0].week_start if raw[ticker] else "?"
            print(f"  {ticker}: {len(raw[ticker])} weeks (from {first})")
        except Exception as exc:  # noqa: BLE001
            print(f"  {ticker}: ERROR {exc}")

    if len(raw) < config.top_n + 1:
        print("Not enough tickers with data. Aborting.")
        return 1

    universe = _align(raw)
    n = min(len(v) for v in universe.values())
    # Sentiment NEUTRAL for the whole history — enforced, not assumed.
    neutral_series = [Regime.NEUTRAL] * n

    print("\nSentiment gate held NEUTRAL for the historical run (look-ahead honesty).")
    print("Stops active. Measuring alpha over benchmark across the full cycle.\n")

    for cost in (0.002, 0.005):
        r = run_rotation_backtest(universe, config, round_trip_cost=cost,
                                  regime_series=neutral_series)
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        ret_v = "BEATS" if r.total_return > r.buy_hold_return else "lags "
        sh_v = "BEATS" if r.sharpe > r.buy_hold_sharpe else "lags "
        dd_v = "shallower" if r.max_drawdown < r.buy_hold_max_drawdown else "DEEPER"
        print(f"--- round-trip cost {cost:.1%} ---")
        print(f"  weeks={r.weeks}  rotations={r.n_rotations}  avg_holdings={r.avg_holdings:.2f}")
        print(f"  TOTAL RETURN   strat {r.total_return:+.1%}   b&h {r.buy_hold_return:+.1%}   [{ret_v} b&h]")
        print(f"  RISK-ADJUSTED  Sharpe {r.sharpe:.2f}   b&h {r.buy_hold_sharpe:.2f}   [{sh_v} b&h]  <-- alpha")
        print(f"  CRASH TEST     maxDD strat {r.max_drawdown:.1%}   b&h {r.buy_hold_max_drawdown:.1%}   [{dd_v}]")
        print(f"  CAGR={r.cagr:+.1%}   profit_factor={pf}\n")

    print("How to read this against your thesis:")
    print("  - Alpha claim = the RISK-ADJUSTED line (Sharpe vs b&h's own Sharpe).")
    print("  - Your thesis is recovery+alpha through the cycle, NOT crash avoidance;")
    print("    the CRASH TEST line shows what the mechanical strategy did to drawdown")
    print("    with sentiment OFF. Any sentiment benefit is upside to prove forward.")
    print("  - Still one country, one history. Past performance != future results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
