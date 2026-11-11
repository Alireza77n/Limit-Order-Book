# Limit-Order-Book: branch MM_algo3

This is a market-making bot for the Nobitex exchange. Every round a worker reads the order book of a rial
market, keeps one limit buy below the mid and one limit sell above it, and earns the spread when both fill,
after the fee on each leg. It runs in two modes:

* **demo**: real order books, simulated fills in a paper ledger;
* **live**: real orders, real money.

A results dashboard and an authenticated admin panel go with it. This branch documents **MM_algo3**.
[MM_algo2](https://github.com/Alireza77n/Limit-Order-Book/tree/MM_algo2) and
[MM_algo1v2](https://github.com/Alireza77n/Limit-Order-Book/tree/MM_algo1v2) document their own algorithms.
The code of all three is here, so they can run side by side.

## MM_algo3

MM_algo3 (code key `algo4`, `market_maker/strategy/algo4.py`) works in four steps:

* It fits an Ornstein–Uhlenbeck mean-reversion model to the last 30 minutes of mids.
* When a unit-root test and a half-life bound admit the fit, it leans its quotes toward the fitted mean, by
  the expected reversion over the order lifetime.
* Each depth comes from the first-order solution of an inventory-penalised optimal market-making problem
  (Avellaneda–Stoikov, Cartea–Jaimungal): $`1/\kappa`$, plus or minus the lean, minus or plus the inventory
  skew, plus the fee. $`\kappa`$ is the fill-intensity decay, fitted on the measured fill table.
* The pair is put on the tick and widened if needed to clear the fee bar.

The complete specification covers a round from its first step to its last, with every derivation, worked
examples and the measured forecast skill: **[docs/MM_algo3.md](docs/MM_algo3.md)**.

## One round

```text
ROUND of a demo lane (engine.py SimpleMarketMaker._run_demo; live: SimpleMarketMaker.run)

 1  reload the pair's Demo overlay; apply the hot fields; the lane's algorithm stays algo4
 2  ob <- GET https://apiv2.nobitex.ir/v3/orderbook/<SYMBOL>
    if the request fails or bids / asks / lastTradePrice are missing: skip the round
 3  paper fills: a resting buy at B fills when best_ask <= B, a resting sell at A when best_bid >= A,
    at its own limit price, at most order_size per side per round; fee f on every fill
 4  cancel every order older than cancel_after_minutes (3 min)
 5  q, cash, locked, available <- paper account snapshot (q includes the coins locked in sells)
 6  write the diagnostics document (the decision below, without changing any state)

 7  DECIDE: strategy/algo4.py Algo4Strategy.decide_order(ob, q)
    S <- (best_bid + best_ask) / 2                       ; invalid top of book -> no quote
    if algo4_sample_seconds have passed since the last sample: append (now, S)
    drop the samples older than algo4_window_seconds
    if fewer than 30 samples: fit <- warming_up (no forecast)
    else OLS of S[k+1] = a + b S[k] + e over the window:
        dt <- (t_last - t_first) / (n - 1);  t <- (b - 1) / se(b)
        theta <- -ln(b) / dt;  mu <- a / (1 - b);  half_life <- ln 2 / theta
        admissible <- 0 < b < 1 and t <= -2.86 and halflife_min <= half_life <= halflife_max
    if algo4_sigma_pause > 0 and s_eps / S > algo4_sigma_pause: no quote (volatility_pause)
    tau <- algo4_horizon_seconds, or cancel_after_minutes * 60 when it is 0
    m <- 0
    if algo4_use_alpha and admissible:
        m <- clip((mu - S) (1 - exp(-theta tau)), -L S, +L S) / S        ; L = algo4_lean_clip
    x <- clip((q - inv_min) / (inv_max - inv_min), 0, 1);  s <- skew_perc (2x - 1)
    kappa <- fit of the fresh fill table (fill_model.py FillTable.fit_intensity):
             OLS of ln(-ln(1 - P) / tau_table) = ln(lambda) - kappa d over the buckets
             with n >= fill_table_min_samples and 0 < P < 1;  else algo4_kappa
    ask depth d+ <- max(tick / S, 1/kappa + m - s + algo4_risk_perc) + f
    bid depth d- <- max(tick / S, 1/kappa - m + s + algo4_risk_perc) + f
    A <- round_up(max(S (1 + d+), best_bid + tick))
    B <- round_down(min(S (1 - d-), best_ask - tick))
    G <- (1 + r) / (1 - f)^2 - 1                         ; f = commission_perc, r = exp_ret_min
    if A / B <= 1 + G:
        w <- max(((1 + G) B - A) / (2 + G), 0) + tick
        B <- round_down(B - w);  A <- round_up(A + w)
    if B >= A or (1 - f)^2 A / B - 1 <= r: no quote (net_return_below_bar)
    if the last pair is within the requote band of (B, A) on both sides, younger than
       refresh_seconds and still above the bar: (B, A) <- the last pair      ; hysteresis
    return (B, A)

 8  trend pause: mid fell >= 0.3 % over 600 s -> no buys; rose >= 0.3 % -> no sells
    for side in (sell, buy):
        paused: cancel the side's resting orders
        else:   cancel resting orders farther than band(P) = max(5 ticks, 0.03 % of P)
                from the new price; an order still within the band -> no new order there
 9  sizes: buy = 2 order_size (1 - x_buy), sell = 2 order_size x_sell
           (x_buy counts the open buys, x_sell leaves out the locked sells)
    sell amount <- min(sell size, available inventory - inv_min)
    buy amount  <- min(buy size, available cash / B); none if open buys + q + amount > inv_max
    an amount below 0.1 order_size is not sent
10  send the sell at A, then the buy at B
        demo: paper ledger order "<symbol>-<side>-<round>-<ms>"
        live: signed POST /market/orders/add {type, srcCurrency, dstCurrency, amount, price, clientOrderId}
11  record the round (SQLite store and CSV export) and the Telegram messages
12  wait for the next round start (every time_step seconds)
```

## Key formulas

```math
\begin{aligned}
&\text{fit:} && S_{k+1} = a + b\,S_k + \varepsilon_k,\qquad \theta = -\frac{\ln b}{\Delta t},\qquad \mu = \frac{a}{1-b},\qquad t_{1/2} = \frac{\ln 2}{\theta}\\
&\text{admissible:} && 0 < b < 1,\qquad \frac{b-1}{\mathrm{se}(b)} \le -2.86,\qquad t_{\min} \le t_{1/2} \le t_{\max}\\
&\text{lean:} && m = \mathrm{clip}\Big(\frac{(\mu - S)(1 - e^{-\theta\tau})}{S},\ -L,\ L\Big)\quad\text{(0 when not admissible)}\\
&\text{depths:} && \delta^{\pm} = \max\Big(\frac{\epsilon}{S},\ \frac1\kappa \pm m \mp \chi\,(2x-1) + \varrho\Big) + f\\
&\text{prices:} && A = \big\lceil S(1+\delta^+)\big\rceil_\epsilon,\qquad B = \big\lfloor S(1-\delta^-)\big\rfloor_\epsilon\\
&\text{fee bar:} && (1-f)^2\,\frac AB - 1 > r\ \Longleftrightarrow\ \frac AB > 1 + G,\qquad G = \frac{1+r}{(1-f)^2} - 1
\end{aligned}
```

$`1/\kappa + f`$ is the depth that maximises the expected gain $`\lambda e^{-\kappa\delta}(\delta - f)`$ of
one order, and the Avellaneda–Stoikov half-spread as risk aversion goes to zero. The lean, the skew
$`\chi(2x-1)`$ and the widening $`\varrho`$ are the other terms of the first-order optimal quotes.

## Settings

The defaults are in `market_maker/config.py` (`TRADING_PROFILES`). The demo values are edited per market on
the admin panel and applied at the next round.

| Setting | Meaning | Default |
|---|---|---:|
| `algo4_enabled` | Demo: run MM_algo3 in its own lane on this market | on |
| `algo4_use_alpha` | use the forecast (off: the plain optimal quotes) | on |
| `algo4_window_seconds`, `algo4_sample_seconds` | window length and minimum sample spacing, s | 1800, 10 |
| `algo4_horizon_seconds` | horizon $`\tau`$ of the lean; 0 = the order lifetime | 0 |
| `algo4_halflife_min_seconds`, `algo4_halflife_max_seconds` | admissible half-lives, s | 60, 900 |
| `algo4_kappa` | $`\kappa`$ when the fill table cannot be fitted ($`1/\kappa`$ = 0.25 %) | 400 |
| `algo4_risk_perc` | widening $`\varrho`$ of both depths | 0 |
| `algo4_lean_clip` | largest lean $`L`$, fraction of the price | 0.005 |
| `algo4_sigma_pause` | no quote while the residual exceeds this fraction of the price; 0 = off | 0 |
| `skew_perc` | inventory skew $`\chi`$ at the band edges | 0.002 |
| `commission_perc`, `exp_ret_min` | fee $`f`$ per leg, required net return $`r`$ | 0.0010, 0.0002 |
| `hysteresis_ticks`, `requote_band_perc`, `refresh_seconds` | requote band and the longest a pair is kept | 5, 0.0003, 300 |
| `fill_table_enabled`, `fill_table_max_age_hours`, `fill_table_min_samples` | when the fill table is fitted for $`\kappa`$ | on, 48, 50 |
| `cancel_after_minutes` | order lifetime, min | 3 |
| `trend_window_seconds`, `trend_pause_perc` | trend pause | 600, 0.003 |
| `order_size`, `inv_min`, `inv_max`, `time_step` | sizes, inventory band and round spacing | per market |

The demo runs on five markets, BTCIRT, USDTIRT, ETHIRT, XAUTIRT and TRXIRT, each worker running every
enabled algorithm in its own lane.

## Run, test, deploy

```bash
pip install -r requirements-dev.txt
```

Demo of one market, every enabled algorithm in its own lane:

```bash
cd market_maker && python run.py --profile usdt --mode demo --continuous
```

MM_algo3 alone:

```bash
cd market_maker && python run.py --profile usdt --mode demo --algorithm algo4
```

The tests run from the repository root; they are deterministic and need no network or account:

```bash
python -m pytest -q
```

The local stack (the five demo workers, the dashboard, the admin panel and its control agent):

```bash
docker compose up -d --build --wait
```

On the server, the demo stack runs from `deploy/docker-compose.production.yml`:

```bash
docker compose -f deploy/docker-compose.production.yml up -d --build
```

`--mode real` places real orders with real money, using the Nobitex keys in `market_maker/.env` (see
`market_maker/.env.example`). The live forward test has its own units in `deploy/`
(`market-maker-forward-*.service`), and only the operator starts or stops a live worker.
