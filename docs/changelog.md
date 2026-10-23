# Changelog — branch `MM_algo2`

Newest first. Commit hashes are on this branch.

## 2026-10-23 — formulas typeset as math on GitHub

### Changed
- README and PROJECT_GUIDE: every formula block that was monospace text with Unicode symbols is
  now a LaTeX display block (`$$ … $$`), and the end-of-section algorithm summary is a numbered list
  with inline math. The guide's demo fee-bar half-spread now reads 0.0900 % (fee 0.08 %, target 0.02 % since 22 October); 0.0850 % was the value at the 0.01 % target.

### Reason
The operator: plain-text formula blocks render badly on GitHub; the rest of the documents already use
MathJax.

### Result
GitHub renders the formulas like the notation tables and the specification. No code change.

### Files
`README.md`, `PROJECT_GUIDE.md`.

## 2026-10-23 — admin export streams the archive from disk

### Changed
- `admin_app.py::_zip_backup_response`: `/admin/export` and `/admin/backups/<name>/download` zip
  the snapshot into a temporary file on disk (deflated) and stream it; the file is removed after
  the response. Before, the archive was built in an `io.BytesIO`.

### Reason
A snapshot now holds a 150 MB SQLite store and more; building the zip in memory took the admin
process over its 384 MiB container limit and the kernel OOM-killed it on every export (three times
on 23 October 2026 during the pages-mode smoke test, `docker events` shows `oom` then `die 137`).
Every kill restarted the admin container and dropped the connection ("Remote end closed connection
without response").

### Result
Memory stays flat whatever the snapshot size; the smoke test's `/admin/export` passes again. Each
export still creates a new snapshot directory under `runtime/backups/` (unchanged).

### Files
`market_maker/admin_app.py`.

## 2026-10-23 — a failed order-status lookup no longer ends the live worker

### Changed
- `engine.py::SimpleMarketMaker.update_inventory_and_cash`: when a status lookup raises (the
  client fails closed on a timeout or an incomplete page), the round keeps inventory and cash
  unchanged, logs `live_status_lookup_failed` and goes on; matched amounts and fees are
  cumulative, so the next successful round applies the missing delta once.
- `engine.py::SimpleMarketMaker._cancel_owned_stale_orders`: a failed lookup or cancel logs
  `live_stale_cancel_failed` and leaves the orders for the next round.
- `engine.py::SimpleMarketMaker._open_order_totals_or_none` wraps `get_open_order_totals` in the
  live quote step: on a failed or incomplete exposure lookup the round sends nothing
  (`live_exposure_lookup_failed`) instead of raising. The lookup itself still fails closed.
- Tests: `tests/test_engine_status_lookup_failure.py` (6 tests).

### Reason
Both live units exited with code 1 on 23 October 2026 (USDT 09:46 UTC, BTC 10:27 UTC): one
Nobitex request in `update_inventory_and_cash` timed out after 20 s (`RuntimeError: order status
lookup failed: ... Read timed out`), nothing caught it, and systemd recorded the failure (journal
of `market-maker-forward-live-*.service`). The orderbook fetch already tolerated such errors; the
status lookups did not.

### Result
A transient API failure costs one round, not the worker. Engine suites: 117 passed. No live
worker was started or stopped; the two failed units stay as the operator left them.

### Files
`market_maker/engine.py`, `market_maker/tests/test_engine_status_lookup_failure.py`.

## 2026-10-23 — live fill tracking survives pandas 3

### Changed
- `engine.py::SimpleMarketMaker.update_filled_amounts` casts the `Buy Status` and `Sell Status`
  columns to object dtype before it writes a matched amount over a client order id. New
  `tests/test_engine_filled_amounts.py` (3 tests) loads the table with `pandas.read_csv`, as the
  live worker does, and reproduced the error before the fix.

### Reason
Since pandas 3 (the live venv runs 3.0.6 on Python 3.14, the demo image 3.0.6 on Python 3.11) a
column of client order ids read from CSV is typed `str`; writing the float matched amount raised
`TypeError: Invalid value '2.1e-05' for dtype 'str'` and ended the BTC live worker on 17 October
2026 11:34 UTC (`/var/log/market-maker-forward/btc-live.log`); the exit-time reconciliation failed
on the same error ("Bulk cancel failed"). The demo lanes build their table with `dtype=object` and
were not affected.

### Result
Live fill tracking writes the amount; the engine suites pass. No live worker was started or
stopped. The same fix is on every branch (`MM_algo1v2`, `MM_algo2`, `MM_algo3`).

### Files
`market_maker/engine.py`, `market_maker/tests/test_engine_filled_amounts.py`.

## 2026-10-22

### Changed
- Dashboard: the Track list follows the Algorithm choice (one algorithm = the live track, its own
  demo lane and the single-worker history; "All algorithms" = every track); the two-lane track is
  named *Demo, both algorithms (compare)*; `apply_url` sets the tab only, `tracks_for_algorithm` the
  track.
- Target 0.02 %: `exp_ret_min` 0.002 → 0.0002 in both profiles and, through the control queue, in
  both pairs' demo overlays (in force since 10:14 UTC; ADR-012).
- Admin Algorithm page: the Demo table names the chosen algorithm in every row (column
  *Algorithm* = name + lane state); the other lanes moved to *Also running*.
- `docs/algorithms/MM_algo1v2.md` removed; every reference points at branch `MM_algo1v2` on GitHub
  (catalog `doc`, README, PROJECT_GUIDE, config comment).
- Math in the Markdown docs: `\operatorname` (not rendered by GitHub) replaced by `\mathrm`
  (README, MM_algo2.md, the guide, the investor report).
- Server: `/root/deploy_dashboard.sh` v5 takes `SKIP_TESTS=1` (with a `TESTED_LOCALLY` note in the
  log) — the 2 GB box cannot run the suite without starving sshd/nginx or being OOM-killed, which
  is what took the dashboards down on 20 and 21 October; the suite runs on the operator's PC.
- Server, 09:44–09:52 UTC: `f9b27a9` deployed to both stacks (tests skipped); the demo worker
  containers recreated on the new image; both pairs' overlays set to the 20 October rules
  (`cancel_after_minutes` 3, `exp_ret_min` 0.002, `requote_band_perc` 0.0003, `trend_pause_perc`
  0.003 over 600 s, both lanes on; fee 0.08 % kept); new demo session
  `demo-20261022T094903Z-81cabc208b6d` with all four lanes running; the fill-table cron and the
  tables rebuilt with `--horizon-seconds 180`. No live worker touched.

### Reason
The operator's review of 22 October: the Demo table showed the running algorithm's name where the
chosen one was expected; a page about MM_algo1v2 still lived on this branch; GitHub refused the
`\operatorname` macro.

## 2026-10-21

### Changed
- Added `CLAUDE.md`, `docs/project_context.md`, `docs/decisions.md`, `docs/changelog.md`.
- Branch `MM_algo1v2` received the same engine rules with its own documentation (`e6cf973`,
  guide `3ddb1b7`); `base-algorithms/algo1v2_` regenerated (`c438391`).
- Server: the deploy of `f9b27a9` to both stacks, the recreation of the demo worker containers
  on the new image, both pairs' demo overlays set to the 20 October rules (fee 0.08 % kept), a new
  demo session so both lanes of both pairs start fresh, and the nightly fill table built with
  `--horizon-seconds 180` — see the entry below this one once it is confirmed.

### Reason
The lanes release had not reached the server (the first deploy's test run on 20 October lost
pytest to a reboot, then was killed by memory pressure); the project needed a short, stable memory
for AI sessions; each branch documents its own algorithm.

## 2026-10-20 — `faeb0f6`, `62efbfa` (guides `059cdea`, `f9b27a9`)

### Changed
- Engine, both algorithms: requote band in percent with replacement (`requote_band_perc`
  0.0003 → one order per side), orders live 3 minutes, target 0.20 %, trend pause
  (`trend_window_seconds` 600, `trend_pause_perc` 0.003, on by default).
- Demo lanes: `algo1_enabled` / `algo3_enabled`; `run.py::LaneRunner`; per-lane files
  (`<pair>_track_demo_<lane>.*`, `paper_<session>_<pair>irt_<lane>.json`,
  `runtime_config_<pair>_<lane>.json`); the `algorithm` field is live-only.
- Admin Algorithm page: lane table (Running / Starting / Off), session shown as
  `<algorithm> · <session>`, Activate / Switch off per lane; Overview, Sessions, Orders, Balances
  updated. Dashboard: tracks per lane and side by side; the single-worker history track.
- Dashboard session picker no longer jumps back to "All sessions (compare)".
- `docs/algorithms/MM_algo1v2.md` back to a one-page pointer; MM_algo2.md §19 holds the rules
  and the replay table.

### Reason
The 15–17 October demo (fee 0.08 %, target 0.01 %) lost 19.96 M rial: all fills were price
touches of stale, stacked orders (`Report_investor/demo_analysis/`). The operator asked for the
four fixes on both algorithms, for all algorithms runnable at once, and for the trend pause.

### Result
Replay (fee 0.08 %): BTC MM_algo2 −1.90 % → −0.56 %, USDT −2.15 % → −0.48 %; fills 1,015 → 39
and 1,806 → 31; time at the best price 81 % → 9 % and 57 % → 1 %.

### Files
`market_maker/{config,engine,run,demo_config,forward_config,dashboard,admin_app}.py`,
`market_maker/strategy/{common,algo1,algo3}.py`, `market_maker/templates/admin/*.html`,
`deploy/control_agent.py`, `market_maker/tests/test_requote_lanes.py`, docs.

## 2026-10-17 — `76e9ddb`, `a229bd3`, `6f05978`, `3810761`

- Admin Algorithm page (drop-down → Demo table, Live table, active sessions); `admin.js`
  `#confirm-close` fix; session labels start with the algorithm(s) that ran.
- Docs rewritten around MM_algo2 (`docs/algorithms/MM_algo2.md` with real-book data and the
  generator `docs/algorithms/data/make_mm_algo2_doc_data.py`; guide rebuilt).

## 2026-10-16/17 — `8717bcc` … `d1420c7`

- MM_algo2 (`strategy/algo3.py`: order-book imbalance, microprice, fixed spread) added and
  selectable per pair; labels = branch names; deployed to both stacks.

## 2026-10-15 — `393605e`, `0ce09b3`

- PnL attribution tab; deploy script v3 (gzip test, sha256, test gate) after a partial upload
  swapped truncated trees in; session-deletion ownership fix (`_restore_stack_ownership`).

## 2026-10-11 — Algorithm 1 version 2 (branch `MM_algo1v2`, `c9a424d` …)

- Grid candidates, measured fill probabilities, expected-value selection, one-sided quotes,
  hysteresis; worker memory fixes (`affb34e`), dashboard row cap (`28970a7`).
