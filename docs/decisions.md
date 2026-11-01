# Architecture Decisions — branch `MM_algo3`

Accepted decisions, newest last. Change one by adding a new ADR that supersedes it. ADR-001 to
ADR-012 are inherited from branch `MM_algo2` (this branch was cut from it on 23 October 2026).

## ADR-001: algorithm keys stay, names are the branch names

Decision: the strategy keys in code and rows are `algo1`, `algo3`, `algo4`; everything a person
sees calls them `MM_algo1v2`, `MM_algo2`, `MM_algo3`, the git branch names. Reason: the keys are
written in every recorded round; the operator thinks in branch names. Status: accepted 2026-10-17,
extended 2026-10-23.

## ADR-002: demo lanes are threads inside one worker per pair

Decision: a pair's demo worker runs every enabled algorithm at once, each in its own lane (thread)
with its own paper account, result files and runtime snapshot. Reason: the server's memory.
Status: accepted 2026-10-20.

## ADR-003: requote band in percent, one order per side

Decision: a resting order is kept while the new price is within
max(`hysteresis_ticks` × tick, `requote_band_perc` × price) of it and replaced otherwise.
Status: accepted 2026-10-20.

## ADR-004: orders live 3 minutes

Decision: `cancel_after_minutes` = 3. Status: accepted 2026-10-20.

## ADR-006: trend pause on by default

Decision: `trend_pause_perc` = 0.003 over `trend_window_seconds` = 600 (no buy after the mid fell
0.3 % within 10 minutes, no sell after such a rise). Status: accepted 2026-10-20.

## ADR-007: the `algorithm` field is for live workers only

Decision: the demo runs the lanes switched on by `<key>_enabled`. Status: accepted 2026-10-20.

## ADR-008: each branch documents its own algorithm

Decision: `MM_algo3` documents MM_algo3; MM_algo2's and MM_algo1v2's behaviour, numbers and
changes live on their branches; the catalog's doc links for `algo3` and `algo1` point at those
branches on GitHub. Reason: the operator's rule. Status: accepted 2026-10-17, applied to this
branch 2026-10-23.

## ADR-009: the demo fills by price touch only

Decision: a paper order fills when the opposite best price reaches it. Consequence: fill counts
are a lower bound; real fills need a real account. Status: accepted.

## ADR-010: deploy gate and demo containers

Decision: a deploy verifies the archive's sha256 and runs with the test suite skipped on the
server (`SKIP_TESTS=1`, the suite runs on the operator's PC); the demo worker containers are
recreated by hand after worker code changes. Status: accepted 2026-10-15, amended 2026-10-22.

## ADR-011: the dashboard refresh never rewrites a valid choice

Decision: periodic callbacks return `no_update` for a picker whose value is still valid.
Status: accepted 2026-10-20.

## ADR-012: target 0.02 %

Decision: `exp_ret_min` = 0.0002 in both profiles. Status: accepted 2026-10-22.

## ADR-013: MM_algo3 is the design note's Algorithm 4, key `algo4`, as a third lane

Decision: the mean-reverting stochastic-control market maker is `strategy/algo4.py`
(`Algo4Strategy`, key `algo4`, shown as MM_algo3) and runs as a third demo lane next to MM_algo2
and MM_algo1v2 (`algo4_enabled`); the `algo2` Avellaneda–Stoikov stub stays as it is.
Reason: the note's numbering gives the key, the branch gives the name (ADR-001); a lane makes the
three comparable on the same market. Status: accepted 2026-10-23.

## ADR-014: the unit-root test uses the Dickey–Fuller 5 % critical value

Decision: the forecast is admissible only when the t-statistic of b − 1 is below −2.86, not the
note's −2. Reason: measured on 200 simulated random walks of 180 samples, −2 admits 25.5 % of them
as mean-reverting, −2.86 admits 4.5 % (`docs/algorithms/MM_algo3.md` §7.2). Status: accepted
2026-10-23.

## ADR-015: the warm-up quotes without the forecast

Decision: with fewer than 30 samples the round is quoted as the Avellaneda–Stoikov pair instead of
not at all. Reason: a restarted worker would otherwise sit out 30 minutes; the pair needs no
forecast. Status: accepted 2026-10-23.

## ADR-016: κ from the fill table, the skew and the risk term in relative units

Decision: κ is fitted on the pair's fresh fill table (`FillTable.fit_intensity`), `algo4_kappa`
is the fallback; the inventory term is the shared `skew_perc` and the widening `algo4_risk_perc`,
both as fractions of the price. Reason: one setting serves both pairs; the table is the project's
measured fill evidence; the skew is the one the other algorithms use. Status: accepted 2026-10-23.

## ADR-017: the forecast stays on by default despite no measured skill

Decision: `algo4_use_alpha` = true. Reason: on the recorded demo mids the lean has no skill
(correlation with the next move −0.03 / −0.00); the design note's rule for that case is to run with
the forecast off, but the demo lane is the agreed test — the whole algorithm runs, its estimates
are visible on the dashboard, and the comparison with the other lanes decides. Revisit after the
demo. Status: accepted 2026-10-23.

## ADR-018: three more demo markets, ETH, XAUT and TRX

Decision: the demo lanes run on five IRT markets, the original BTC and USDT plus ETH, XAUT and
TRX, each as its own worker and profile (`config.TRADING_PROFILES`), with the same strategy
defaults as USDT and sizes of about 3 million rial per order. Reason: the Nobitex 24 h statistics
of 1 November 2026 (228 rial markets): ETH (59 bn toman/day, spread 0.115 %) is the second major
asset and its spread is wider than the algorithm's optimal depth, so the quotes sit at the touch
more often than on USDT; XAUT (82 bn, 0.047 %) is tokenised gold with very low volatility, the
best case for a mean-reverting signal and almost no inventory risk; TRX (135 bn, 0.019 %) has
BTC-size volume with a stable price. Rejected: ZEC (volume from a one-day pump), BEAMX, PROM, G,
DOOD and the meme coins (10–20 % daily moves make the inventory one-sided), SOL and BNB (less
volume than ETH, wider and noisier). Consequences: three more containers (256 MiB each) on a
4 GB, 2-CPU host, so the new workers step every 30 s; no fill table until the cron has their
own rounds; the choice is re-examined after two weeks of demo data with the replay of
`Report_investor/proposal_20261025`. Status: accepted 2026-11-01.
