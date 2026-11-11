# MM_algo2: order-book imbalance and microprice market maker

**MM_algo2** is the market-making algorithm of this branch, for the Nobitex pairs BTC/IRT and USDT/IRT. Every
round it reads the order book, centres its quote on the **microprice** (the mid pulled toward the side with
less volume waiting), skews that centre against its inventory, and places one buy below it and one sell above
it, each one **half-spread** away: the smallest half-spread whose round trip clears both fees and the required
return. It also measures the **order-book imbalance** of the top levels; the imbalance would lean the
centre by `algo3_beta`, which is 0 because no lean has passed the calibration test yet. In the code and the
settings its key is `algo3` (class `Algo3Strategy` in [`market_maker/strategy/algo3.py`](market_maker/strategy/algo3.py)).
It runs in **demo** (real books, simulated fills in a paper ledger) and **live** (real orders, real money).

**Full description:** [docs/MM_algo2.md](docs/MM_algo2.md), every step from first to last, with the proofs,
the forecasting part, the engine rules, worked examples and limitations.
Other algorithms live on their own branches: `MM_algo1v2` and `MM_algo3`.

## One round, in pseudocode

```text
every time_step seconds (BTC 60 s, USDT 10 s)                  market_maker/engine.py

 1  [demo] reload the settings saved in the admin panel
 2  book <- GET https://apiv2.nobitex.ir/v3/orderbook/<PAIR>
    if the request failed, or best bid / best ask / lastTradePrice is missing:
        wait for the next round
 3  fills   [demo] a resting buy fills at its own price once best_ask <= price,
                   a resting sell once best_bid >= price (at most order_size per side per round)
            [live] read the status of our orders on Nobitex
 4  cancel our orders older than cancel_after_minutes (3)
 5  q <- coins held, including coins locked in our open sells

    # the strategy: (book, q) -> (B, A) or no quote            market_maker/strategy/algo3.py
 6  if the top is missing or crossed, or a side has fewer than k levels:  NO QUOTE
 7  V_b <- sum over i < k of rho^i * bid_volume[i];   V_a <- the same on the ask side
    I   <- (V_b - V_a) / (V_b + V_a)
 8  if V_b + V_a < algo3_min_volume_orders * order_size:        # thin book
        I <- 0;  p <- mid
    else:
        I <- clip(I, -0.8, +0.8)
        p <- (a0*vb0 + b0*va0) / (vb0 + va0)                    # microprice
 9  x <- clamp((q - inv_min) / (inv_max - inv_min), 0, 1)
    c <- p * (1 + beta*I) * (1 - skew_perc*(2x - 1))            # beta = 0 by default
10  G <- (1 + exp_ret_min) / (1 - f)^2 - 1                      # f = commission_perc
    h <- max(algo3_half_spread, G/(2 + G) + tick/c)            # tick = 10 rial
11  B <- round_down(min(c*(1 - h), a0 - tick))
    A <- round_up(max(c*(1 + h), b0 + tick))
12  if B <= 0 or B >= A or (1 - f)^2 * A/B - 1 <= exp_ret_min:  NO QUOTE   # cannot happen
13  if the last pair (B0, A0) is younger than refresh_seconds, still clears the bar,
       and |B - B0| <= w(B0) and |A - A0| <= w(A0):              # w(p) = max(5 ticks, 0.0003*p)
        (B, A) <- (B0, A0)
    write the decision to the diagnostics file

    # the engine: two prices -> at most one order per side
14  if the mid fell (rose) by trend_pause_perc or more over trend_window_seconds:
        pause buys (sells): cancel our resting orders on that side and send none
15  on each side, cancel our resting orders farther than w(new price) from it
16  buy_target  <- 2*order_size*(1 - x_b)        x_b: position of q + our open buys
    sell_target <- 2*order_size*x_s              x_s: position of q - our open sells
17  SELL at A, size min(sell_target, free coins - inv_min),
        unless sells are paused, one of our sells rests within w(A), or size < 0.1*order_size
18  BUY at B, size min(buy_target, free cash / B),
        unless buys are paused, one of our buys rests within w(B), size < 0.1*order_size,
        or open buys + q + size > inv_max
    [live] also refuse an order that would lock more than 25 % of the cash (buy) or coins (sell)
    send: [demo] to the paper ledger   [live] signed POST /market/orders/add
19  record the round; wait for the next one
```

## Key formulas

Imbalance of the top $`k`$ levels and the microprice ($`b_0,a_0`$ best bid and ask, $`v^b_0,v^a_0`$ their
volumes, $`m`$ the mid, $`s`$ the spread):

```math
I=\frac{V^b-V^a}{V^b+V^a},\qquad V^b=\sum_{i<k}\rho^{\,i}v^b_i,\qquad
p_{\mathrm{micro}}=\frac{a_0v^b_0+b_0v^a_0}{v^b_0+v^a_0}=m+\frac{s}{2}\,\frac{v^b_0-v^a_0}{v^b_0+v^a_0}.
```

Centre, with the inventory position $`x\in[0,1]`$:

```math
c=p_{\mathrm{micro}}\,(1+\beta I)\,\bigl(1-\sigma_q(2x-1)\bigr).
```

