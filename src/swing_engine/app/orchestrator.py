"""The orchestrator: wires ports and runs the two cycles.

No strategy math, no vendor calls — it coordinates. Side effects (lock, commit,
notify) live here; the domain stays pure. Any failure aborts WITHOUT committing,
releases the lock, and fires a failure alert, so a silent run can never be
mistaken for "no trades".
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..domain import strategy
from ..domain.config import StrategyConfig
from ..domain.ports import MarketDataPort, NotifierPort, SentimentPort, StatePort
from ..domain.types import Advisory, PortfolioState


class Orchestrator:
    def __init__(self, config, market, sentiment, state_store, notifier):
        self.config = config
        self.market = market
        self.sentiment = sentiment
        self.state_store = state_store
        self.notifier = notifier

    def run_saturday(self) -> list[Advisory]:
        run_id = f"sat-{uuid.uuid4().hex[:8]}"
        self.state_store.acquire_lock(run_id)
        try:
            state = self.state_store.load()
            advisories: list[Advisory] = []
            failed: list[tuple[str, str]] = []

            # Universe from config/universe.json (operator-editable). A load
            # problem falls back to the built-in default and is REPORTED, not
            # swallowed.
            from .universe_loader import load_universe
            universe, warning = load_universe(getattr(self.state_store, "repo", "."))
            if warning:
                failed.append(("universe.json", warning))

            for ticker in universe:
                try:
                    candles = self.market.fetch_weekly_candles(ticker)
                    news = self.market.fetch_news(ticker)
                    regime = self.sentiment.regime_tag(news, smoothed=True)
                    advisory = strategy.generate_saturday_advisory(
                        ticker=ticker, candles=candles,
                        current_price=candles[-1].close, regime=regime,
                        state=state, config=self.config,
                    )
                    advisories.append(advisory)
                except Exception as exc:  # noqa: BLE001 — isolate per ticker
                    # A bad ticker (typo, delisting, vendor outage) must not
                    # kill the run: skip it, keep the reason, tell the human.
                    failed.append((ticker, f"{type(exc).__name__}: {exc}"))

            if not advisories and failed:
                # Everything failed — that's a real run failure, not a skip.
                raise RuntimeError(
                    f"All {len(failed)} tickers failed; first: {failed[0][1]}"
                )

            state.active_advisories = advisories
            state.last_saturday_run = datetime.now(timezone.utc).isoformat()
            self._commit(state, run_id)
            self.notifier.send_advisories(advisories, failed_tickers=failed or None)
            self.notifier.send_heartbeat(run_id)
            return advisories
        except Exception as exc:  # noqa: BLE001 — top-level guard by design
            self._abort(run_id, exc)
            raise
        finally:
            self.state_store.release_lock()

    def run_monday(self) -> list[Advisory]:
        run_id = f"mon-{uuid.uuid4().hex[:8]}"
        self.state_store.acquire_lock(run_id)
        try:
            state = self.state_store.load()
            deltas: list[Advisory] = []
            failed: list[tuple[str, str]] = []
            for sat_advisory in state.active_advisories:
                if sat_advisory.action.value == "hold":
                    continue
                try:
                    snapshot = self.market.fetch_price_snapshot(sat_advisory.ticker)
                    news = self.market.fetch_news(sat_advisory.ticker)
                    fresh_regime = self.sentiment.regime_tag(news, smoothed=False)
                    result = strategy.reconcile_monday(
                        snapshot=snapshot, saturday_advisory=sat_advisory,
                        fresh_regime=fresh_regime, config=self.config,
                    )
                    deltas.append(result)
                except Exception as exc:  # noqa: BLE001 — isolate per ticker
                    failed.append((sat_advisory.ticker, f"{type(exc).__name__}: {exc}"))

            state.last_monday_run = datetime.now(timezone.utc).isoformat()
            self._commit(state, run_id)
            changed = [d for d in deltas if d.action.value != "hold"]
            self.notifier.send_advisories(changed, failed_tickers=failed or None)
            self.notifier.send_heartbeat(run_id)
            return changed
        except Exception as exc:  # noqa: BLE001
            self._abort(run_id, exc)
            raise
        finally:
            self.state_store.release_lock()

    def _commit(self, state: PortfolioState, run_id: str) -> None:
        history = getattr(self.sentiment, "history", None)
        commit = getattr(self.state_store, "commit")
        try:
            commit(state, run_id, sentiment_history=history)
        except TypeError:
            commit(state, run_id)

    def _abort(self, run_id: str, exc: Exception) -> None:
        try:
            self.notifier.send_failure(f"{type(exc).__name__}: {exc}", run_id)
        except Exception:
            pass
