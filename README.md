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

```text
EVERY time_step SECONDS                                  engine.py: SimpleMarketMaker._run_demo
  apply the settings saved in the admin panel            (live: read once, at worker start)
  book <- GET /v3/orderbook/<PAIR>                       missing or malformed: skip the round
  b0, a0 <- best bid, best ask

  for each resting order of ours, best price first:      demo paper rule (live: Nobitex fills them)
      a buy is reached if a0 <= limit, a sell if b0 >= limit
      (or if the book reports a trade price that crosses the limit)
      fill at the limit (or that trade price), at most order_size per side per round
  cancel our resting orders older than cancel_after_minutes        (stale_timeout)
  q, cash, locked cash, locked coins <- account snapshot

  write the decision diagnostics (JSONL)
  quote <- DECIDE(book, q)                               None = no quote this round
  paused <- {buy}  if mid / (oldest mid of the last trend_window_seconds) - 1 <= -trend_pause_perc
            {sell} if it is >= +trend_pause_perc, else {}
  if quote is None: record the round, wait

  for side in (sell, buy):
      side paused:      cancel our resting orders on it                      (trend_pause)
      side has a price: cancel our resting orders farther than w(price) from it (replaced)
  x_b <- position of (q + resting buys),  x_s <- position of (q - resting sells)
  buy_size  <- order_size * clip(1 - k (2 x_b - 1), min_fraction, 1 + k)
  sell_size <- order_size * clip(1 + k (2 x_s - 1), min_fraction, 1 + k)
  SELL min(sell_size, free coins - inv_min) at the ask, unless
      no ask | paused | our sell rests within w(ask) | size < 0.1 order_size | exposure guard
  BUY min(buy_size, free cash / bid) at the bid, unless
      no bid | paused | our buy rests within w(bid) | size < 0.1 order_size
      | resting buys + q + size > inv_max | exposure guard
  record the round (SQLite row, CSV export, Telegram), wait for the next round

DECIDE(book, q)                                          strategy/algo1.py: Algo1Strategy.decide_order
  if q rose since the last call and a bid was issued then: book the rise at that bid (cost basis)
  D <- min(depth_max, bid levels - 1, ask levels - 1)
  if D = 0 or the book is crossed: forget the kept prices, return None
  x <- clamp((q - inv_min) / (inv_max - inv_min), 0, 1)
  c <- mid * (1 - skew_perc * (2x - 1))
  for delta = grid_min_perc * grid_ratio^k, while delta <= grid_max_perc:
      bid: first level i <= D with b_i <= c (1 - delta) -> B = b_i + tick
           (no such level: c (1 - delta) rounded down to the tick);  B <- min(B, a0 - tick)
      ask: first level j <= D with a_j >= c (1 + delta) -> A = a_j - tick
           (no such level: rounded up);                              A <- max(A, b0 + tick)
      keep each price once; distance = (c - B) / c or (A - c) / c
      p <- fill probability at that distance: the measured table if fresh,
           else 1 - exp(-fill_lambda_tau * exp(-fill_kappa * distance))
  if x >= one_sided_hi: return the best lone ask (reference: cost basis or c), or None
  if x <= one_sided_lo: return the best lone bid (reference: c), or None
  for every pair (B, A):
      R <- (1 - f)^2 * A / B - 1
      J <- pB * pA * R - eta * (pB (1 - pA) + pA (1 - pB))
      feasible if R > exp_ret_min and J > ev_min
  choice <- the feasible pair with the largest J (tie: the nearer pair)
            else the best feasible lone leg on either side, else return None
  hysteresis: per side keep the last issued price while the new one is within w of it
              and it is younger than refresh_seconds, if the kept quote still clears the bar
  return bid, ask (one may be None), levels, returns, probabilities, J, mode, reason
```

`w(P) = max(hysteresis_ticks * tick, requote_band_perc * P)` is the requote band (tick: 10 rial on
IRT pairs); a lone leg is a single bid or ask judged against a reference price.

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
\text{buy} = S\,\operatorname{clip}\bigl(1 - k(2x_b - 1),\ \phi,\ 1 + k\bigr), \qquad
\text{sell} = S\,\operatorname{clip}\bigl(1 + k(2x_s - 1),\ \phi,\ 1 + k\bigr),
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
