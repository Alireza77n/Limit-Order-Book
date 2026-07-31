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
place/cancel orders → track PnL → persist to CSV/MySQL) is exchange- and
strategy-agnostic by design: `run.py` picks which algorithm runs against
which exchange for which currency pair, and adding a new one of any of
those is a registry entry, not a new file (see §9).

## 2. Architecture

```
                  ┌─────────────────────┐
   run.py  ─────► │  SimpleMarketMaker   │  (the engine: run() loop,
  --algorithm     │      (engine.py)      │   inventory/cash/PnL bookkeeping,
  --exchange      └──────────┬───────────┘   CSV + MySQL persistence)
  --profile                  │
                 ┌───────────┼───────────┐
                 ▼                       ▼
        ┌─────────────────┐      ┌──────────────────┐
        │     Strategy      │      │   ExchangeClient   │
        │ (strategy/base.py) │      │ (exchange/base.py)  │
        └────────┬─────────┘      └─────────┬────────┘
                 │                           │
     ┌───────────┴───────────┐    ┌──────────┴──────────┐
     ▼                       ▼    ▼
Algo1Strategy          Algo2Strategy   NobitexExchangeClient
(implemented,           (stub, raises  (get_orderbook, send_order,
 fully tested)           NotImplementedError) wallet_balance, order status/
                                              cancellation via
                                              order_status.py)
```

- **`Strategy`** (`market_maker/strategy/base.py`): the only method the
  engine relies on is `decide_order(orderbook, inventory) -> quote_dict |
  None`. Anything implementing this can be plugged into `SimpleMarketMaker`
  via its optional `strategy=` constructor argument (defaults to
  `Algo1Strategy` if omitted).
- **`ExchangeClient`** (`market_maker/exchange/base.py`): `get_orderbook`,
  `send_order`, `wallet_balance`, `get_order_list_status`, `cancel_order`,
  `cancel_all_open_orders`, `cancel_old_orders`. `SimpleMarketMaker` takes
  an `exchange=` constructor argument (required) implementing this.
- **`portfolio.py`** (`Nobitex_portfolio`): read-only wallet/portfolio
  valuation helper, independent of the trading engine. **Still
  Nobitex-specific** -- not behind `ExchangeClient` (see §8).
- **`order_status.py`** (`OrderStatusChecker`, `OrderCanceller`): raw
  Nobitex order-status/cancellation HTTP calls, composed inside
  `NobitexExchangeClient` rather than called directly by the engine.
- **`config.py`** (`MMConfig`): a single dataclass holding both strategy
  knobs (`inv_max`, `depth_max`, `commission_perc`, ...) and exchange
  config (`token`, `url_send_order`, ...). Not yet split -- see §8.
- **`run.py`**: the single entry point. `STRATEGIES`/`EXCHANGES` registries
  plus named `PROFILES` (currency-pair configs) select what actually runs.

## 3. Directory structure

```
market_maker/
  engine.py               SimpleMarketMaker: the run() loop and engine glue
  config.py                MMConfig dataclass; loads secrets from .env
  portfolio.py              Nobitex_portfolio: wallet/portfolio valuation
  order_status.py           OrderStatusChecker, OrderCanceller (raw Nobitex calls)
  run.py                    Entry point: --algorithm / --exchange / --profile
  live_plot.py              Dash live PnL chart, reads the run() CSV output
  strategy/
    base.py                 Strategy interface (decide_order contract)
    algo1.py                 Algorithm 1's quoting logic (pure, no I/O)
    algo2.py                 Algorithm 2 stub (raises NotImplementedError)
  exchange/
    base.py                  ExchangeClient interface
    nobitex.py                NobitexExchangeClient (Nobitex HTTP calls)
  tests/                     pytest suite (see §6)
  .env.example               Template for required environment variables
  .env                       Real secrets, gitignored, not committed

algo2_/
  research/                  Parameter-estimation notebooks (kappa, sigma);
                              exploratory, not part of a runnable strategy

reports/                     Local-only analysis notes, gitignored
requirements.txt             Runtime dependencies
requirements-dev.txt         + pytest
```

