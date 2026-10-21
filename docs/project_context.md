# Project Context — branch `MM_algo2`

Stable facts for anyone (person or AI session) picking up this branch. History is in
`changelog.md`, accepted decisions in `decisions.md`; the full guides are `PROJECT_GUIDE.md`,
`README.md` and `docs/guide/MARKET_MAKER_EXPLAINED.md`.

## What this is

A market-making bot for Nobitex (BTC/IRT and USDT/IRT). Every round it reads the order book,
lets the chosen algorithm pick a bid and an ask, sizes them by inventory and rests them as limit
orders. It is judged on a demo (paper trading, real market data) before real money.

## Stack

- Python 3.12 locally (`Server_project\.venv-mm`), 3.14 in the server venv.
- pandas, numpy, requests (Nobitex REST), Flask (admin panel), Dash (dashboard), SQLite result
  stores, Docker Compose (demo stack), systemd (live stack).
- Tests: pytest, `market_maker/tests/` (about 1,000; on Windows 13 file-lock tests fail by design).

## Architecture

`run.py` (worker per pair and mode; a demo worker is a `LaneRunner` with one engine thread per
enabled algorithm) → `engine.py::SimpleMarketMaker` (one round: book → strategy → sizing → orders;
demo and live loops) → `strategy/` (`catalog.py`: key `algo1` = MM_algo1v2, key `algo3` = MM_algo2;
`common.py` = shared arithmetic incl. the requote band) → `exchange/` (`nobitex.py` REST,
`paper.py` demo ledger with the price-touch fill rule).

Control plane: `demo_config.py` (per-pair overlays `runtime/state/demo_config_<pair>.json`, hot
reload at round boundaries, runtime snapshots per lane) ← `control_queue.py` ← `admin_app.py`
(Flask, port 8653) and `deploy/control_agent.py` (host agent that executes queued actions).
`dashboard.py` (Dash, port 8652) reads the result stores. Live stack: `forward_config.py`,
`deploy/forward_control_agent.py`.

## Where things run

- Server `37.32.31.162` (2 GB RAM, 2 CPUs, no swap): demo stack `/opt/limit-order-book` (runtime
  in `runtime/`), live stack `/opt/market-maker-forward`. Deploy with
  `/root/deploy_dashboard.sh <commit> <sha256>` (full test suite first; it never recreates the demo
  worker containers — recreate them by hand after worker code changes).
- GitHub `Alireza77n/Limit-Order-Book`: `MM_algo2` (this branch; default; deployed), `MM_algo1v2`
  (Algorithm 1 version 2 alone, with its own documentation), `base-algorithms` (standalone
  algorithm files), `MM_algo1v1` (old).

## Current goal (October 2026)

Run both algorithms on the demo at the same time (lanes) under the rules of 20 October 2026 —
one order per side (requote band 0.03 %), 3-minute orders, target 0.20 %, trend pause 0.3 % over
10 minutes — measure fills, and prepare the fee-discount proposal for the exchange
(`Report_investor/`). Live workers are stopped; the operator decides when they run.

## Important constraints

- Never start or stop a live (real-money) worker from automation.
- Each branch documents only its own algorithm; algorithm names are the branch names
  (`MM_algo2`, `MM_algo1v2`); no version-to-version comparisons in the docs.
- Every number in the docs comes from a script or a recorded file, never from a hand calculation.
- Settings changed on the admin live in the overlays, not in `config.py` (profile defaults).
- `docs/` is gitignored: `git add -f` for new documentation files. Commit as Alireza.
