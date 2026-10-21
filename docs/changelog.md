# Changelog — branch `MM_algo1v2`

Newest first.

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
