# Changelog — branch `MM_algo1v2`

Newest first.

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
- Target 0.02 %: `exp_ret_min` 0.002 → 0.0002 in both profiles (ADR-008); on the server the demo
  overlays of both pairs carry the same value for this algorithm's lanes.
- `\operatorname` → `\mathrm` in the Markdown math (GitHub does not render `\operatorname`); the
  guide's plain-text listings read `pos(q)` again.

### Reason
At 0.20 % the demo lanes placed almost no order; GitHub refused the macro.

## 2026-10-21 — engine rules of 20 October 2026

### Changed
- Engine: requote band in percent with replacement (`requote_band_perc` 0.0003 → one order per
  side), orders live 3 minutes, target 0.20 %, trend pause (`trend_window_seconds` 600,
  `trend_pause_perc` 0.003, on by default). The strategy's hysteresis uses the same band
  (`strategy/algo1.py::Algo1Strategy.requote_band`).
- Settings: three new hot fields on the Config and Live Config pages (37 per pair, 34 hot).
- Docs: `ALGORITHM_1_V2.md` §2.6 and new §2.8 (the rules and this algorithm's replay numbers),
  README step 6 and stage 7, PROJECT_GUIDE, usage, ADMIN_PAGE, FILL_MODEL (horizon 180 s), the
  guide; `base-algorithms/algo1v2_` regenerated.
- Added `CLAUDE.md`, `docs/project_context.md`, `docs/decisions.md`, `docs/changelog.md`.

### Reason
The 15–17 October demo (fee 0.08 %, target 0.01 %) lost 19.96 M rial: all fills were price
touches of stale, stacked orders. The operator asked for the four fixes on both algorithms and for
each algorithm's changes to be documented on its own branch.

### Result
Replay (fee 0.08 %, touch fill rule): BTC −0.91 % → 0.00 % with no fill, USDT −0.21 % → −0.11 %
with 3 fills — this algorithm barely trades under the demo's fill rule once replacement is on.

### Files
`market_maker/{config,engine,demo_config,forward_config}.py`, `market_maker/strategy/algo1.py`,
`market_maker/tests/test_requote_rules.py` and the updated suites, docs.

## 2026-10-17 — `f528270`

- README points to branch `MM_algo2`; describes Algorithm 1 as version 2.

## 2026-10-16 — `18a74cf`, `11527ba`

- Whole-project guide `docs/guide/MARKET_MAKER_EXPLAINED.{md,html}`; part 11 as three pseudocode
  listings.

## 2026-10-15 — `393605e`, `0ce09b3`

- PnL attribution tab; deploy script v3 (gzip test, sha256, test gate); session-deletion ownership
  fix.

## 2026-10-11 — Algorithm 1 version 2 (`c9a424d` …)

- Grid candidates, measured fill probabilities, expected-value selection, one-sided quotes,
  hysteresis; worker memory fixes (`affb34e`), dashboard row cap (`28970a7`).