The fee bar. A round trip bought at $`B`$ and sold at $`A`$ returns $`R(B,A)=(1-f)^2A/B-1`$. With
$`B=c(1-h)`$ and $`A=c(1+h)`$:

```math
R>r_{\min}\iff\frac{A}{B}>1+G,\quad G=\frac{1+r_{\min}}{(1-f)^2}-1
\qquad\Longleftrightarrow\qquad
h>h_{\min}=\frac{G}{2+G}.
```

Prices on the 10-rial tick $`\epsilon`$, never crossing the book; the clip and the rounding only widen the
pair, so every issued pair clears the bar:

```math
B=\epsilon\Bigl\lfloor\frac{\min(c(1-h),\ a_0-\epsilon)}{\epsilon}\Bigr\rfloor,\qquad
A=\epsilon\Bigl\lceil\frac{\max(c(1+h),\ b_0+\epsilon)}{\epsilon}\Bigr\rceil,\qquad
\frac{A}{B}\ge\frac{1+h}{1-h}>1+G .
```

Requote band and order sizes ($`S`$ = `order_size`):

```math
w(p)=\max(n\,\epsilon,\ \omega\,p),\qquad
\text{buy}=2S\,(1-x_b),\qquad \text{sell}=2S\,x_s .
```

With the profile defaults ($`f`$ = 0.10 %, $`r_{\min}`$ = 0.02 %) the half-spread is about 0.110 %.

## Settings

Defaults from `market_maker/config.py` (both profiles unless marked). Editable in the admin panel; in demo
they apply at the next round (`commission_perc` at the next demo session); a live worker reads them when it
is started.

| Setting | Meaning | Default |
|---|---|---|
| `algo3_depth_k` | levels per side in the imbalance, $`k`$ | 3 |
| `algo3_decay_rho` | weight of each deeper level, $`\rho^i`$ | 0.5 |
| `algo3_beta` | lean of the centre per unit of imbalance, $`\beta`$ | 0 |
| `algo3_half_spread` | half-spread; 0 = the smallest that clears the bar, plus one tick | 0 |
| `algo3_min_volume_orders` | thin-book threshold, in order sizes | 20 |
| `algo3_imbalance_clip` | largest imbalance used | 0.8 |
| `skew_perc` | largest inventory shift of the centre, $`\sigma_q`$ | 0.002 |
| `commission_perc` | fee per fill, $`f`$ (live: at least 0.0025) | 0.0010 |
| `exp_ret_min` | required net return of a round trip, $`r_{\min}`$ | 0.0002 |
| `inv_min`, `inv_max` | inventory band, in coins | 0; BTC 0.000472, USDT 50 |
| `order_size` | order size $`S`$, in coins | BTC 0.000015, USDT 1 |
| `hysteresis_ticks`, `requote_band_perc` | requote band $`w(p)`$: $`n`$ ticks or $`\omega`$ of the price, the wider | 5, 0.0003 |
| `refresh_seconds` | a kept pair is re-issued after this age | 300 |
| `cancel_after_minutes` | life of a resting order | 3 |
| `trend_window_seconds`, `trend_pause_perc` | trend pause: window and threshold (0 = off) | 600, 0.003 |
| `time_step` | seconds between rounds | BTC 60, USDT 10 |
| `inventory_sizing_strength`, `inventory_sizing_min_fraction` | slope and floor of the linear sizing | 1, 0 |
| `algo3_enabled` | demo: run the MM_algo2 lane on this pair | on |
| `algorithm` | live: the algorithm the worker runs (`algo3` = MM_algo2) | `algo1` |

## Run, test, deploy

Install (Python 3.11, as in the Docker image) and run from `market_maker/`:

```bash
cd market_maker && pip install -r ../requirements-dev.txt
python run.py --profile btc --mode demo --continuous        # demo: real books, paper fills, every enabled lane
python run.py --profile btc --mode demo --algorithm algo3   # demo: MM_algo2 alone
python run.py --profile btc --mode real --algorithm algo3   # live: REAL MONEY
python -m pytest -q tests                                    # tests: no network, account or database
python dashboard.py                                          # results dashboard
python admin_app.py                                          # admin panel
```

Live needs the Nobitex keys in `market_maker/.env` (template `.env.example`); outside the server's forward
runtime, which sets `LOB_FORWARD_CONFIG_DIR`, it also writes to a MySQL database. On the server, live workers
are started and stopped from the admin panel only. The tests need Linux (the engine uses `fcntl`); on Windows
run them in Docker.

The whole demo stack in Docker (both pairs, dashboard on port 8050, admin on port 8051), locally and on the
server:

```bash
docker compose up -d --build --wait
docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env up -d --build --wait
```

Code, all under `market_maker/`: `strategy/algo3.py` (the strategy), `strategy/common.py` (shared arithmetic),
`engine.py` (the round), `paper.py` (demo fills), `exchange/nobitex.py` (the exchange), `config.py` (the
profiles), `dashboard.py` and `admin_app.py`.

This is a research project. Live mode trades real money; run the demo first and check the demo-to-real gate
([docs/MM_algo2.md](docs/MM_algo2.md#13-the-demo-to-real-gate)).
