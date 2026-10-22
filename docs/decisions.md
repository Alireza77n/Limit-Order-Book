# Architecture Decisions — branch `MM_algo1v2`

Accepted decisions, newest last. Change one by adding a new ADR that supersedes it.

## ADR-001: this branch holds Algorithm 1 version 2 alone

Decision: `MM_algo1v2` carries one strategy and documents only it; other algorithms and the
per-pair selection live on their own branches (`MM_algo2`). Reason: the operator's rule — each
branch is a self-contained record of one algorithm. Status: accepted 2026-10-11, reaffirmed
2026-10-20.

## ADR-002: fill probabilities are measured, with a formula as fallback

Decision: `FillModel` reads the nightly table `fill_table_<pair>irt.json` when fresh and falls
back to `1 − exp(−λτ · exp(−κδ))`. Reason: the measured price paths are the only evidence of fills
the demo can give. Status: accepted 2026-10-11 (`docs/algorithms/FILL_MODEL.md`).

## ADR-003: requote band in percent, one order per side

Decision: a resting order is kept while the new price is within
max(`hysteresis_ticks` × tick, `requote_band_perc` × price) of it and replaced otherwise; the
strategy's hysteresis uses the same band. Reason: the tick-only band (50 rial) was nothing on BTC;
orders stacked and most fills were of stale orders. Status: accepted 2026-10-20
(`docs/algorithms/ALGORITHM_1_V2.md` §2.8).

## ADR-004: orders live 3 minutes

Decision: `cancel_after_minutes` = 3 (was 10); the fill table is built with
`--horizon-seconds 180` so the probabilities refer to the same lifetime. Reason: an order that no
longer follows the quote should not rest for ten minutes. Status: accepted 2026-10-20.

## ADR-005: target 0.20 % until real fills are measured

Decision: `exp_ret_min` = 0.002 in both profiles. Reason: the operator's requirement; the demo's
touch-only fill rule cannot measure fills. Status: accepted 2026-10-20; revisit with real fills.

## ADR-008: target 0.02 % (supersedes ADR-005)

Decision: `exp_ret_min` = 0.0002 in both profiles. Reason: at 0.20 % the demo lanes of 22 October
placed almost no order in their first 25 minutes (this algorithm: a buy in 1 of 27 BTC rounds and
0 of 161 USDT rounds); the operator decided the demo must trade. Status: accepted 2026-10-22.

## ADR-006: trend pause on by default

Decision: `trend_pause_perc` = 0.003 over `trend_window_seconds` = 600: no buy after the mid fell
0.3 % within 10 minutes (no sell after such a rise). Reason: the operator wants the bot not to keep
buying into a fall; the replay of 15–17 October is neutral for this algorithm. `0` switches it off.
Status: accepted 2026-10-20.

## ADR-007: the demo fills by price touch only

Decision: a paper order fills when the opposite best price reaches it; no trade feed is modelled.
Consequence: with replacement on, this algorithm barely trades on the demo (its candidates jump
between book levels); real fills need a real account. Status: accepted (limitation documented).
