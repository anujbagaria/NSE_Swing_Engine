"""Tests for the external universe file and per-ticker fault isolation."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from swing_engine.app.universe_loader import load_universe
from swing_engine.domain.config import DEFAULT_CONFIG
from swing_engine.domain.types import Candle, PortfolioState, PriceSnapshot, Regime


# ---------- universe loader ----------

def test_missing_file_falls_back_with_warning(tmp_path):
    tickers, warning = load_universe(tmp_path)
    assert tickers == list(DEFAULT_CONFIG.universe)
    assert warning and "not found" in warning


def test_valid_file_loads_and_dedupes(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "universe.json").write_text(json.dumps({
        "universe": ["AAA.NS", "BBB.NS", "AAA.NS", "  CCC.NS  ", ""]
    }))
    tickers, warning = load_universe(tmp_path)
    assert tickers == ["AAA.NS", "BBB.NS", "CCC.NS"]
    assert warning is None


def test_malformed_json_falls_back_with_warning(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "universe.json").write_text("{not valid json")
    tickers, warning = load_universe(tmp_path)
    assert tickers == list(DEFAULT_CONFIG.universe)
    assert warning and "malformed" in warning


def test_too_few_tickers_falls_back(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "universe.json").write_text(json.dumps({"universe": ["ONLY.NS"]}))
    tickers, warning = load_universe(tmp_path)
    assert tickers == list(DEFAULT_CONFIG.universe)
    assert warning and ">= 2" in warning


# ---------- per-ticker fault isolation in the orchestrator ----------

def _candles(n=60):
    s = date(2023, 1, 2)
    return [Candle(week_start=s + timedelta(weeks=i), open=100 + i, high=102 + i,
                   low=99 + i, close=100 + i, volume=1000) for i in range(n)]


class FailingMarket:
    """BAD.NS raises on fetch; every other ticker returns clean data."""
    def fetch_weekly_candles(self, ticker):
        if ticker == "BAD.NS":
            raise RuntimeError("yfinance returned no weekly data for BAD.NS")
        return _candles()

    def fetch_price_snapshot(self, ticker):
        if ticker == "BAD.NS":
            raise RuntimeError("no daily data")
        from datetime import datetime, timezone
        return PriceSnapshot(ticker, 160.0, datetime.now(timezone.utc))

    def fetch_news(self, ticker):
        return "steady markets"


class StubSentiment:
    history = [0.0]
    def regime_tag(self, text, smoothed):
        return Regime.NEUTRAL


class MemoryState:
    def __init__(self, repo="."):
        self.repo = repo
        self.s = PortfolioState(capital=100000.0)
    def acquire_lock(self, r): pass
    def release_lock(self): pass
    def load(self): return self.s
    def commit(self, state, run_id, sentiment_history=None):
        self.s = state
        return "deadbeef"


class RecordingNotifier:
    def __init__(self):
        self.advisories = None
        self.failed = None
        self.failures_sent = []
        self.heartbeats = []
    def send_advisories(self, advisories, failed_tickers=None):
        self.advisories = advisories
        self.failed = failed_tickers
    def send_failure(self, error, run_id):
        self.failures_sent.append(error)
    def send_heartbeat(self, run_id):
        self.heartbeats.append(run_id)


def _make_universe_file(tmp_path, tickers):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "universe.json").write_text(json.dumps({"universe": tickers}))


def test_bad_ticker_is_skipped_and_reported(tmp_path):
    from swing_engine.app.orchestrator import Orchestrator
    _make_universe_file(tmp_path, ["GOOD1.NS", "BAD.NS", "GOOD2.NS"])
    notifier = RecordingNotifier()
    orch = Orchestrator(DEFAULT_CONFIG, FailingMarket(), StubSentiment(),
                        MemoryState(repo=tmp_path), notifier)
    advisories = orch.run_saturday()   # must NOT raise
    tickers_ok = {a.ticker for a in advisories}
    assert tickers_ok == {"GOOD1.NS", "GOOD2.NS"}          # bad one skipped
    assert notifier.failed is not None
    assert notifier.failed[0][0] == "BAD.NS"                # and reported
    assert "no weekly data" in notifier.failed[0][1]
    assert notifier.heartbeats                              # run completed OK


def test_all_tickers_failing_fails_the_run(tmp_path):
    from swing_engine.app.orchestrator import Orchestrator
    _make_universe_file(tmp_path, ["BAD.NS", "BAD.NS2"])

    class AllFail(FailingMarket):
        def fetch_weekly_candles(self, ticker):
            raise RuntimeError("vendor down")

    notifier = RecordingNotifier()
    orch = Orchestrator(DEFAULT_CONFIG, AllFail(), StubSentiment(),
                        MemoryState(repo=tmp_path), notifier)
    with pytest.raises(RuntimeError, match="All 2 tickers failed"):
        orch.run_saturday()
    assert notifier.failures_sent                           # loud, not silent


def test_universe_warning_is_surfaced_not_swallowed(tmp_path):
    from swing_engine.app.orchestrator import Orchestrator
    # No universe file at all -> default universe used, warning reported.
    notifier = RecordingNotifier()
    orch = Orchestrator(DEFAULT_CONFIG, FailingMarket(), StubSentiment(),
                        MemoryState(repo=tmp_path), notifier)
    orch.run_saturday()
    assert notifier.failed is not None
    assert notifier.failed[0][0] == "universe.json"
    assert "not found" in notifier.failed[0][1]
