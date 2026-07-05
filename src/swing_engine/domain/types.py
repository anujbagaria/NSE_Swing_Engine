"""Typed value objects that cross architectural boundaries.

Nothing in this module imports pandas, yfinance, Gemini, or any I/O library.
Adapters convert vendor payloads into these objects at the edge, so the domain
never sees a raw vendor type.

REVISION: strategy core is now Donchian trend-following. Advisories carry a
stop_price and quantity (position sizing), and exits are modelled as Kite OCO
GTTs (stop-loss + target in one order) so a position is never left naked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class Regime(str, Enum):
    """Coarse market-sentiment regime. A calibrated numeric score from an LLM
    is not trustworthy; a coarse tag is."""
    RISK_OFF = "risk_off"
    NEUTRAL = "neutral"
    RISK_ON = "risk_on"


class Side(str, Enum):
    BUY = "buy"
    EXIT = "exit"


class Action(str, Enum):
    """What an advisory tells the human to do on Kite (advise-only; the engine
    never places or cancels orders itself)."""
    PLACE_GTT = "place_gtt"            # new entry: single buy GTT
    PLACE_OCO_EXIT = "place_oco_exit"  # you were filled: place OCO stop+target
    UPDATE_TRAIL = "update_trail"      # trend intact: raise the trailing stop
    CANCEL_GTT = "cancel_gtt"          # a prior entry advisory is void; delete it
    EXIT_POSITION = "exit_position"    # get out now (regime/again structure broke)
    HOLD = "hold"                      # no change


@dataclass(frozen=True)
class Candle:
    """One completed weekly OHLCV bar. Weekly = Monday-Friday, always closed."""
    week_start: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Channel:
    """Donchian channel for one ticker, computed on completed weekly candles.

    upper = highest high over entry_lookback (the breakout level to buy)
    lower = lowest low over exit_lookback (the structural exit level)
    atr    = average true range over atr_window (volatility, for stops & sizing)
    """
    upper: float
    lower: float
    atr: float
    entry_lookback: int
    exit_lookback: int
    as_of_week: date


@dataclass(frozen=True)
class Advisory:
    """A single actionable recommendation emitted by a run. Persisted in state
    so the next run and the Monday delta run can reconcile against it."""
    ticker: str
    side: Side
    action: Action
    trigger_price: float          # entry breakout level, or exit trigger
    limit_price: float            # limit for the triggered order
    stop_price: float             # protective stop (hard floor). 0.0 if N/A
    target_price: float           # OCO take-profit leg. 0.0 if trailing/none
    quantity: int                 # position size from 1% risk rule. 0 if N/A
    regime: Regime
    atr: float                    # ATR used for stop distance & sizing
    channel_as_of: date
    created_at: datetime
    rationale: str = ""


@dataclass(frozen=True)
class PriceSnapshot:
    """A single (possibly delayed) price used by the Monday pre-open delta run."""
    ticker: str
    price: float
    as_of: datetime


@dataclass
class Position:
    """An open position the user has confirmed. Carries the live stop so the
    Saturday run can ratchet a trailing stop upward."""
    ticker: str
    entry_price: float
    quantity: int
    stop_price: float
    highest_close: float          # peak close since entry, for trailing logic


@dataclass
class PortfolioState:
    """The entire persisted state, serialized as one atomic JSON blob."""
    schema_version: int = 2
    capital: float = 100000.0                       # total deployable capital (INR)
    last_saturday_run: Optional[str] = None
    last_monday_run: Optional[str] = None
    active_advisories: list[Advisory] = field(default_factory=list)
    open_positions: list[Position] = field(default_factory=list)

    def advisory_for(self, ticker: str) -> Optional[Advisory]:
        for a in self.active_advisories:
            if a.ticker == ticker:
                return a
        return None

    def position_for(self, ticker: str) -> Optional[Position]:
        for p in self.open_positions:
            if p.ticker == ticker:
                return p
        return None
