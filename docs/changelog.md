# Changelog — branch `MM_algo3`

Newest first. Commit hashes are on this branch; it was cut from `MM_algo2` at `cb8c6ab` on
23 October 2026, whose history (the 20 October engine rules, the demo lanes, the admin and
dashboard work) it carries.

## 2026-10-23 — MM_algo3

### Changed
- New algorithm `strategy/algo4.py` (`Algo4Strategy`, key `algo4`, shown as **MM_algo3**): a
  mean-reverting (Ornstein–Uhlenbeck) fit on a 30-minute window of mids, the admissibility rule
  (0 < b < 1, t < −2.86, half-life 60–900 s), the lean (μ̂ − S)(1 − e^(−θ̂τ)) clipped to 0.5 %,
  the first-order depths 1/κ ± lean ∓ skew + risk, the fee shift and the fee-bar widening, a
  volatility guard, the shared hysteresis. Warm-up quotes without the forecast.
- `fill_model.py`: `FillTable.fit_intensity` and `FillModel.intensity` (κ and λ from the table).
- Catalog entry; `algo4_enabled` lane switch and ten `algo4_*` settings (57 fields per pair,
  54 hot; 52 live fields). Three demo lanes per pair.
- Dashboard: MM_algo3 charts (centre vs mid; quoted spread and net return), Quote decision
  panels (the window with μ̂, the ladder, the trace), the combined track renamed *Demo, all
  algorithms (compare)*.
- Tests: `tests/test_strategy_algo4.py` (36); the catalog, config, lane, admin and dashboard
  suites extended to three algorithms (1,024 passed locally plus the 13 Windows-only lock tests).
- Docs: `docs/algorithms/MM_algo3.md` with `data/make_mm_algo3_doc_data.py` (numbers from the
  recorded demo mids of 12–17 October and the fill tables of 17 October) and three figures;
  README, PROJECT_GUIDE, usage, ADMIN_PAGE, PNL_ATTRIBUTION, FORWARD_TEST rewritten around
  MM_algo3; MM_algo2's specification, data, figures and guide removed from this branch (they live
  on `MM_algo2`); `CLAUDE.md`, `project_context.md`, `decisions.md` (ADR-013…017).

### Reason
The operator asked for the third algorithm of the design documents under the branch name
`MM_algo3`, shown everywhere by that name, and for each branch to document its own algorithm.

### Result
On the recorded demo series the forecast is admissible in 6–8 % of the 30-minute windows and has
no measured skill (correlation of the lean with the next 180-second move −0.03 on USDT, −0.00 on
BTC); κ from the fill tables of 17 October is 727 (USDT) and 626 (BTC), i.e. a fee-free half-spread
of 0.14–0.16 %. The demo comparison with the other two lanes decides what to keep.

### Files
`market_maker/strategy/algo4.py`, `market_maker/strategy/catalog.py`, `market_maker/fill_model.py`,
`market_maker/{config,demo_config,forward_config,dashboard}.py`, `market_maker/tests/test_strategy_algo4.py`,
`docs/algorithms/MM_algo3.md`, `docs/algorithms/data/`, `docs/algorithms/figures/`.
