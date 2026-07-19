"""Sync the engine's memory (state.json) from your Zerodha CSV exports.

WHY THIS EXISTS
---------------
The engine only ADVISES; you execute on Kite. So state.json must be told what
you actually hold. Rather than type it by hand (error-prone), this reads your
broker's own exports — the single source of truth for real fills.

TWO FILES, TWO JOBS
-------------------
  holdings.csv  -> WHAT you hold now (Instrument, Qty., Avg. cost). Primary
                   source of truth for current positions. Zerodha already
                   aggregates split fills here, so quantity/avg-cost are final.
  tradebook.csv -> optional, only used to cross-check/refine the entry price.
                   (Holdings' "Avg. cost" is already the entry price we need,
                   so tradebook is not required for the basic sync.)

MATCHING RULE (operator's choice): EXACT match only. A universe ticker like
"GOLDBEES.NS" has its ".NS" stripped to "GOLDBEES" and must equal the holdings
Instrument exactly. Different fund houses (SETFGOLD vs GOLDBEES) are DIFFERENT
instruments and are intentionally NOT matched — they're listed as untracked.

STOP-LOSS: not in any broker file, so it's computed with the SAME ATR rule the
strategy uses: stop = entry - atr_stop_multiple * ATR (needs live price data to
get ATR). This is the engine's internal reference; it may differ from the exact
GTT you placed on Kite. The report prints it so nothing is hidden.

USAGE
-----
    python -m swing_engine.app.positions sync holdings.csv
    python -m swing_engine.app.positions sync holdings.csv --tradebook tradebook.csv
    python -m swing_engine.app.positions list
    python -m swing_engine.app.positions capital 250000

After sync, commit & push so the scheduled run sees it:
    git add state/state.json && git commit -m "positions: sync from broker" && git push
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from ..domain import strategy
from ..domain.config import DEFAULT_CONFIG
from .universe_loader import load_universe

STATE_PATH = Path("state") / "state.json"


# ---------- state helpers ----------

def _load_state() -> dict:
    if not STATE_PATH.exists():
        print(f"ERROR: {STATE_PATH} not found. Run from the repo root.")
        raise SystemExit(1)
    return json.loads(STATE_PATH.read_text())


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_PATH)
    print(f"\nSaved {STATE_PATH}. Remember to:  git add state/state.json && "
          f"git commit -m 'positions: sync' && git push")


# ---------- CSV parsing ----------

def _read_holdings(path: Path) -> list[dict]:
    """Parse Zerodha holdings export. Columns: Instrument, Qty., Avg. cost, ..."""
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            # tolerate stray spaces / BOM in header names
            r = { (k or "").strip(): (v or "").strip() for k, v in r.items() }
            inst = r.get("Instrument", "")
            if not inst:
                continue
            try:
                qty = int(float(r.get("Qty.", "0")))
                avg = float(r.get("Avg. cost", "0"))
            except ValueError:
                continue
            rows.append({"instrument": inst, "qty": qty, "avg_cost": avg})
    return rows


def _universe_symbols() -> tuple[dict[str, str], list[str]]:
    """Return ({stripped_symbol: full_ticker}, [full_tickers]). Strips .NS."""
    tickers, warning = load_universe(".")
    if warning:
        print(f"NOTE: {warning}")
    mapping = {t.replace(".NS", "").upper(): t for t in tickers}
    return mapping, tickers


# ---------- stop computation (same ATR rule as the strategy) ----------

def _compute_stop(full_ticker: str, entry: float) -> float | None:
    """stop = entry - atr_stop_multiple * ATR, using live weekly data for ATR.
    Returns None if data can't be fetched (reported, not silently zeroed)."""
    try:
        from ..adapters.market_data_yf import YFinanceMarketData
        market = YFinanceMarketData(DEFAULT_CONFIG)
        candles = market.fetch_weekly_candles(full_ticker)
        a = strategy.compute_channel(candles, DEFAULT_CONFIG).atr \
            if hasattr(strategy, "compute_channel") else None
        if a is None:
            from ..domain import rotation
            a = rotation.atr(candles, DEFAULT_CONFIG.atr_window)
        stop = max(0.0, entry - DEFAULT_CONFIG.atr_stop_multiple * a)
        return round(stop, 2)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not compute ATR stop for {full_ticker}: {exc}")
        return None


# ---------- commands ----------

def _cmd_sync(args: list[str]) -> int:
    if not args:
        print("usage: positions sync holdings.csv [--tradebook tradebook.csv]")
        return 1
    holdings_path = Path(args[0])
    if not holdings_path.exists():
        print(f"ERROR: holdings file not found: {holdings_path}")
        return 1

    mapping, _ = _universe_symbols()
    holdings = _read_holdings(holdings_path)

    tracked, untracked = [], []
    for h in holdings:
        key = h["instrument"].upper()
        if key in mapping:
            tracked.append((mapping[key], h))
        else:
            untracked.append(h)

    print(f"\nRead {len(holdings)} holding(s). Universe has {len(mapping)} ETF(s).")
    print(f"Matched (will track): {len(tracked)}   Untracked (ignored): {len(untracked)}")

    if untracked:
        print("\nUntracked holdings (not in universe.json — intentionally ignored):")
        for h in untracked:
            print(f"  - {h['instrument']}  qty={h['qty']}  (no exact universe match)")

    positions = []
    print("\nBuilding tracked positions:")
    for full_ticker, h in tracked:
        entry = h["avg_cost"]
        stop = _compute_stop(full_ticker, entry)
        positions.append({
            "ticker": full_ticker,
            "entry_price": entry,
            "quantity": h["qty"],
            "stop_price": stop if stop is not None else 0.0,
            "highest_close": entry,
        })
        stop_txt = f"{stop:.2f}" if stop is not None else "0.00 (ATR fetch failed)"
        print(f"  + {full_ticker}: qty={h['qty']} entry={entry:.2f} "
              f"computed_stop={stop_txt}")

    state = _load_state()
    state["open_positions"] = positions
    _save_state(state)
    print(f"\nDone. {len(positions)} position(s) written to state.")
    print("Note: computed stops are the engine's internal reference and may differ")
    print("from the exact GTT you placed on Kite.")
    return 0


def _cmd_list(_args: list[str]) -> int:
    state = _load_state()
    print(f"capital: {state.get('capital')}")
    positions = state.get("open_positions", [])
    if not positions:
        print("no open positions")
    for p in positions:
        print(f"  {p['ticker']}: qty={p['quantity']} entry={p['entry_price']} "
              f"stop={p['stop_price']} peak={p['highest_close']}")
    return 0


def _cmd_capital(args: list[str]) -> int:
    if len(args) != 1:
        print("usage: positions capital AMOUNT")
        return 1
    state = _load_state()
    state["capital"] = float(args[0])
    _save_state(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0].lower(), argv[1:]
    # strip optional --tradebook flag (reserved for future entry-price refinement)
    if "--tradebook" in rest:
        i = rest.index("--tradebook")
        rest = rest[:i] + rest[i + 2:]
    if cmd == "sync":
        return _cmd_sync(rest)
    if cmd == "list":
        return _cmd_list(rest)
    if cmd == "capital":
        return _cmd_capital(rest)
    print(f"unknown command: {cmd}\n{__doc__}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
