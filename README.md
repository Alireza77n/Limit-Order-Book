# MM_algo1v2: distance-anchored, expected-value market making

MM_algo1v2 (Algorithm 1, version 2) is a market maker for Nobitex spot pairs (BTC/IRT and USDT/IRT).
Every round it reads the order book, moves a quote centre away from the mid according to its
inventory, builds candidate bid and ask prices at fixed distances from that centre (each one tick in
front of a real book level), gives each candidate the probability that it fills within the order
lifetime, and quotes the bid/ask pair with the largest expected value that clears the fee bar, or a
single side when the inventory is near a band edge. It runs as a demo (paper orders on the real book)
or live (real orders). This branch documents MM_algo1v2 only; the other algorithms are on branches
`MM_algo2` and `MM_algo3`.

**Complete specification, with every formula, proof and worked example:
[docs/MM_algo1v2.md](docs/MM_algo1v2.md).**

## One round

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/figures/listing1_round_dark.svg">
  <img alt="Listing 1: one round of the worker, from reading the order book to sending at most one order per side" src="docs/figures/listing1_round.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/figures/listing2_decide_dark.svg">
  <img alt="Listing 2: DECIDE, how MM_algo1v2 chooses its bid and ask from the distance grid by expected value" src="docs/figures/listing2_decide.svg">
</picture>

The grey § numbers are sections of [docs/MM_algo1v2.md](docs/MM_algo1v2.md). Listing 1 is
`market_maker/engine.py`, Listing 2 is `market_maker/strategy/algo1.py` with the fill probabilities of
`market_maker/fill_model.py`.

**Listing 1.** $`T_s`$ time step (BTC 60 s, USDT 10 s), $`T_c`$ order lifetime (3 min), $`S`$ order size,
$`q_{\min}, q_{\max}`$ inventory band, $`x_b`$ and $`x_s`$ the position in the band counting our open buys and
leaving out our open sells, $`k`$ and $`\phi`$ the sizing strength and floor (1 and 0, so the sizes are
$`2S(1 - x_b)`$ and $`2S x_s`$), $`q_{\mathrm{free}}`$ and $`C`$ free coins and free cash, $`m_W`$ the oldest
mid of the last $`W`$ = 600 s, $`\theta`$ = 0.3 %, $`w(P) = \max(5\epsilon,\ 0.0003\,P)`$ the requote band
($`\epsilon`$: the 10-rial tick). The caps are the band ceiling for buys and the exposure guards.

**Listing 2.** $`B'`$ the last bid issued, $`D_{\max}`$ = 10 and $`n^b, n^a`$ the number of book levels,
$`\sigma`$ the centre shift, $`\delta_{\min}, \rho, \delta_{\max}`$ the distance grid (first point 0.05 %, ratio 1.4, at most 2 %),
$`\lfloor\cdot\rfloor_\epsilon`$ and $`\lceil\cdot\rceil_\epsilon`$ rounding to the tick. A fill probability
comes from the measured fill table when it is fresh, otherwise from $`1 - \exp(-\lambda_\tau e^{-\kappa\delta})`$.
$`x_{lo}, x_{hi}`$ = 0.1, 0.9 are the band positions where only one side is quoted; a lone leg is judged
against a reference price (an ask against the cost basis when it is known, else $`c`$; a bid against $`c`$). $`\eta`$ is the cost of a single
fill, $`J_{\min}`$ the expected-value floor, $`T_r`$ = 300 s.

## Key formulas

Inventory position, quote centre and the distance grid:

```math
x = \min\Bigl(1,\ \max\Bigl(0,\ \frac{q - q_{\min}}{q_{\max} - q_{\min}}\Bigr)\Bigr), \qquad
c = m\,\bigl(1 - \sigma(2x - 1)\bigr), \qquad
\delta_k = \delta_{\min}\,\rho^{\,k-1} \le \delta_{\max} .
```

Anchoring (bid side; the ask mirrors it):

```math
i^{*} = \min\{\, i \le D : b_i \le c(1 - \delta_k) \,\}, \qquad
B = \min\bigl(b_{i^{*}} + \epsilon,\ a_0 - \epsilon\bigr), \qquad
\hat\delta_b = \frac{c - B}{c} .
```

Fill probability when no fresh measured table answers:

```math
p(\hat\delta) = 1 - \exp\bigl(-\lambda_\tau\,e^{-\kappa\hat\delta}\bigr).
```

Net return of a round trip, the fee bar, and the expected value of a pair:

