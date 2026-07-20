"""Regression test for the yfinance MultiIndex column bug that caused
'float() argument must be a string or a real number, not Series'."""
from __future__ import annotations

import pandas as pd

from swing_engine.adapters.market_data_yf import _flatten_columns, _scalar


def _frame():
    idx = pd.to_datetime(["2024-01-01", "2024-01-08"])
    return pd.DataFrame(
        {"Open": [100, 102], "High": [105, 107], "Low": [99, 101],
         "Close": [104, 106], "Volume": [1000, 1100]}, index=idx)


def test_flatten_flat_columns_passthrough():
    f = _flatten_columns(_frame(), "X.NS")
    assert list(f.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_flatten_price_ticker_multiindex():
    df = _frame()
    df.columns = pd.MultiIndex.from_product([df.columns, ["X.NS"]])
    f = _flatten_columns(df, "X.NS")
    assert "Close" in f.columns
    assert _scalar(f.iloc[0]["Close"]) == 104.0


def test_flatten_ticker_price_multiindex():
    df = _frame()
    df.columns = pd.MultiIndex.from_product([["X.NS"], df.columns])
    f = _flatten_columns(df, "X.NS")
    assert "Close" in f.columns
    assert _scalar(f.iloc[0]["Close"]) == 104.0


def test_scalar_from_series_does_not_crash():
    # the exact input that produced the production error
    assert _scalar(pd.Series([104.0])) == 104.0


def test_scalar_from_nan_returns_none():
    assert _scalar(float("nan")) is None


def test_scalar_from_garbage_returns_none():
    assert _scalar("not a number") is None
