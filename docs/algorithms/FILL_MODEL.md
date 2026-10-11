# The fill model — where the fill probabilities come from

Algorithm 1 v2 scores every candidate quote by *expected value*, which needs,
for each distance $\delta$ from the quote centre, the probability
$p(\delta) = P(\text{an order resting there is filled within one lifetime } \tau)$.
This note describes the two sources of that number, the file that carries it,
how it is rebuilt, and how the strategy uses it.

**Code:** [`market_maker/fill_model.py`](../../market_maker/fill_model.py),
[`market_maker/tools/build_fill_table.py`](../../market_maker/tools/build_fill_table.py).

---

## 1. Two sources

### 1.1 Measured table (preferred)

`fill_table_<pair>.json` in the results directory
(`/var/lib/market-maker-forward/results/` on the server). Built by
`tools/build_fill_table.py` from two kinds of evidence:

**Price path.** A bid at $c_t(1-\delta)$ is filled when sellers come down to it,
i.e. when the best ask reaches it. For every sampled moment $t$ (the engine
writes `Best Bid` / `Best Ask` every round, 10 s on USDT, 60 s on BTC):

$$\hat p_b(\delta) = \frac{\#\{t : \min_{t<u\le t+\tau} a_0(u) \le c_t(1-\delta)\}}{\#\{t\}},
\qquad
\hat p_a(\delta) = \frac{\#\{t : \max_{t<u\le t+\tau} b_0(u) \ge c_t(1+\delta)\}}{\#\{t\}}.$$

A window that reaches less than 80 % of $\tau$, or that crosses a gap in the
data larger than three sampling steps (worker stopped, HTTP 403), is skipped.
The price path over-estimates fills near the touch (volume ahead of us may
absorb the trades), so a `--queue-factor` (< 1) can scale it once calibrated.

**Own orders.** Every demo/live row that carries an order (`Buy Limit Price`,
`Buy Status`, …) gives the order's distance at placement and its outcome; the
fraction filled per distance bucket is the true number the price path only
approximates.

**Blend and shape.** Per bucket, $p = w\,p_{\text{own}} + (1-w)\,p_{\text{path}}$
with $w = n_{\text{own}}/(n_{\text{own}} + 100)$; samples are weighted by age
with an exponential half-life (default 3 days, window 14 days); the final curve
is made non-increasing in distance (pool-adjacent-violators).

### 1.2 Parametric fallback

Used when there is no table, the table is older than `fill_table_max_age_hours`,
or a bucket has fewer than `fill_table_min_samples` observations:

$$p(\delta) = 1 - \exp\!\bigl(-\lambda_\tau\,e^{-\kappa\delta}\bigr).$$

$e^{-\kappa\delta}$ is the fill intensity relative to the touch
($\kappa = 400$: an e-fold every 0.25 %), $\lambda_\tau$ the expected number of
fills at zero distance per lifetime, and $1 - e^{-(\cdot)}$ turns an expected
count into the probability of at least one fill. With the defaults
($\kappa = 400$, $\lambda_\tau = 1$): $p(0) = 0.63$, $p(0.19\%) = 0.38$,
$p(0.38\%) = 0.20$, $p(0.75\%) = 0.05$, $p(1.5\%) = 0.0025$.

---

## 2. File schema

```json
{
  "pair": "USDT",
  "horizon_seconds": 600,
  "updated_at": "2026-10-11T03:00:14+00:00",
  "method": "price_path+own_orders",
  "samples": 120960,
  "own_orders": 1284,
  "window_days": 14.0,
  "half_life_days": 3.0,
  "queue_factor": 1.0,
  "sources": ["usdt_track.csv", "usdt_track_demo.csv"],
  "buckets": [
    {"distance": 0.001, "p_bid": 0.61, "p_ask": 0.58, "n_bid": 15310, "n_ask": 15310,
     "n_bid_path": 15000, "n_bid_own": 310, "n_ask_path": 15000, "n_ask_own": 310},
    {"distance": 0.0015, "p_bid": 0.47, "p_ask": 0.45, "n_bid": 15264, "n_ask": 15264, "...": "..."}
  ]
}
```

`distance` is a fraction of price; `n_*` counts drive the sample-size rule;
`updated_at` drives freshness. The strategy interpolates linearly between
bucket centres and clamps outside the first/last usable bucket.

---

## 3. Rebuilding it

On the server, daily (cron, as `marketmaker-forward`), from
`/opt/market-maker-forward/market_maker`:

```bash
venv/bin/python -m tools.build_fill_table --pair usdt --horizon-seconds 600 \
  --csv /var/lib/market-maker-forward/results/usdt_track.csv \
  --csv /var/lib/market-maker-forward/results/usdt_track_demo.csv \
  --out /var/lib/market-maker-forward/results/fill_table_usdtirt.json
venv/bin/python -m tools.build_fill_table --pair btc --horizon-seconds 600 \
  --csv /var/lib/market-maker-forward/results/btc_track.csv \
  --csv /var/lib/market-maker-forward/results/btc_track_demo.csv \
  --out /var/lib/market-maker-forward/results/fill_table_btcirt.json
```

(The file name must be `fill_table_<pair in lower case>.json`, e.g.
`fill_table_usdtirt.json`, which is what `FillModel.table_path()` looks for.)
A running worker reloads the file as soon as its modification time changes;
no restart is needed. `--queue-factor`, `--window-days`, `--half-life-days`
are the knobs; the tool prints the table it wrote.

---

## 4. How the strategy uses it

`FillModel.probability(distance, side)` → table if fresh and populated, else
formula; `FillModel.source(...)` says which; `FillModel.describe()` is included
in every diagnostics document (`fill_model` key) so the dashboard can show the
table's age, method and bucket count.

The dashboard's **Fill model** tab (usage.md §3) draws the measured table
against the formula, compares the probability written into each round row
(`Bid Fill Prob` / `Ask Fill Prob`) with the realised fill rate of our own
orders per distance bucket, shows the mid move after our fills at several
horizons (the adverse-selection estimate $\eta$ is read from it) and reports
the table's health (file, method, age, samples, own orders, sources).

The probability enters the expected value of a pair,
$J = p_b p_a R_{\text{net}} - \eta[p_b(1-p_a) + p_a(1-p_b)]$, and of a lone leg,
$J = pR_{\text{net}} - \eta p$ (see ALGORITHM_1_V2.md §2.4–2.5).

---

## 5. What is updated automatically and what is not

| Updated by the daily job (data) | Fixed, changed by hand from the admin panel (policy) |
|---|---|
| the table $p(\delta)$ per pair, per side | the grid (`grid_*`), `skew_perc`, `exp_ret_min` |
| $\eta$ once measured from post-fill price moves (written back as `adverse_selection_perc` only when you choose to) | `one_sided_lo/hi`, `hysteresis_ticks`, `refresh_seconds`, `ev_min` |
| the cost basis $\bar P$ (inside the strategy, every fill) | `fill_kappa`, `fill_lambda_tau` (re-fitted weekly at most, applied only where a bucket is thin) |

Keeping the policy fixed while the data follows the market is what lets a
report distinguish "the market changed" from "the bot changed".
