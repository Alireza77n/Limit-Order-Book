# MM_algo1v2 — the comparison algorithm, in one page

This branch belongs to **MM_algo2** ([`MM_algo2.md`](MM_algo2.md)). **MM_algo1v2** (Algorithm 1 version 2,
`strategy/algo1.py`, setting value `algo1`) stays in the code and selectable per pair. That way both
algorithms can run under the same engine and be compared on the same dashboard (*Algorithm → All
algorithms*). It is also the code default of the `algorithm` setting. This page says what it is; its complete
documentation is on its own branch:

| Document (branch `MM_algo1v2`) | Content |
|---|---|
| [README](https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/README.md) | the algorithm with its formulas, worked example and code trace |
| [`ALGORITHM_1_V2.md`](https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/docs/algorithms/ALGORITHM_1_V2.md) | the full specification and its test map |
| [`FILL_MODEL.md`](https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/docs/algorithms/FILL_MODEL.md) | how the fill probabilities are measured |
| [`CHANGES_V1_TO_V2.md`](https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/docs/algorithms/CHANGES_V1_TO_V2.md) | what changed from version 1 |
| [`MARKET_MAKER_EXPLAINED.md`](https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/docs/guide/MARKET_MAKER_EXPLAINED.md) | the whole system explained around MM_algo1v2 |

## The rule in five steps

1. **Centre:** $c = m\,\bigl(1 - \sigma_q (2x - 1)\bigr)$. This is the same inventory skew as MM_algo2's, but
   applied to the mid instead of the microprice.
2. **Candidates:** prices at a geometric grid of distances from $c$ (0.05 %, then ×1.4 each step, up to 2 %),
   each one tick inside the first book level beyond its distance.
3. **Fill probability** $p$ of each candidate within one order lifetime (10 minutes): from the measured table
   `fill_table_<pair>irt.json`, rebuilt every night by `tools/build_fill_table.py`, or from a fallback formula.
4. **Choice:** among all pairs $(B, A)$ with $R(B, A) > r_{\min}$, the one with the largest expected value
   $J = p_B\,p_A\,R - \eta\,[p_B(1 - p_A) + p_A(1 - p_B)]$, provided $J > J_{\min}$ (`ev_min`).
5. **Single orders** at the band edges ($x \le 0.1$ buys only, $x \ge 0.9$ sells only) or when no pair passes;
   a resting price within the requote band (5 ticks or 0.03 % of the price, whichever is wider) is kept, as
   in MM_algo2.

## Against MM_algo2

| | MM_algo2 | MM_algo1v2 |
|---|---|---|
| Centre | microprice × imbalance lean × inventory skew | mid × inventory skew |
| Prices | one symmetric pair at ±$h$ around the centre | the best pair from a grid of candidates |
| How the spread is chosen | it is not: $h$ is the fee bar plus a tick | largest expected value $J$ above `ev_min` |
| Fill probabilities | not used | measured table, or a formula |
| One side only | never; at the band edges the engine's sizing makes one side zero | at the band edges, and as a fallback |
| Fee bar, sizing, caps, cancellation, hysteresis, one order per side, trend pause | the same | the same |

## Its settings and code in this branch

Read by MM_algo1v2 only: `grid_min_perc`, `grid_ratio`, `grid_max_perc`, `one_sided_lo`, `one_sided_hi`,
`cost_basis_reference`, `ev_min`, `adverse_selection_perc`, `fill_table_enabled`, `fill_table_max_age_hours`,
`fill_table_min_samples`, `fill_kappa`, `fill_lambda_tau`, `fill_queue_volume_ref`. They stay on the admin
config pages. The fee, the inventory band, the sizing, `skew_perc`, the hysteresis and the engine rules of
20 October 2026 (the requote band with one order per side, 3-minute orders, the target of 0.20 %, the trend
pause) are shared with MM_algo2: [`MM_algo2.md` §19](MM_algo2.md#19-changes-of-20-october-2026-one-order-per-side-shorter-orders-higher-target-lanes)
says what changed and why. On the demo MM_algo1v2 runs in its own lane (`algo1_enabled`), with its own paper
account and results (`<pair>_track_demo_algo1.*`), at the same time as MM_algo2.

Its code here (`strategy/algo1.py`, `fill_model.py`, `tools/build_fill_table.py`) makes the same decisions as
on its branch. When MM_algo2 was added, the order-book arithmetic both algorithms use (tick, mid, returns,
fee bar, inventory position) moved unchanged into `strategy/common.py`. The dashboard's Fill model tab and the
nightly fill-table job serve MM_algo1v2.
