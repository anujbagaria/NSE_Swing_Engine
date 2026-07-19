"""Entrypoint the scheduler calls: `python -m swing_engine.app.main saturday`.

The only place that reads the environment and constructs concrete adapters.
Fails fast and loud if a required secret is missing.
"""
from __future__ import annotations

import os
import sys

from ..adapters.market_data_yf import YFinanceMarketData
from ..adapters.notifier_composite import CompositeNotifier
from ..adapters.notifier_discord import DiscordNotifier
from ..adapters.notifier_email import EmailNotifier
from ..adapters.sentiment_gemini import GeminiSentiment
from ..adapters.state_git import GitState
from ..domain.config import DEFAULT_CONFIG
from .orchestrator import Orchestrator

REQUIRED_ENV = ["GEMINI_API_KEY", "DISCORD_WEBHOOK"]


def _check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")


def build_orchestrator(repo_dir: str) -> Orchestrator:
    config = DEFAULT_CONFIG
    state = GitState(repo_dir)
    sentiment = GeminiSentiment(config, history=state.load_sentiment_history())

    # Discord is the primary channel; email is an independent fallback that is
    # only added if it's configured. This keeps deployment flexible: run
    # Discord-only until email secrets are set, then it auto-activates.
    channels = [DiscordNotifier()]
    email = EmailNotifier()
    if email.configured:
        channels.append(email)

    return Orchestrator(
        config=config,
        market=YFinanceMarketData(config),
        sentiment=sentiment,
        state_store=state,
        notifier=CompositeNotifier(channels),
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0] if argv else ""
    if mode not in {"saturday", "monday"}:
        raise SystemExit("Usage: python -m swing_engine.app.main [saturday|monday]")
    _check_env()
    repo_dir = os.environ.get("STATE_REPO_DIR", ".")
    orch = build_orchestrator(repo_dir)
    if mode == "saturday":
        orch.run_saturday()
    else:
        orch.run_monday()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
