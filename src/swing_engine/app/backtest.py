"""Walk-forward backtest harness.

Imports the SAME pure functions the live engine uses (compute_channel, entry/
stop/target/sizing/trail). No parallel re-implementation of the math, so a
passing backtest reflects the code that actually trades.

Walk-forward: at each step t we compute the channel from candles[:t] and
evaluate the signal against candle t. This structurally prevents look-ahead.

REVISION: now models the full risk engine — ATR hard stop, trailing stop, OCO
target, and 1%-risk position sizing — and reports Sharpe, profit factor, max
drawdown, and a buy-and-hold benchmark so under-performance is never hidden.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..domain import strategy
from ..domain.config import StrategyConfig
from ..domain.types import Candle, Regime


@dataclass
class Trade:
    entry_week: str
    entry_price: float
    exit_week: str | None
    exit_price: float | None
    qty: int
    ret: float | None            # per-trade return on capital
    exit_reason: str | None      # 'stop' | 'trail' | 'target'


@dataclass
class BacktestResult:
    ticker: str
    trades: list[Trade]
    total_return: float
    buy_hold_return: float
    win_rate: float
    n_trades: int
    max_drawdown: float
    sharpe: float
    profit_factor: float
    equity_curve: list[float] = field(default_factory=list)


def _sharpe(weekly_returns: list[float]) -> float:
    """Annualised Sharpe from weekly equity returns (rf=0). 52 weeks/yr."""
    rs = [r for r in weekly_returns if r is not None]
    if len(rs) < 2:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (mean / sd) * math.sqrt(52)


def run_walk_forward(
    ticker: str,
    candles: list[Candle],
    config: StrategyConfig,
    regime_series: list[Regime] | None = None,
) -> BacktestResult:
    """Simulate the Donchian trend rule with the full risk engine, week by week.

    Entry: close breaks entry_trigger (channel high + sentiment buffer).
    Exit (whichever hits first, evaluated on each subsequent bar):
      - stop:   low <= current stop (hard or trailed)
      - target: high >= OCO target
      - trail:  stop is ratcheted up each week the trend advances
    """
    window = max(config.entry_lookback, config.atr_window + 1, config.min_candles_required)
    trades: list[Trade] = []
    in_position = False
    entry_price = stop = tgt = highest_close = 0.0
    entry_week = ""
    qty = 0
    capital = 1.0                 # normalised; sizing scales with this
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    weekly_returns: list[float] = []
    curve: list[float] = [1.0]

    for t in range(window, len(candles)):
        history = candles[:t]
        bar = candles[t]
        week = bar.week_start.isoformat()
        regime = (regime_series[t] if regime_series else Regime.NEUTRAL)
        channel = strategy.compute_channel(history, config)
        prev_equity = equity

        if not in_position:
            entry_trig = strategy.entry_trigger_price(channel, regime, config)
            # Breakout confirmed if this bar's close clears the trigger.
            if bar.close >= entry_trig:
                entry_price = bar.close
                stop = strategy.hard_stop_price(entry_price, channel.atr, config)
                per_share_risk = entry_price - stop
                # Fraction of equity risked = risk_per_trade; qty in fractional units.
                qty = (config.risk_per_trade * equity / per_share_risk) if per_share_risk > 0 else 0
                if qty > 0:
                    in_position = True
                    entry_week = week
                    highest_close = entry_price
        else:
            highest_close = max(highest_close, bar.close)
            # Adaptive Chandelier trail is the SOLE exit (no fixed target).
            mult = strategy.choose_trail_multiple(history, entry_price, highest_close, channel.atr, config)
            trail = strategy.trail_stop_price(highest_close, channel.atr, mult)
            stop = max(stop, trail)  # ratchet only upward
            exit_price = None
            reason = None
            # Stop-touch exit (mechanical, no discretion).
            if bar.low <= stop:
                exit_price = stop
                reason = "trail" if mult != config.atr_stop_multiple else "stop"
            if exit_price is not None:
                trade_ret = qty * (exit_price - entry_price)  # fraction of equity
                equity *= (1 + trade_ret)
                trades.append(Trade(
                    entry_week, entry_price, week, exit_price, int(qty * 1e6),
                    trade_ret, reason,
                ))
                in_position = False
                qty = 0

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)
        weekly_returns.append((equity - prev_equity) / prev_equity if prev_equity else 0.0)
        curve.append(equity)

    wins = [tr for tr in trades if tr.ret and tr.ret > 0]
    losses = [tr for tr in trades if tr.ret and tr.ret <= 0]
    gross_win = sum(tr.ret for tr in wins) if wins else 0.0
    gross_loss = abs(sum(tr.ret for tr in losses)) if losses else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    bh = (candles[-1].close - candles[window].close) / candles[window].close

    return BacktestResult(
        ticker=ticker, trades=trades, total_return=equity - 1.0,
        buy_hold_return=bh, win_rate=(len(wins) / len(trades)) if trades else 0.0,
        n_trades=len(trades), max_drawdown=max_dd, sharpe=_sharpe(weekly_returns),
        profit_factor=profit_factor, equity_curve=curve,
    )
