"""Git implementation of StatePort.

State is a single JSON file committed to a (private) repo. One file, one commit
= atomic and versioned (the commit log is a free audit trail). A lockfile with
run-id + timestamp is a lease that stops a manual re-run colliding with a
scheduled one; a stale lease (older than TTL) is reclaimable.

REVISION: serializes the richer Advisory (stop/target/qty/atr) and the new
Position objects (with trailing-stop state).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..domain.types import Advisory, PortfolioState, Position

LOCK_TTL = timedelta(minutes=30)


class GitState:
    def __init__(self, repo_dir: str, state_filename: str = "state/state.json",
                 sentiment_filename: str = "state/sentiment_history.json"):
        self.repo = Path(repo_dir)
        self.state_path = self.repo / state_filename
        self.sentiment_path = self.repo / sentiment_filename
        self.lock_path = self.repo / "state" / ".lock"

    def acquire_lock(self, run_id: str) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                meta = json.loads(self.lock_path.read_text())
                ts = datetime.fromisoformat(meta["ts"])
                if datetime.now(timezone.utc) - ts < LOCK_TTL:
                    raise RuntimeError(f"State locked by run {meta['run_id']} at {meta['ts']}")
            except (json.JSONDecodeError, KeyError):
                pass
        self.lock_path.write_text(json.dumps(
            {"run_id": run_id, "ts": datetime.now(timezone.utc).isoformat()}
        ))

    def release_lock(self) -> None:
        if self.lock_path.exists():
            self.lock_path.unlink()

    def load(self) -> PortfolioState:
        if not self.state_path.exists():
            return PortfolioState()
        raw = json.loads(self.state_path.read_text())
        advisories = [self._advisory_from_dict(a) for a in raw.get("active_advisories", [])]
        positions = [Position(**p) for p in raw.get("open_positions", [])]
        return PortfolioState(
            schema_version=raw.get("schema_version", 2),
            capital=raw.get("capital", 100000.0),
            last_saturday_run=raw.get("last_saturday_run"),
            last_monday_run=raw.get("last_monday_run"),
            active_advisories=advisories,
            open_positions=positions,
        )

    def load_sentiment_history(self) -> list[float]:
        if not self.sentiment_path.exists():
            return []
        return json.loads(self.sentiment_path.read_text())

    def commit(self, state: PortfolioState, run_id: str,
               sentiment_history: list[float] | None = None) -> str:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": state.schema_version,
            "capital": state.capital,
            "last_saturday_run": state.last_saturday_run,
            "last_monday_run": state.last_monday_run,
            "open_positions": [asdict(p) for p in state.open_positions],
            "active_advisories": [self._advisory_to_dict(a) for a in state.active_advisories],
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(self.state_path)
        if sentiment_history is not None:
            self.sentiment_path.write_text(json.dumps(sentiment_history))
        return self._git_commit(run_id)

    def _git_commit(self, run_id: str) -> str:
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "swing-engine", "GIT_AUTHOR_EMAIL": "bot@local",
               "GIT_COMMITTER_NAME": "swing-engine", "GIT_COMMITTER_EMAIL": "bot@local"}
        subprocess.run(["git", "-C", str(self.repo), "add", "state/"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "--allow-empty",
             "-m", f"state: run {run_id}"], check=True, env=env)
        out = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, env=env)
        return out.stdout.strip()

    @staticmethod
    def _advisory_to_dict(a: Advisory) -> dict:
        d = asdict(a)
        d["side"] = a.side.value
        d["action"] = a.action.value
        d["regime"] = a.regime.value
        d["channel_as_of"] = a.channel_as_of.isoformat()
        d["created_at"] = a.created_at.isoformat()
        return d

    @staticmethod
    def _advisory_from_dict(d: dict) -> Advisory:
        from ..domain.types import Action, Regime, Side
        return Advisory(
            ticker=d["ticker"], side=Side(d["side"]), action=Action(d["action"]),
            trigger_price=d["trigger_price"], limit_price=d["limit_price"],
            stop_price=d.get("stop_price", 0.0), target_price=d.get("target_price", 0.0),
            quantity=d.get("quantity", 0), regime=Regime(d["regime"]),
            atr=d.get("atr", 0.0),
            channel_as_of=date.fromisoformat(d["channel_as_of"]),
            created_at=datetime.fromisoformat(d["created_at"]),
            rationale=d.get("rationale", ""),
        )
