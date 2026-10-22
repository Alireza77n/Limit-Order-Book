# Changelog — branch `MM_algo2`

Newest first. Commit hashes are on this branch.

## 2026-10-22

### Changed
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
