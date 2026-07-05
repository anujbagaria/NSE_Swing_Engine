"""Ports: the interfaces the domain depends on. Adapters implement them.

The strategy code depends only on these Protocols — so yfinance can be swapped
for a broker feed, or Gemini for another model, without touching the math.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import Advisory, Candle, PortfolioState, PriceSnapshot, Regime


@runtime_checkable
class MarketDataPort(Protocol):
    def fetch_weekly_candles(self, ticker: str) -> list[Candle]:
        """Completed weekly (Mon-Fri) candles for the configured lookback.
        MUST drop any still-forming current-week bar before returning."""

    def fetch_price_snapshot(self, ticker: str) -> PriceSnapshot:
        """Latest available (possibly ~15 min delayed) price, for Monday."""

    def fetch_news(self, ticker: str) -> str:
        """Recent headlines/text for the ticker, strictly dated <= T-1."""


@runtime_checkable
class SentimentPort(Protocol):
    def regime_tag(self, news_text: str, smoothed: bool) -> Regime:
        """Coarse regime tag. smoothed=True applies the weekly EWMA (Saturday);
        smoothed=False returns the raw fresh tag (Monday delta run)."""


@runtime_checkable
class StatePort(Protocol):
    def acquire_lock(self, run_id: str) -> None: ...
    def release_lock(self) -> None: ...
    def load(self) -> PortfolioState: ...
    def commit(self, state: PortfolioState, run_id: str) -> str: ...


@runtime_checkable
class NotifierPort(Protocol):
    def send_advisories(self, advisories: list[Advisory]) -> None: ...
    def send_failure(self, error: str, run_id: str) -> None: ...
    def send_heartbeat(self, run_id: str) -> None: ...
