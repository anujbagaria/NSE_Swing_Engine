"""Tests for syncing state from Zerodha broker CSV exports."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swing_engine.app import positions as P


HOLDINGS_CSV = """Instrument,Qty.,Avg. cost,LTP,Invested,Cur. val,P&L,Net chg.,Day chg.
MODEFENCE,743,99.03,102.41,73579.48,76090.63,2511.15,3.41,0.49
SBISILVER,407,245.64,209.91,99977.07,85433.37,-14543.7,-14.55,-1.02
SETFGOLD,3893,127.87,119.41,497813.5,464863.13,-32950.37,-6.62,-0.66
SETFNIF50,1575,256.21,261.43,403523.79,411752.25,8228.46,2.04,1.05
SILVERBEES,300,216.06,204.92,64818,61476,-3342,-5.16,-0.93
"""


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "universe.json").write_text(json.dumps({
        "universe": ["MID150BEES.NS", "BANKBEES.NS", "ITBEES.NS",
                     "GOLDBEES.NS", "SILVERBEES.NS"]
    }))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps({
        "schema_version": 2, "capital": 100000.0, "last_saturday_run": None,
        "last_monday_run": None, "open_positions": [], "active_advisories": []
    }))
    (tmp_path / "holdings.csv").write_text(HOLDINGS_CSV)
    # deterministic stop, no network
    monkeypatch.setattr(P, "_compute_stop", lambda t, e: round(e * 0.9, 2))


def test_holdings_parsed_and_only_exact_match_tracked(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    rc = P._cmd_sync(["holdings.csv"])
    assert rc == 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    pos = state["open_positions"]
    assert len(pos) == 1                       # only SILVERBEES matched
    assert pos[0]["ticker"] == "SILVERBEES.NS"
    assert pos[0]["quantity"] == 300           # Zerodha pre-aggregated
    assert pos[0]["entry_price"] == 216.06
    assert pos[0]["stop_price"] == round(216.06 * 0.9, 2)


def test_non_universe_holdings_are_ignored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    P._cmd_sync(["holdings.csv"])
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    tracked = {p["ticker"] for p in state["open_positions"]}
    # SETFGOLD etc are different instruments -> must NOT be tracked as GOLDBEES
    assert "GOLDBEES.NS" not in tracked
    assert tracked == {"SILVERBEES.NS"}


def test_stop_below_entry(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    P._cmd_sync(["holdings.csv"])
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    p = state["open_positions"][0]
    assert p["stop_price"] < p["entry_price"]   # a stop must be below entry


def test_missing_holdings_file_errors(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert P._cmd_sync(["nope.csv"]) == 1


def test_capital_command(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert P._cmd_capital(["250000"]) == 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["capital"] == 250000.0