## 4. Setup

```sh
cd market_maker
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
cd market_maker
python3 run.py --profile btc      # Algorithm 1 live on BTCIRT via Nobitex
python3 run.py --profile usdt     # Algorithm 1 live on USDTIRT via Nobitex
python3 live_plot.py              # live PnL chart, reads the CSV run.py writes
```

`--algorithm` (default `algo1`) and `--exchange` (default `nobitex`) are
optional; `python3 run.py --help` lists all choices. `--algorithm algo2`
currently raises `NotImplementedError` when the engine calls
`decide_order()` -- expected, see §7.

**These place real orders against Nobitex using the token in `.env` and
require a live MySQL server.** There is no dry-run/paper-trading mode
today -- test strategy changes via the unit tests (§6), not by running
`run.py`.

## 6. Testing

```sh
cd market_maker/tests
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

Before this work, `algo2_/Alg2.ipynb` (presumably meant to hold the
Avellaneda-Stoikov implementation) was **empty**. `Kapa Estimation.ipynb`
and `sigma_estimation.ipynb` (now in `algo2_/research/`) estimate the
model's κ (order-arrival intensity decay) and σ (volatility) parameters
from historical data, but reference hardcoded local file paths from the
original author's machine and don't implement the quoting decision itself.

`market_maker/strategy/algo2.py` provides `Algo2Strategy`, a `Strategy`
implementation whose `decide_order()` raises `NotImplementedError` --
scaffolding only, so the engine/tests/`run.py` can already depend on the
`Strategy` interface. Implementing the real model means: writing the
reservation-price and optimal-spread formulas from README.md's "Algorithm
2" section, wiring in κ/σ (static config values or live estimates from the
research notebooks), and adding characterization tests before it's
considered done.

## 8. Known limitations / next steps

- **`config.py` mixes strategy and exchange config** in one `MMConfig`
  dataclass. Splitting it (e.g. `StrategyConfig` + `ExchangeConfig`) was
  deferred to keep each refactor step small; do it before adding a second
  exchange, since exchange-specific fields (`url_send_order`, ...) will
  otherwise leak into every strategy's config.
- **Persistence (`save_to_db`, the MySQL connection in `__init__`) is not
  behind an interface** -- `SimpleMarketMaker` still requires a live MySQL
  server to be constructed at all. Extracting a `TradeLogStore` interface
  (mirroring `ExchangeClient`) is the natural next seam.
- **`portfolio.py`'s `Nobitex_portfolio` is not behind `ExchangeClient`** --
  `run.py`'s `--exchange` flag today only changes order execution/quotes,
  not portfolio valuation. Abstracting portfolio valuation the same way is
  a follow-up, not done here.
- **Git history still contains the original hardcoded secrets** (the
  Nobitex token and MySQL password, from commits predating this work).
  Moving them to `.env` stops new exposure but doesn't erase history --
  rotating both credentials is recommended and is a team/repo-owner
  decision, not something done unilaterally here.
- **No dry-run/paper-trading mode** -- there's currently no way to exercise
  the full `run()` loop without placing real orders and needing a live
  MySQL server.

## 9. Extending

**Add a new algorithm:** implement `Strategy` (`decide_order`) in
`market_maker/strategy/`, add it to `run.py`'s `STRATEGIES` dict (or
construct `SimpleMarketMaker(cfg, portfolio, exchange,
strategy=YourStrategy(cfg))` directly). No engine or exchange changes needed.

**Add a new exchange:** implement `ExchangeClient`
(`market_maker/exchange/base.py`) in `market_maker/exchange/`, add it to
`run.py`'s `EXCHANGES` dict. No engine or strategy changes needed.

**Add a new currency pair:** add an entry to `run.py`'s `PROFILES` dict
with the pair's `MMConfig` values. No new files.
