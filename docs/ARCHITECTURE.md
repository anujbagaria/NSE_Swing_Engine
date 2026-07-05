# Architecture & audit reference — NSE swing engine v2

This document is written for **third-party AI audits before production deployment**.
It describes the system precisely enough that another agent can verify the risk
controls, data-flow integrity, and separation of concerns without running the code.
All UML uses Mermaid, which GitHub renders natively.

## 1. System context

An advise-only engine. It never holds broker credentials; a human executes every
order on Zerodha Kite. Two scheduled GitHub Actions runs per week produce Discord
advisories.

```mermaid
flowchart LR
    GHA["GitHub Actions<br/>(cron scheduler)"] -->|invokes| ENG["Swing engine<br/>(Python)"]
    YF["yfinance<br/>weekly OHLCV + news"] -->|reads| ENG
    GEM["Gemini<br/>sentiment regime"] -->|reads| ENG
    ENG -->|commits| GIT["git state<br/>(JSON, versioned)"]
    ENG -->|advisories| DIS["Discord<br/>(mobile alerts)"]
    DIS -->|human reads| USER["Operator"]
    USER -->|places GTT manually| KITE["Zerodha Kite"]
```

Trust boundary: everything left of the operator is automated and holds NO money
or broker access. The operator is the only actor who can move capital.

## 2. Layered architecture (ports & adapters)

The domain layer is pure (no I/O, no vendor imports) and is the only place trading
math lives. Adapters quarantine all I/O. This is what makes the engine auditable:
the risk logic can be read and unit-tested in isolation.

```mermaid
flowchart TB
    subgraph APP["app layer (orchestration + side effects)"]
        MAIN["main.py<br/>build adapters from env"]
        ORCH["orchestrator.py<br/>run_saturday / run_monday"]
        BT["backtest.py<br/>walk-forward harness"]
    end
    subgraph DOMAIN["domain layer (PURE — no I/O)"]
        STRAT["strategy.py<br/>Donchian, ATR, sizing, trail"]
        CFG["config.py<br/>all parameters"]
        TYPES["types.py<br/>value objects"]
        PORTS["ports.py<br/>interfaces"]
    end
    subgraph ADAPTERS["adapters (all I/O quarantined)"]
        MKT["market_data_yf"]
        SENT["sentiment_gemini"]
        STATE["state_git"]
        NOTIF["notifier_discord"]
    end
    MAIN --> ORCH
    ORCH --> STRAT
    ORCH -->|depends on| PORTS
    BT --> STRAT
    STRAT --> CFG
    STRAT --> TYPES
    MKT -.implements.-> PORTS
    SENT -.implements.-> PORTS
    STATE -.implements.-> PORTS
    NOTIF -.implements.-> PORTS
```

**Audit checkpoint:** `strategy.py` and `config.py` import only `statistics`,
`datetime`, and sibling domain modules. If either imports pandas, yfinance,
google, or os, the purity guarantee is broken. Verify with:
`grep -E "import (pandas|yfinance|google|os|requests)" src/swing_engine/domain/*.py`
(should return nothing).

## 3. Domain class model

```mermaid
classDiagram
    class Candle {
        +date week_start
        +float open, high, low, close
        +float volume
    }
    class Channel {
        +float upper "20wk high"
        +float lower "10wk low"
        +float atr
        +date as_of_week
    }
    class Advisory {
        +str ticker
        +Side side
        +Action action
        +float trigger_price
        +float stop_price
        +float target_price "0 = trailing"
        +int quantity "1pct-risk sized"
        +Regime regime
        +float atr
    }
    class Position {
        +str ticker
        +float entry_price
        +int quantity
        +float stop_price
        +float highest_close "for trailing"
    }
    class PortfolioState {
        +float capital
        +list~Advisory~ active_advisories
        +list~Position~ open_positions
    }
    PortfolioState "1" o-- "*" Advisory
    PortfolioState "1" o-- "*" Position
    Advisory --> Channel : derived from
```

