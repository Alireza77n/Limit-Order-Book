# Algorithm 1: what changed from version 1 to version 2

Version 1 is preserved on the git branch `MM_algo1v1` (formerly `main`).
Version 2 lives on `MM_algo1v2`. This note lists the behavioural differences,
the defects they fix, and the files touched.

## 1. Why

The live forward test (26 Sep – 1 Oct 2026) quoted in 0.15 % of BTC/IRT rounds
and 0.003 % of USDT/IRT rounds, sent 8 orders and filled none; the paper run
at a 0.10 % fee filled but lost 3.6 % because the second leg of each round trip
was re-quoted around a new mid. The review (PROJECT_GUIDE.md §9, README
"Logical weaknesses") found the causes in the selection rule itself.

## 2. Behavioural changes

| # | Version 1 | Version 2 | Fixes |
|---|---|---|---|
| 1 | `inv_d = int(10x) − 5`, unclamped; no quote at all above ≈1.6 × `inv_max` | $x$ clamped to $[0,1]$; continuous centre shift $c = m(1 - \sigma_q(2x-1))$ | PROJECT_GUIDE §9 item 1, README W1, W2 |
| 2 | candidates = level-index pairs $(i, i - \text{inv}_d)$; "level 10" means 0.1 % or 3 % depending on the book | candidates at fixed price distances (geometric grid), anchored one tick inside the first level beyond the target | README W5, the "parameter means nothing" problem |
| 3 | scan from the widest pair inward, **stop at the first failure**, keep the last pass | score **all** pairs by expected value $J = p_bp_aR_{\text{net}} - \eta[\dots]$; best feasible pair wins | README W4, W6 (`exec prob = 1.0` placeholders) |
| 4 | two-sided or nothing | one-sided at band edges, and as a fallback against a reference price (centre or own cost basis) | no sell-down possible with a full wallet; paper-run loss (selling below cost) |
| 5 | re-decides and re-posts every round | hysteresis in the strategy + resting-order guard in the engine | order accumulation (batch-fill incident), cancel churn, lost queue position |
| 6 | `float(ob["lastTradePrice"])` crashes on `null` | last price optional | PROJECT_GUIDE §9 item 6 |
| 7 | division by zero when `inv_max == inv_min` | position defaults to 0.5 | PROJECT_GUIDE §9 item 7 |
| 8 | the ask could land on the best bid (one-tick gap, ask level 0) | candidates clipped: $B \le a_0 - \epsilon$, $A \ge b_0 + \epsilon$ | PROJECT_GUIDE §9 item 8 |
| 9 | profitability bar on submitted prices, $R_{\text{net}} > r_{\min}$ | **unchanged** | — |
| 10 | sizes by the linear inventory rule in the engine | **unchanged** | — |

Unchanged on purpose: the fee bar semantics, the sizing rule, the engine's
accounting, cancellation, CSV/Telegram/dashboard formats (new keys are
additive; one-sided rows use the existing `not_sent_<reason>` convention).

## 3. New components

| File | Purpose |
|---|---|
| `market_maker/strategy/algo1.py` | rewritten (same class name, same `decide_order` / `diagnostics` / `no_quote_reason` interface) |
| `market_maker/fill_model.py` | fill-probability table loader + parametric fallback |
| `market_maker/tools/build_fill_table.py` | daily builder of `fill_table_<pair>.json` from the CSVs |
| `market_maker/config.py` | 18 new `MMConfig` fields and profile values; `demo_level1_fast_fill_enabled=False` in both profiles |
| `market_maker/demo_config.py`, `forward_config.py` | the new fields are admin-editable (hot) and saved for live; two cross-field checks |
| `market_maker/engine.py` | a quote may omit a side; resting-order guard (`_resting_order_blocks`); resting prices collected in `get_open_order_totals`; Telegram line for a missing side |
| `docs/algorithms/` | this folder: specification, fill model, figures (`figures/make_figures.py` regenerates them from the code) |
| tests | `test_strategy_algo1.py`, `test_algo1_baseline.py`, `test_algo1_fallback_regression.py`, `test_algo1_profitability.py`, `test_algo1_diagnostics.py` rewritten; `test_fill_model.py`, `test_build_fill_table.py`, `test_engine_one_sided.py` new; demo-engine tests that post a fixed price every round now switch the guard off explicitly, and the two that relied on the level-1 fast fill opt in explicitly |

## 4. Removed

`Algo1Strategy.leg_pairs`, `Algo1Strategy.algo1_bid_ask_prices` and the
engine wrappers `_leg_pairs`, `_algo1_bid_ask_prices`. `spread_ok(ob, i, j)`
is kept as a diagnostic helper (it is what the admin hot-reload tests probe).
`diagnostics()` still emits `inv_d` (the legacy staircase value) and
`fallback_detected = False` for readers that expect them.

## 5. Operational notes

* The demo matcher's `level1_fast_fill` is off in the profiles; demo fills
  now come from the price path, like the fill table assumes.
* The first fill table should be built from the existing CSVs before the demo
  run starts (see FILL_MODEL.md §3); until it exists the formula is used and
  the diagnostics say so.
* The validation gate in ALGORITHM_1_V2.md §7 decides when the same code may
  run in real mode.
