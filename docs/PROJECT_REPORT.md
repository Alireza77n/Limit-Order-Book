# Project Report: Crypto Market-Making Bot

Architecture, setup, usage, and current status. For the mathematical
description of the quoting strategies themselves, see [../README.md](../README.md).

## 1. What this is

A crypto market-making bot with two intended strategies:

- **Algorithm 1** — a deterministic, depth-aware, inventory-skewed quoting
  strategy. Implemented, tested, and runnable live against Nobitex.
- **Algorithm 2** — the Avellaneda-Stoikov optimal market-making model.
  **Not implemented.** Only documented in README.md and explored in
  parameter-estimation notebooks (`algo2_/research/`). See §7.

The engine that runs a strategy live (fetch order book → decide quotes →
place/cancel orders → track PnL → persist to CSV/MySQL) is exchange-agnostic
by design: today it talks to Nobitex, and swapping exchanges means writing
one new class, not touching strategy or engine code.

## 2. Architecture

```
                     ┌─────────────────────┐
demo_btc.py  ─────►  │  SimpleMarketMaker   │  (the engine: run() loop,
demo_usdt.py         │      (Algo1.py)      │   inventory/cash/PnL bookkeeping,
                     └──────────┬───────────┘   CSV + MySQL persistence)
                                │
                 ┌──────────────┼──────────────┐
                 ▼                             ▼
        ┌─────────────────┐           ┌──────────────────┐
        │     Strategy      │           │   ExchangeClient   │
        │ (strategy/base.py) │           │ (exchange/base.py)  │
        └────────┬─────────┘           └─────────┬────────┘
                 │                                │
     ┌───────────┴───────────┐          ┌─────────┴──────────┐
     ▼                       ▼          ▼                    
Algo1Strategy          Algo2Strategy   NobitexExchangeClient
(implemented,           (stub, raises  (get_orderbook, send_order,
 fully tested)           NotImplementedError) wallet_balance, order status/
                                              cancellation via
                                              Order_Statuse.py)
```

- **`Strategy`** (`algo1_/strategy/base.py`): the only method the engine
  relies on is `decide_order(orderbook, inventory) -> quote_dict | None`.
  Anything implementing this can be plugged into `SimpleMarketMaker` via
  its optional `strategy=` constructor argument (defaults to
  `Algo1Strategy` if omitted, so existing call sites are unaffected).
- **`ExchangeClient`** (`algo1_/exchange/base.py`): `get_orderbook`,
  `send_order`, `wallet_balance`, `get_order_list_status`, `cancel_order`,
  `cancel_all_open_orders`, `cancel_old_orders`. `SimpleMarketMaker` takes
  an `exchange=` constructor argument (required) implementing this.
- **`Portfolio.py`** (`Nobitex_portfolio`): read-only wallet/portfolio
  valuation helper, independent of the trading engine.
- **`Order_Statuse.py`** (`OrderStatusChecker`, `OrderCanceller`): raw
  Nobitex order-status/cancellation HTTP calls, composed inside
  `NobitexExchangeClient` rather than called directly by the engine.
- **`Config.py`** (`MMConfig`): a single dataclass holding both strategy
  knobs (`inv_max`, `depth_max`, `commission_perc`, ...) and exchange
  config (`token`, `url_send_order`, ...). Not yet split -- see §8.

## 3. Directory structure

```
algo1_/
  Algo1.py               SimpleMarketMaker: the run() loop and engine glue
  Config.py               MMConfig dataclass; loads secrets from .env
  Portfolio.py             Nobitex_portfolio: wallet/portfolio valuation
  Order_Statuse.py         OrderStatusChecker, OrderCanceller (raw Nobitex calls)
  demo_btc.py              Entry point: runs Algorithm 1 on BTCIRT
  demo_usdt.py             Entry point: runs Algorithm 1 on USDTIRT
  live_plot.py             Dash live PnL chart, reads the run() CSV output
  strategy/
    base.py                Strategy interface (decide_order contract)
    algo1_strategy.py       Algorithm 1's quoting logic (pure, no I/O)
    algo2_strategy.py       Algorithm 2 stub (raises NotImplementedError)
  exchange/
    base.py                 ExchangeClient interface
    nobitex.py               NobitexExchangeClient (Nobitex HTTP calls)
  tests/                    pytest suite (see §6)
  .env.example              Template for required environment variables
  .env                      Real secrets, gitignored, not committed

algo2_/
  research/                 Parameter-estimation notebooks (kappa, sigma);
                             exploratory, not part of a runnable strategy

reports/                    Local-only analysis notes, gitignored
requirements.txt            Runtime dependencies
requirements-dev.txt        + pytest
```

## 4. Setup

```sh
cd algo1_
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r ../requirements-dev.txt

cp .env.example .env
# edit .env: fill in NOBITEX_API_TOKEN and MYSQL_* credentials
```

Required environment variables (`.env`, see `.env.example`):