## 4. Saturday run sequence

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant O as Orchestrator
    participant St as StatePort
    participant M as MarketData
    participant Se as Sentiment
    participant Str as strategy (pure)
    participant N as Notifier
    S->>O: run_saturday()
    O->>St: acquire_lock(run_id)
    O->>St: load() -> state
    loop each ticker in universe
        O->>M: fetch_weekly_candles + news
        O->>Se: regime_tag(news, smoothed=True)
        O->>Str: generate_saturday_advisory(...)
        Str-->>O: Advisory (entry+stop+qty OR trail update)
    end
    O->>St: commit(state, run_id)  %% atomic
    O->>N: send_advisories + heartbeat
    O->>St: release_lock()
    Note over O,N: any exception -> abort WITHOUT commit,<br/>send_failure, re-raise. Silence is never "no trades".
```

## 5. The risk framework (the core of the audit)

Five independent controls, each verifiable in isolation:

```mermaid
flowchart TB
    E["Entry breakout confirmed"] --> SZ["1. Position sizing<br/>qty = 1pct capital / (entry-stop)"]
    SZ --> HS["2. Hard stop<br/>entry - 3*ATR, ALWAYS set"]
    HS --> TR["3. Adaptive trail<br/>3.0 / 2.5 / 2.0 ATR by trend state"]
    TR --> RATCHET["4. Ratchet<br/>stop only rises, never falls"]
    RATCHET --> CAP["5. Concurrency cap<br/>max 4 positions, GTT budget < 50"]
```

| # | Control | Where | Failure it prevents |
|---|---|---|---|
| 1 | 1%-risk sizing | `position_size()` | Oversized position blows up account on one loss |
| 2 | ATR hard stop | `hard_stop_price()` | Naked position riding a crash to zero (v1's fatal flaw) |
| 3 | Adaptive trail | `choose_trail_multiple()` | Giving back all open profit / exiting too early |
| 4 | Upward-only ratchet | `generate_saturday_advisory` | Loosening a stop under pressure (emotion) |
| 5 | Concurrency cap | `max_open_positions` | Correlated cluster of positions = hidden leverage |

**Audit checkpoint:** every entry Advisory with `action == PLACE_GTT` must have
`stop_price > 0`, `quantity > 0`, and `stop_price < trigger_price`. There is no
code path that emits a sized entry without a stop.

## 6. Technology-lens risk controls

| Risk | Guard |
|---|---|
| Silent API failure | Top-level try/except aborts without commit, fires Discord `send_failure`, re-raises (non-zero exit fails the Action visibly). |
| Missed run (dead engine) | `send_heartbeat` on success arms a dead-man's switch; absence of the green ping is the alarm. |
| Look-ahead bias | Backtest computes channel from `candles[:t]`, evaluates on `candle[t]`. Test `test_backtest_has_no_lookahead` asserts every exit week > entry week. |
| Still-forming candle | yfinance adapter drops any bar whose week hasn't closed (Fri + buffer). |
| Concurrent runs | git lockfile lease with 30-min TTL; `concurrency` group in the workflow. |
| Secret leakage | Secrets only via env in `main.py`; `.gitignore` blocks `.env`; GitHub Actions secrets never printed. |
| State corruption | Atomic temp-file replace + single git commit = all-or-nothing; commit log is a free audit trail. |
| Stale GTT after corporate action | Kite auto-cancels on ex-date; Saturday run detects the missing position and re-advises. |

## 7. Overfitting / data-snooping defenses

- Parameters are **fixed priors from literature** (Chandelier 3.0 ATR / 22-period,
  Donchian 20), not grid-searched over the ~156 weekly bars available.
- Sentiment is a **coarse 3-state tag**, not a calibrated numeric multiplier — an
  LLM's fine-grained score is not trustworthy enough to size trades on.
- Backtest reports **buy-and-hold benchmark** alongside strategy return, so
  under-performance can't hide behind a positive absolute number.
- **Known limitation stated plainly:** the bundled synthetic backtests use
  Brownian motion (zero autocorrelation) and therefore validate risk *mechanics*
  only — they cannot demonstrate a trend edge. Real-data walk-forward via
  `run_backtest` across a full bull+bear cycle is mandatory before live use.

## 8. What an auditing agent should check

1. Domain purity (grep in section 2).
2. Every sized entry carries a stop (section 5).
3. No look-ahead (run `pytest -k lookahead`).
4. Secrets never committed (`git log -p | grep -i "api_key\|webhook"` returns nothing).
5. Failure path commits nothing (read `orchestrator._abort`).
6. Real-data backtest was actually run and reviewed (not just synthetic).
7. Parameters in `config.py` match the values claimed here (no silent drift).
