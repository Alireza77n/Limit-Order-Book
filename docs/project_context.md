# Project Context — branch `MM_algo1v2`

Stable facts for anyone (person or AI session) picking up this branch. History is in
`changelog.md`, accepted decisions in `decisions.md`; the full guides are `PROJECT_GUIDE.md`,
`README.md`, `docs/algorithms/ALGORITHM_1_V2.md` and `docs/guide/MARKET_MAKER_EXPLAINED.md`.

## What this is

A market-making bot for Nobitex (BTC/IRT and USDT/IRT) running **Algorithm 1 version 2**
(`MM_algo1v2`): candidate prices on a distance grid, measured fill probabilities, expected-value
selection, one-sided quotes at the inventory band edges, hysteresis. This branch holds that
algorithm alone; the second algorithm (`MM_algo2`) and the per-pair selection live on branch
`MM_algo2`, which is the branch deployed on the server.

## Stack

- Python 3.12 locally (`Server_project\.venv-mm`), 3.14 in the server venv.
- pandas, numpy, requests (Nobitex REST), Flask (admin panel), Dash (dashboard), SQLite result
  stores, Docker Compose (demo stack), systemd (live stack).
- Tests: pytest, `market_maker/tests/` (about 900; on Windows 13 file-lock tests fail by design).

## Architecture

`run.py` (worker per pair and mode) → `engine.py::SimpleMarketMaker` (one round: book →
strategy → sizing → orders; demo and live loops; the engine rules of 20 October 2026) →
`strategy/algo1.py::Algo1Strategy` (+ `fill_model.py::FillModel`, table from
`tools/build_fill_table.py`) → `exchange/` (`nobitex.py` REST, `paper.py` demo ledger with the
price-touch fill rule). Control plane: `demo_config.py` (overlays, hot reload, runtime snapshots)
← `control_queue.py` ← `admin_app.py` (Flask) and `deploy/control_agent.py`. `dashboard.py`
(Dash) reads the result stores. Live stack: `forward_config.py`, `deploy/forward_control_agent.py`.

## Where things run

- Not deployed on its own since 16 October 2026: the server (`37.32.31.162`) runs branch
  `MM_algo2`, where this algorithm is one of two demo lanes per pair.
- GitHub `Alireza77n/Limit-Order-Book`: `MM_algo1v2` (this branch), `MM_algo2` (default,
  deployed), `base-algorithms` (`algo1v2_/`: this strategy and fill model as standalone files,
  regenerated whenever `strategy/algo1.py` or `fill_model.py` changes).

## Current goal (October 2026)

Keep this branch a faithful, self-contained record of Algorithm 1 version 2 under the rules of
20 October 2026 (one order per side, 3-minute orders, target 0.02 % since 22 October, trend pause), with its own
documentation and tests; measure its fills on a real account before judging it.

## Important constraints

- Never start or stop a live (real-money) worker from automation.
- This branch documents only Algorithm 1 version 2; no comparisons with other versions or
  algorithms; algorithm names are the branch names.
- The fill table's horizon must equal the order lifetime (`cancel_after_minutes × 60` = 180 s).
- Every number in the docs comes from a script or a recorded file, never from a hand calculation.
- `docs/` is gitignored: `git add -f` for new documentation files. Commit as Alireza.
