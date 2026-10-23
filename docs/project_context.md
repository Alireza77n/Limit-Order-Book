# Project Context — branch `MM_algo3`

Stable facts for anyone (person or AI session) picking up this branch. History is in
`changelog.md`, accepted decisions in `decisions.md`; the full guides are `PROJECT_GUIDE.md`,
`README.md` and `docs/algorithms/MM_algo3.md`.

## What this is

A market-making bot for Nobitex (BTC/IRT and USDT/IRT). Every round it reads the order book,
lets the chosen algorithm pick a bid and an ask, sizes them by inventory and rests them as limit
orders. It is judged on a demo (paper trading, real market data) before real money. **This branch
runs and documents MM_algo3**: a market maker with a mean-reverting price signal and quotes from
stochastic control (Avellaneda–Stoikov / Cartea–Jaimungal), code key `algo4`
(`market_maker/strategy/algo4.py`). The other two algorithms stay selectable for comparison and
are documented on their own branches.

## Stack

- Python 3.12 locally (`Server_project\.venv-mm`), 3.14 in the server venv.
- pandas, numpy, requests (Nobitex REST), Flask (admin panel), Dash (dashboard), SQLite result
  stores, Docker Compose (demo stack), systemd (live stack).
- Tests: pytest, `market_maker/tests/` (about 1,040; on Windows 13 file-lock tests fail by design).

## Architecture

`run.py` (worker per pair and mode; a demo worker is a `LaneRunner` with one engine thread per
enabled algorithm) → `engine.py::SimpleMarketMaker` (one round: book → strategy → sizing → orders;
demo and live loops; the engine rules of 20 October 2026) → `strategy/` (`catalog.py`: key `algo4`
= MM_algo3, `algo3` = MM_algo2, `algo1` = MM_algo1v2; `common.py` = shared arithmetic incl. the
requote band; `algo4.py` keeps its own mid window and clock and reads the fill table through
`fill_model.py::FillModel.intensity` for κ) → `exchange/` (`nobitex.py` REST, `paper.py` demo
ledger with the price-touch fill rule).

Control plane: `demo_config.py` (per-pair overlays `runtime/state/demo_config_<pair>.json`, hot
reload at round boundaries, runtime snapshots per lane) ← `control_queue.py` ← `admin_app.py`
(Flask, port 8653) and `deploy/control_agent.py` (host agent that executes queued actions).
`dashboard.py` (Dash, port 8652) reads the result stores. Live stack: `forward_config.py`,
`deploy/forward_control_agent.py`.

## Where things run

- Server `37.32.31.162` (4 GB RAM since 22 October 2026, 2 CPUs, no swap): demo stack
  `/opt/limit-order-book` (runtime in `runtime/`), live stack `/opt/market-maker-forward`. Deploy
  with `SKIP_TESTS=1 TESTED_LOCALLY="…" DEPLOY_BRANCH=MM_algo3 /root/deploy_dashboard.sh <commit> <sha256>`
  after running the suite on the PC (the box cannot run it); the deploy never recreates the demo
  worker containers — recreate them by hand after worker code changes.
- GitHub `Alireza77n/Limit-Order-Book`: `MM_algo3` (this branch; deployed since 23 October 2026),
  `MM_algo2` (order-book imbalance and microprice, with its own documentation), `MM_algo1v2`
  (Algorithm 1 version 2 alone, with its own documentation), `base-algorithms` (standalone
  algorithm files), `MM_algo1v1` (old).

## Current goal (October 2026)

Run the three algorithms on the demo at the same time (lanes) under the rules of 20 October 2026 —
one order per side (requote band 0.03 %), 3-minute orders, target 0.02 %, trend pause 0.3 % over
10 minutes — and compare them on the same market; for MM_algo3, decide from the demo whether its
forecast (no measured skill on the recorded data) stays on; prepare the fee-discount proposal for
the exchange (`Report_investor/`). Live workers: the operator decides when they run.

## Important constraints

- Never start or stop a live (real-money) worker from automation.
- Each branch documents only its own algorithm; algorithm names are the branch names
  (`MM_algo3`, `MM_algo2`, `MM_algo1v2`); no version-to-version comparisons in the docs.
- Every number in the docs comes from a script or a recorded file, never from a hand calculation
  (`docs/algorithms/data/make_mm_algo3_doc_data.py`).
- Settings changed on the admin live in the overlays, not in `config.py` (profile defaults).
- `docs/` is gitignored: `git add -f` for new documentation files. Commit as Alireza.
