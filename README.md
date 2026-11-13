# MM_algo2_mazdax: order-book imbalance and microprice market maker

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

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/Alireza77n/Limit-Order-Book/raw/MM_algo2_mazdax/docs/figures/listing1_round_dark.svg">
  <img alt="Listing 1: one round of the worker, from reading the order book to sending at most one order per side" src="docs/figures/listing1_round.svg">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/Alireza77n/Limit-Order-Book/raw/MM_algo2_mazdax/docs/figures/listing2_decide_dark.svg">
  <img alt="Listing 2: DECIDE, how MM_algo2 turns the order book and the inventory into a bid B and an ask A" src="docs/figures/listing2_decide.svg">
</picture>

The grey § numbers are sections of [docs/MM_algo2.md](docs/MM_algo2.md). Listing 1 is
`market_maker/engine.py`, Listing 2 is `market_maker/strategy/algo3.py`.

**Listing 1.** $`T_s`$ time step (BTC 60 s, USDT 10 s), $`T_c`$ order lifetime (3 min), $`S`$ order size,
$`q_{\min}, q_{\max}`$ inventory band, $`x_b`$ and $`x_s`$ the position in the band counting our open buys and
leaving out our open sells, $`q_{\mathrm{free}}`$ and $`C`$ free coins and free cash, $`m_W`$ the oldest mid of
the last $`W`$ = 600 s, $`\theta`$ = 0.3 %, $`w(P)`$ the requote band. The caps are the band ceiling for buys
and, live, the 25 % exposure guards.

**Listing 2.** $`k`$ = 3 levels with weights $`\rho^i`$ ($`\rho`$ = 0.5), $`n_V`$ = 20, $`\beta`$ = 0,
$`\sigma_q`$ the inventory skew, $`h_0`$ the configured half-spread (0 = use the bar alone), $`\epsilon`$ the
tick, $`\lfloor\cdot\rfloor_\epsilon`$ and $`\lceil\cdot\rceil_\epsilon`$ rounding down and up to the tick,
$`T_r`$ = 300 s.

## Key formulas

Imbalance of the top $`k`$ levels and the microprice ($`b_0,a_0`$ best bid and ask, $`v^b_0,v^a_0`$ their
volumes, $`m`$ the mid, $`s`$ the spread):

```math
I=\frac{V^b-V^a}{V^b+V^a},\qquad V^b=\sum_{i < k}\rho^{\,i}v^b_i,\qquad
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

## Exchanges: Nobitex and Mazdax

This branch (`MM_algo2_mazdax`) runs MM_algo2 on two exchanges. It is built on the code of
`MM_algo3_mazdax` (the engine that runs many markets and two exchanges); MM_algo1v2 and MM_algo3 are in the tree too,
switched off here (`algo1_enabled`, `algo3_enabled`, `algo4_enabled` in `market_maker/config.py`). Every
market in `config.py::TRADING_PROFILES` belongs to one exchange (its `exchange` key), and the worker picks
the exchange client from the market: `exchange/nobitex.py` or `exchange/mazdax.py`.

| | Nobitex | Mazdax |
|---|---|---|
| Markets | `btc`, `usdt`, `eth`, `xaut`, `trx` (BTCIRT, ...) | `ahrm1`, `roba1`, `nogh1`, `zfam1` (AHRM1IRR, ...) |
| What trades | crypto against rial | fund and commodity tokens against rial |
| Price tick, amount step | 10 rial, none | 1 rial, 1 unit |
| Smallest order | 10 % of `order_size` | the same, and 100,000 rial (ZFAM1: 1,000,000) |
| Cash wallet | `rls` | `irr` |
| Credentials (live only) | `NOBITEX_API_KEY`, `NOBITEX_SECRET_KEY` | `MAZDAX_API_KEY`, `MAZDAX_SECRET_KEY` |
| Demo fills | touch rule | touch rule, and Mazdax's public trades |

The results dashboard has an **Exchange** selector in its top bar and the admin panel has one in its
header: choosing Nobitex or Mazdax shows that exchange's markets on every page (`?exchange=mazdax` in a
dashboard link opens it on Mazdax).

Mazdax's books are wide and thin (on 13 November 2026 the best bid and the best ask of the four markets
were 6 to 10 % apart, with trades printing inside that gap). The Mazdax profiles carry a first calibration from Mazdax's public trades of 12-13 November 2026:
quotes about 1.5 % from the middle of the recent trades (2 % on ROBA1), where the gross gain per hour of
a quoted pair was largest. It is a starting point to be measured again on the demo's own fills. A Mazdax API key for a live worker needs the
`read_only` and `spot` permissions only, never `withdraw` or `transfer`, and the server's address in its IP
list. Details: appendix "Exchanges: Nobitex and Mazdax" of [docs/MM_algo2.md](docs/MM_algo2.md).

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
