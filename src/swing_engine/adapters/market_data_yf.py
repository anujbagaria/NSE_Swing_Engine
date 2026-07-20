"""yfinance implementation of MarketDataPort.

All vendor quirks are quarantined here: silent NaNs, duplicate index rows,
the still-forming current-week candle, OHLC sanity. The domain never sees a
raw DataFrame — only validated Candle / PriceSnapshot value objects.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..domain.config import StrategyConfig
from ..domain.types import Candle, PriceSnapshot


def _flatten_columns(df, ticker: str):
    """Normalize yfinance columns to simple names: Open/High/Low/Close/Volume.

    Recent yfinance returns a MultiIndex even for a single ticker, and the level
    order has varied across versions — sometimes ('Close','TICKER'), sometimes
    ('TICKER','Close'). We pick whichever level actually contains the OHLC names
    so downstream code always sees flat column names. This is the fix for the
    'float() argument must be ... not Series' error, which happens when a
    MultiIndex leaves df['Close'] returning a whole sub-frame instead of a column.
    """
    import pandas as pd

    if isinstance(df.columns, pd.MultiIndex):
        ohlc = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        chosen = None
        for lvl in range(df.columns.nlevels):
            values = set(df.columns.get_level_values(lvl))
            if values & ohlc:               # this level holds the price fields
                chosen = lvl
                break
        if chosen is None:
            chosen = 0
        df = df.copy()
        df.columns = df.columns.get_level_values(chosen)
    # Deduplicate any repeated column labels (keep first), so df['Close'] is 1-D.
    df = df.loc[:, ~df.columns.duplicated(keep="first")]
    return df


def _scalar(value):
    """Coerce a cell to a float, even if pandas hands back a 1-element Series.

    The defensive core of the fix: if a stray duplicate column ever makes a cell
    a Series again, take its first element rather than crashing on float(Series).
    Returns None for missing/non-numeric so the caller can skip the row.
    """
    try:
        if hasattr(value, "iloc"):          # it's a Series, not a scalar
            value = value.iloc[0]
        f = float(value)
        return f if f == f else None        # NaN check (NaN != NaN)
    except (TypeError, ValueError, IndexError):
        return None


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

        df = _flatten_columns(df, ticker)

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
            o = _scalar(row["Open"]); h = _scalar(row["High"])
            l = _scalar(row["Low"]); c = _scalar(row["Close"])
            v = _scalar(row["Volume"]) if "Volume" in row else 0.0
            if o is None or h is None or l is None or c is None:
                continue
            if not (h >= max(o, c) and l <= min(o, c)):
                # OHLC sanity failed — skip rather than trust bad data.
                continue
            candles.append(Candle(week_start=week_start, open=o, high=h, low=l, close=c, volume=v or 0.0))

        if not candles:
            raise RuntimeError(f"No valid completed weekly candles for {ticker}")
        return candles

    def fetch_price_snapshot(self, ticker: str) -> PriceSnapshot:
        import yfinance as yf

        df = yf.download(ticker, period="5d", interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned no daily data for {ticker}")
        df = _flatten_columns(df, ticker)
        closes = df["Close"].dropna()
        if closes.empty:
            raise RuntimeError(f"no valid close price for {ticker}")
        last_close = _scalar(closes.iloc[-1])
        if last_close is None:
            raise RuntimeError(f"could not parse last close for {ticker}")
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
