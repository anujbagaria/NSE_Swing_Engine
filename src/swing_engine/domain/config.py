"""All tunable strategy parameters live here and nowhere else.

REVISION: Donchian trend-following config. The philosophy is unchanged from the
original — fixed, defensible priors, NOT grid-searched over ~156 weekly bars
(that overfits). Sentiment shifts a confirmation buffer on the breakout, never
the channel itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import Regime


@dataclass(frozen=True)
class StrategyConfig:
    # --- Universe: five liquid NSE ETFs. Corporate-action risk low. ---
    universe: tuple[str, ...] = (
        "MID150BEES.NS",
        "BANKBEES.NS",
        "ITBEES.NS",
        "GOLDBEES.NS",
        "SILVERBEES.NS",
    )

    # --- Donchian channel parameters (fixed priors) ---
    entry_lookback: int = 20      # buy when price breaks the 20-week high
    exit_lookback: int = 10       # structural exit on the 10-week low
    atr_window: int = 14          # ATR lookback (weeks) for stops & sizing
    lookback_years: int = 3       # ~156 weekly candles pulled

    # --- Risk management (THE core addition) ---
    risk_per_trade: float = 0.01  # risk 1% of capital per position
    atr_stop_multiple: float = 3.0    # initial hard stop = entry - 3*ATR (Chandelier default)
    # Three-tier Chandelier trailing stop (all literature-grounded, LeBeau / Traders Union):
    #   strong trend  -> wide trail  (3.0*ATR) : let winners run
    #   deep profit    -> medium trail (2.5*ATR): standard tighten
    #   momentum fade  -> tight trail (2.0*ATR) : protect open profit
    atr_trail_wide: float = 3.0
    atr_trail_deep_profit: float = 2.5
    atr_trail_fade: float = 2.0
    deep_profit_r: float = 2.0        # "deep in profit" = open gain >= 2R
    # No fixed profit target: trend-following edge lives in the fat right tail,
    # so the (adaptive) trailing stop is the SOLE exit. target_price stays 0.
    max_open_positions: int = 4       # cap concurrent risk / GTT budget
    max_gtt_budget: int = 40          # stay under Kite's 50-GTT hard cap

    # --- Sentiment -> breakout confirmation buffer (in ATR units) ---
    # Risk-off demands the breakout clear the channel by a wider margin before
    # buying (fewer, higher-quality entries); risk-on accepts a cleaner break.
    # entry trigger = channel.upper + buffer * ATR
    regime_buffer_atr: dict = field(default_factory=lambda: {
        Regime.RISK_OFF: +0.5,    # require breakout to clear by 0.5 ATR
        Regime.NEUTRAL:  +0.1,     # small confirmation buffer
        Regime.RISK_ON:  0.0,      # take the clean break
    })

    # --- Robustness knobs ---
    min_candles_required: int = 30    # abort if fewer completed candles

    # --- Sentiment smoothing (Saturday only; Monday uses raw fresh tag) ---
    ewma_span_weeks: int = 4

    # --- Monday delta run ---
    # If regime flips risk-off OR the entry setup is invalidated, void it.
    monday_price_tolerance: float = 0.0

    def buffer_for(self, regime: Regime) -> float:
        return self.regime_buffer_atr[regime]


DEFAULT_CONFIG = StrategyConfig()
