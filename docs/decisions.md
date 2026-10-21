# Architecture Decisions — branch `MM_algo2`

Accepted decisions, newest last. Change one by adding a new ADR that supersedes it.

## ADR-001: algorithm keys stay, names are the branch names

Decision: the strategy keys in code and rows stay `algo1` and `algo3`; everything a person sees
(admin, dashboard, docs) calls them `MM_algo1v2` and `MM_algo2`, the git branch names.
Reason: the keys are written in every recorded round; renaming them would break history, while the
operator thinks in branch names. Status: accepted 2026-10-17.

## ADR-002: demo lanes are threads inside one worker per pair

Decision: a pair's demo worker runs every enabled algorithm at once, each in its own lane (thread)
with its own paper account, result files and runtime snapshot; not one container per algorithm.
Reason: the server has 2 GB of memory and no swap; a second pair of worker containers would not
fit. Status: accepted 2026-10-20.

## ADR-003: requote band in percent, one order per side

Decision: a resting order is kept while the new price is within
max(`hysteresis_ticks` × tick, `requote_band_perc` × price) of it and replaced otherwise.
Reason: the tick-only band (50 rial) was nothing on BTC; orders stacked (up to 11 on BTC, 26 on
USDT) and most fills were of stale orders. The replay cut the demo loss by about two thirds.
Status: accepted 2026-10-20 (`docs/algorithms/MM_algo2.md` §19).

## ADR-004: orders live 3 minutes

Decision: `cancel_after_minutes` = 3 (was 10). Reason: an order that no longer follows the quote
should not rest for ten minutes; little effect once replacement is on. Status: accepted 2026-10-20.

## ADR-005: target 0.20 % until real fills are measured

Decision: `exp_ret_min` = 0.002 in both profiles. Reason: the operator's requirement; the demo's
touch-only fill rule cannot measure fills, so the bar stays high until a real account does.
Status: accepted 2026-10-20; revisit with real fills.

## ADR-006: trend pause on by default

Decision: `trend_pause_perc` = 0.003 over `trend_window_seconds` = 600: no buy after the mid fell
0.3 % within 10 minutes (no sell after such a rise). Reason: the operator wants the bot not to keep
buying into a fall. The replay of 15–17 October is neutral (at most 0.06 of a point either way);
the setting is on all the same and `0` switches it off. Status: accepted 2026-10-20.

## ADR-007: the `algorithm` field is for live workers only

Decision: a live worker reads `algorithm` when it starts; the demo ignores it and runs the lanes
switched on by `algo1_enabled` / `algo3_enabled`. Reason: lanes replace switching on the demo.
Status: accepted 2026-10-20.

## ADR-008: each branch documents its own algorithm

Decision: `MM_algo2` documents MM_algo2; MM_algo1v2's behaviour, numbers and changes live on
branch `MM_algo1v2`; `docs/algorithms/MM_algo1v2.md` here is a one-page pointer.
Reason: the operator's rule. Status: accepted 2026-10-17, reaffirmed 2026-10-20.

## ADR-009: the demo fills by price touch only

Decision: a paper order fills when the opposite best price reaches it; no trade feed is modelled.
Reason: Nobitex's public API gives the book, not the trades. Consequence: demo fill counts are a
lower bound for a resting order and an upper bound for adverse selection; real fills need a real
account. Status: accepted (limitation documented, `MM_algo2.md` §18).

## ADR-010: deploy gate and demo containers

Decision: a deploy runs only after the archive's sha256 is verified and the full test suite passes
on the server (at least 500 passed); the demo worker containers are recreated by hand after worker
code changes. Reason: the 2026-10-15 partial-upload incident; Compose's `paper` profile.
Status: accepted 2026-10-15/17.

## ADR-011: the dashboard refresh never rewrites a valid choice

Decision: periodic callbacks return `no_update` for a picker whose value is still valid.
Reason: a refresh request carries the earlier value as State; writing it back undid a choice
made while the request was pending (the session picker "jumped back"). Status: accepted 2026-10-20.
