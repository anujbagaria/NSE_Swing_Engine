"""Loads the ETF universe from config/universe.json.

Lives in the app layer (it does file I/O; the domain stays pure). The operator
edits the JSON to add/remove tickers — no code change needed. Rules:

  - Missing or malformed file  -> fall back to DEFAULT_CONFIG.universe, and
    return a warning string so the caller can surface it (never silent).
  - Entries are validated lightly (non-empty strings), deduplicated in order.
  - Ticker EXISTENCE is not checked here — that requires a network call, and
    per-ticker fetch failures are handled (skip + notify) by the orchestrator.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..domain.config import DEFAULT_CONFIG

UNIVERSE_FILE = Path("config") / "universe.json"


def _candidate_paths(repo_dir: str | Path) -> list[Path]:
    """Places to look for config/universe.json, in priority order:
    the explicit repo_dir, the current working directory, then up to three
    parent directories of the CWD (so running from a subfolder still works).
    """
    candidates: list[Path] = []
    roots = [Path(repo_dir).resolve(), Path.cwd().resolve()]
    roots += list(Path.cwd().resolve().parents[:3])
    for root in roots:
        p = root / UNIVERSE_FILE
        if p not in candidates:
            candidates.append(p)
    return candidates


def load_universe(repo_dir: str | Path = ".") -> tuple[list[str], str | None]:
    """Returns (tickers, warning). warning is None when the file loaded cleanly."""
    fallback = list(DEFAULT_CONFIG.universe)
    candidates = _candidate_paths(repo_dir)
    path = next((p for p in candidates if p.exists()), None)

    if path is None:
        tried = "; ".join(str(p) for p in candidates[:4])
        return fallback, (
            f"universe file not found (tried: {tried}); using built-in default "
            f"({len(fallback)} tickers)"
        )
    try:
        raw = json.loads(path.read_text())
        entries = raw.get("universe", [])
        tickers: list[str] = []
        for e in entries:
            if isinstance(e, str) and e.strip():
                t = e.strip()
                if t not in tickers:
                    tickers.append(t)
        if len(tickers) < 2:
            return fallback, (
                f"universe file has {len(tickers)} valid ticker(s); rotation "
                f"needs >= 2 — using built-in default"
            )
        return tickers, None
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        return fallback, (
            f"universe file malformed ({type(exc).__name__}: {exc}); "
            f"using built-in default"
        )