```math
\begin{aligned}
R_{\mathrm{net}} &= (1-f)^2\,\frac{A}{B} - 1 > r_{\min} \iff \frac{A}{B} > 1 + G, \qquad G = \frac{1 + r_{\min}}{(1-f)^2} - 1 \approx 2f + r_{\min}, \\
J &= p_b\,p_a\,R_{\mathrm{net}} - \eta\,\bigl[p_b(1 - p_a) + p_a(1 - p_b)\bigr], \qquad (B^{*}, A^{*}) = \arg\max \{\, J : R_{\mathrm{net}} > r_{\min},\ J > J_{\min} \,\}.
\end{aligned}
```

A lone leg against a reference price: ask $`R = (1-f)^2 A/P_{\mathrm{ref}} - 1`$, bid
$`R = (1-f)^2 P_{\mathrm{ref}}/B - 1`$, $`J_1 = p\,(R - \eta)`$. Sizes, with resting orders counted:

```math
\text{buy} = S\,\mathrm{clip}\bigl(1 - k(2x_b - 1),\ \phi,\ 1 + k\bigr), \qquad
\text{sell} = S\,\mathrm{clip}\bigl(1 + k(2x_s - 1),\ \phi,\ 1 + k\bigr),
```

which is $`2S(1 - x_b)`$ and $`2S x_s`$ with the defaults $`k = 1`$, $`\phi = 0`$.

## Settings

Per pair in `TRADING_PROFILES` of [`market_maker/config.py`](market_maker/config.py); editable from the
admin panel. Values are the profile values (BTC / USDT where they differ).

| Setting | Meaning | Default |
|---|---|---|
| `time_step` | seconds between rounds | 60 / 10 |
| `depth_max` | deepest book level used | 10 |
| `grid_min_perc`, `grid_ratio`, `grid_max_perc` | distance grid (11 points, 0.05 % to 1.45 %) | 0.0005, 1.4, 0.02 |
| `skew_perc` | centre shift $`\sigma`$ at the band edges | 0.002 |
| `inv_min`, `inv_max` | inventory band | 0, 0.000472 / 0, 50 |
| `one_sided_lo`, `one_sided_hi` | band positions for bids only, asks only | 0.1, 0.9 |
| `cost_basis_reference` | judge a lone sell against the average purchase price | true |
| `commission_perc` | fee per leg $`f`$ (live: at least 0.0025) | 0.0010 |
| `exp_ret_min` | required net return $`r_{\min}`$ (live: starts at 0.0025) | 0.0002 |
| `ev_min` | expected-value floor $`J_{\min}`$ | 0.00001 |
| `adverse_selection_perc` | cost $`\eta`$ when only one leg fills | 0 |
| `fill_table_enabled`, `fill_table_max_age_hours`, `fill_table_min_samples` | when the measured fill table is used | true, 48, 50 |
| `fill_kappa`, `fill_lambda_tau` | fallback fill formula | 400, 1.0 |
| `fill_queue_volume_ref` | queue correction, 0 = off | 0 |
| `hysteresis_ticks`, `requote_band_perc`, `refresh_seconds` | requote band and how long a price is kept | 5, 0.0003, 300 |
| `cancel_after_minutes` | order lifetime | 3 |
| `order_size` | base size $`S`$ | 0.000015 / 1 |
| `inventory_sizing_enabled`, `inventory_sizing_strength`, `inventory_sizing_min_fraction` | sizing rule, $`k`$, $`\phi`$ | true, 1.0, 0.0 |
| `trend_window_seconds`, `trend_pause_perc` | trend pause | 600, 0.003 |
| `buy_locked_cash_limit_*`, `sell_locked_inventory_limit_*` | exposure guards (live: on, at most 0.25) | off, 0.8 |

All settings, with the `MMConfig` defaults and when each applies: section 11 of the specification.

## Run, test, deploy

```bash
# local stack: both demo workers, dashboard on :8050, admin panel on :8051/admin/
docker compose up -d --build --wait

# one demo worker without Docker (paper orders on the real Nobitex book)
cd market_maker && python run.py --profile usdt --mode demo
# --mode real places real orders with the API key in market_maker/.env

# tests (Linux or the Docker image; no network or exchange account needed)
pip install -r requirements-dev.txt
LOB_DISABLE_DOTENV=1 python -m pytest market_maker/tests -q

# fill-probability table, horizon = 60 * cancel_after_minutes (the server rebuilds it daily)
cd market_maker && python -m tools.build_fill_table --pair usdt --horizon-seconds 180 \
  --csv results/usdt_track_demo.csv --out results/fill_table_usdtirt.json

# server stack (secrets stay in market_maker/.env and deploy/admin.env, never committed)
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env up -d --build --wait
```

The decision is in [`market_maker/strategy/algo1.py`](market_maker/strategy/algo1.py), the fill
probabilities in [`market_maker/fill_model.py`](market_maker/fill_model.py) and the round loop in
[`market_maker/engine.py`](market_maker/engine.py); the full code map is at the top of the
specification. This is research software; live mode trades real money.
