"""yfinance implementation of MarketDataPort.

All vendor quirks are quarantined here: silent NaNs, duplicate index rows,
the still-forming current-week candle, OHLC sanity. The domain never sees a
raw DataFrame — only validated Candle / PriceSnapshot value objects.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..domain.config import StrategyConfig
from ..domain.types import Candle, PriceSnapshot


class YFinanceMarketData:
    def __init__(self, config: StrategyConfig, period_override: str | None = None):
        self.config = config
        # e.g. "max" for full-cycle backtests; None = config-driven lookback.
        self.period_override = period_override

    def fetch_weekly_candles(self, ticker: str) -> list[Candle]:
        import pandas as pd  # local import keeps domain import-light
        import yfinance as yf

        period = self.period_override or f"{self.config.lookback_years + 1}y"
        df = yf.download(
            ticker, period=period, interval="1wk",
            auto_adjust=True, progress=False,
        )
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned no weekly data for {ticker}")

        # yfinance may return a MultiIndex column frame for a single ticker.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[~df.index.duplicated(keep="last")]
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df.sort_index()

        # Drop the still-forming current week. Even though we run on Saturday,
        # guard explicitly: keep only bars whose week has fully closed.
        today = datetime.now(timezone.utc).date()
        candles: list[Candle] = []
        for idx, row in df.iterrows():
            week_start = idx.date() if hasattr(idx, "date") else idx
            # a weekly bar labelled with week_start is complete once we're past
            # that week's Friday (week_start + 4 days).
            if (today - week_start).days < 5:
                continue
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            v = float(row.get("Volume", 0) or 0)
            if not (h >= max(o, c) and l <= min(o, c)):
                # OHLC sanity failed — skip rather than trust bad data.
                continue
            candles.append(Candle(week_start=week_start, open=o, high=h, low=l, close=c, volume=v))

        if not candles:
            raise RuntimeError(f"No valid completed weekly candles for {ticker}")
        return candles

    def fetch_price_snapshot(self, ticker: str) -> PriceSnapshot:
        import yfinance as yf

        df = yf.download(ticker, period="5d", interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned no daily data for {ticker}")
        last_close = float(df["Close"].dropna().iloc[-1])
        return PriceSnapshot(
            ticker=ticker, price=last_close,
            as_of=datetime.now(timezone.utc),
        )

    def fetch_news(self, ticker: str) -> str:
        """yfinance exposes a .news list per ticker. We concatenate recent
        titles/summaries as the text handed to the sentiment adapter. Kept
        deliberately thin — the domain treats this as opaque text."""
        import yfinance as yf

        try:
            items = yf.Ticker(ticker).news or []
        except Exception:
            items = []
        titles = []
        for it in items[:15]:
            content = it.get("content", it)  # yfinance schema has shifted over versions
            title = content.get("title") if isinstance(content, dict) else None
            if title:
                titles.append(title)
        return " | ".join(titles)