| Variable            | Required | Default       | Purpose                          |
|---------------------|----------|---------------|-----------------------------------|
| `NOBITEX_API_TOKEN`  | yes (for live trading) | none | Nobitex API auth token |
| `MYSQL_HOST`         | no       | `localhost`   | Trade-log DB host                 |
| `MYSQL_USER`         | yes (for `run()`/`save_to_db`) | none | Trade-log DB user   |
| `MYSQL_PASSWORD`     | yes (for `run()`/`save_to_db`) | none | Trade-log DB password |
| `MYSQL_DATABASE`     | no       | `trading_bot` | Trade-log DB name                 |

Without `NOBITEX_API_TOKEN`, wallet/order endpoints print "not configured"
and no-op rather than crash. Without valid MySQL credentials, `run()` will
fail to connect (a live MySQL server with the `sessions`/`logs` tables is
a hard requirement of the current engine -- see §8).

## 5. Usage

```sh
cd algo1_
python3 demo_btc.py     # runs Algorithm 1 live on BTCIRT
python3 demo_usdt.py    # runs Algorithm 1 live on USDTIRT
python3 live_plot.py    # live PnL chart, reads the CSV demo_*.py writes
```

**These place real orders against Nobitex using the token in `.env` and
require a live MySQL server.** There is no dry-run/paper-trading mode
today -- test strategy changes via the unit tests (§6), not by running
the demos.

## 6. Testing

```sh
cd algo1_/tests
python3 -m pytest -q
```

46 tests, all exercising pure/I-O-free code (no network, no database):

- `test_algo1_baseline.py` — characterization tests pinning Algorithm 1's
  behavior through `SimpleMarketMaker`'s delegating methods (the original
  regression net from before the strategy/exchange split).
- `test_strategy_algo1.py` — the same scenarios against `Algo1Strategy`
  directly.
- `test_strategy_algo2.py` — pins the Algo2 stub's contract (constructible,
  raises `NotImplementedError`).
- `test_config_env.py` — env-var sourcing for the token/MySQL credentials.

**Not covered, and not safely testable as currently written:** `run()`,
`send_order`, `wallet_balance`, `save_to_db` -- these perform real trading
side effects or require a live MySQL connection. `SimpleMarketMaker.__init__`
still opens a real MySQL connection unconditionally (tests construct it via
`object.__new__` to bypass this -- see `tests/helpers.py`).

## 7. Algorithm 2 status

Before this branch, `algo2_/Alg2.ipynb` (presumably meant to hold the
Avellaneda-Stoikov implementation) was **empty**. `Kapa Estimation.ipynb`
and `sigma_estimation.ipynb` (now in `algo2_/research/`) estimate the
model's κ (order-arrival intensity decay) and σ (volatility) parameters
from historical data, but reference hardcoded local file paths from the
original author's machine and don't implement the quoting decision itself.

`algo1_/strategy/algo2_strategy.py` provides `Algo2Strategy`, a `Strategy`
implementation whose `decide_order()` raises `NotImplementedError` --
scaffolding only, so the engine/tests can already depend on the `Strategy`
interface. Implementing the real model means: writing the reservation-price
and optimal-spread formulas from README.md's "Algorithm 2" section, wiring
in κ/σ (static config values or live estimates from the research notebooks),
and adding characterization tests before it's considered done.

## 8. Known limitations / next steps

- **`Config.py` mixes strategy and exchange config** in one `MMConfig`
  dataclass. Splitting it (e.g. `StrategyConfig` + `ExchangeConfig`) was
  deferred to keep this refactor small; do it before adding a second
  exchange, since exchange-specific fields (`url_send_order`, ...) will
  otherwise leak into every strategy's config.
- **Persistence (`save_to_db`, the MySQL connection in `__init__`) is not
  behind an interface** -- `SimpleMarketMaker` still requires a live MySQL
  server to be constructed at all. Extracting a `TradeLogStore` interface
  (mirroring `ExchangeClient`) is the natural next seam.
- **Git history still contains the original hardcoded secrets** (the
  Nobitex token and MySQL password, from commits predating this branch).
  Moving them to `.env` stops new exposure but doesn't erase history --
  rotating both credentials is recommended and is a team/repo-owner
  decision, not something done unilaterally here.
- **The `algo1_/` directory name predates this refactor** and now holds
  shared engine/exchange code used by both algorithms, not just Algorithm
  1's. Left as-is to avoid a large, high-blast-radius rename; worth
  revisiting once Algorithm 2 is a real, runnable implementation.
- **No dry-run/paper-trading mode** -- there's currently no way to exercise
  the full `run()` loop without placing real orders and needing a live
  MySQL server.

## 9. Extending

**Add a new algorithm:** implement `Strategy` (`decide_order`) in
`algo1_/strategy/`, construct `SimpleMarketMaker(cfg, portfolio, exchange,
strategy=YourStrategy(cfg))`. No engine or exchange changes needed.

**Add a new exchange:** implement `ExchangeClient`
(`algo1_/exchange/base.py`) in `algo1_/exchange/`, construct
`SimpleMarketMaker(cfg, portfolio, exchange=YourExchangeClient(...))`. No
engine or strategy changes needed.
