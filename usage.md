# Usage

Every way to run or exercise this project, in one place. For what the
project *is* and how it's architected, see [README.md](README.md).

## Quick reference

| Mode | Command | Real market data? | Real orders placed? | Needs live MySQL? |
|---|---|---|---|---|
| [Live trading](#1-live-trading-real-money-real-account) | `python run.py --profile btc\|usdt` | Yes | **Yes -- real money** | **Yes** |
| [Demo / paper trading](#1b-demo-mode-real-market-data-simulated-orders) | `python run.py --profile btc\|usdt --mode demo` | **Yes** | No -- simulated | No |
| [Automated tests](#2-automated-tests-no-account-no-money-no-database) | `pytest -q` | No | No | No |
| [History dashboard](#3-history-dashboard) | `python dashboard.py` | No (reads local CSVs) | No | No |
| [Research notebooks](#4-research-notebooks-read-only) | open in Jupyter | No | No | No |

Note on terminology: `demo_btc.py`/`demo_usdt.py` (this branch's `run.py
--profile btc/usdt --mode real`, the default) were **never** a simulated
account, on this branch or on `main` -- "demo" in their old filenames meant
"example script." `--mode demo` below is a real, new capability, distinct
from that old naming.

---

## 1. Live trading (real money, real account)

**This places real orders on Nobitex using the token in `.env` and writes
to a real MySQL database. There is no dry-run flag -- every invocation
below is live.**

### 1.1 Prerequisites

```sh
cd market_maker
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r ../requirements-dev.txt

cp .env.example .env
# edit .env with a REAL Nobitex API token and REAL MySQL credentials
```

Required variables (`market_maker/.env`, template in `.env.example`):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NOBITEX_API_TOKEN` | yes | none | Your real Nobitex account's API token |
| `MYSQL_HOST` | no | `localhost` | Trade-log DB host |
| `MYSQL_USER` | yes | none | Trade-log DB user |
| `MYSQL_PASSWORD` | yes | none | Trade-log DB password |
| `MYSQL_DATABASE` | no | `trading_bot` | Trade-log DB name (needs `sessions`/`logs` tables) |

Without `NOBITEX_API_TOKEN`, wallet/order calls print "not configured" and
no-op instead of crashing -- but `run()` still requires MySQL to even
start (see [§5](#5-why-theres-no-demo-mode-today)).

### 1.2 Run: BTC, Algorithm 1 (the common case)

```sh
cd market_maker
python run.py --profile btc
```

Equivalent to old `demo_btc.py`: `BTCIRT` pair, `depth_max=10`,
`inv_max=0.000472`, `order_size=0.000015`, 60s between rounds, up to 500
rounds, logs to `/root/Nouri/Algo1/btc_track.csv`.

### 1.3 Run: USDT, Algorithm 1

```sh
cd market_maker
python run.py --profile usdt
```

Equivalent to old `demo_usdt.py`: `USDTIRT` pair, `inv_max=50`,
`order_size=1`, 10s between rounds, up to 50000 rounds, logs to
`/root/Nouri/Algo1/usdt_track.csv`.

### 1.4 Run: Algorithm 2 (not implemented -- will error)

```sh
cd market_maker
python run.py --profile btc --algorithm algo2
```

Starts normally, then raises `NotImplementedError` the first time the
engine asks `Algo2Strategy` for a quote. Expected -- see README.md's
"Algorithm 2" section for what's missing.

### 1.5 All flags

```sh
python run.py --help
```

```text
--profile {btc,usdt}       Currency-pair configuration to run. Required.
--algorithm {algo1,algo2}  Quoting strategy. Default: algo1.
--exchange {nobitex}       Exchange providing real market data. Default: nobitex.
--mode {real,demo}         real: real orders, needs MySQL (default).
                           demo: simulated orders, no MySQL -- see §1b.
```

Adding a new profile/algorithm/exchange is a registry entry in `run.py`,
not a new file -- see README.md's "Extending" section.

### 1.6 Stopping

`Ctrl-C` triggers the `KeyboardInterrupt` path: it saves the current state
to MySQL, then (in `finally`) cancels all open orders on Nobitex before
exiting. Killing the process any other way (`kill -9`, terminal close)
skips this cleanup and can leave open orders on the exchange.

---

## 1b. Demo mode (real market data, simulated orders)

`--mode demo` runs the exact same Algorithm 1 decision logic against the
exact same real, live Nobitex order book -- the only thing that changes is
what happens after a quote is decided:

- **Order book, prices, spreads: real.** `DemoExchangeClient` delegates
  `get_orderbook`/`wallet_balance` straight to the real Nobitex client.
  Nothing about how a quote is computed changes.
- **Orders: simulated, never sent to Nobitex.** Instead of calling
  Nobitex's order-placement endpoint, a simulated order is recorded
  locally and reported back as instantly and fully filled. No real order
  is ever placed; no real money moves.
- **No MySQL required.** `persist_to_db=False` in demo mode -- the engine
  never opens a database connection.
- **Results still saved completely** -- to a separate CSV so demo runs
  never mix with real trading history: `btc_track.csv` → `btc_track_demo.csv`
  (same for USDT). Every column real mode logs (inventory, cash, portfolio
  wealth, bid/ask levels and prices, buy/sell size and status, timestamps)
  is logged identically in demo mode.

```sh
cd market_maker
python run.py --profile btc  --mode demo    # paper trade BTCIRT
python run.py --profile usdt --mode demo    # paper trade USDTIRT
```

**Run both at once:** each `run()` is a self-contained blocking loop with
its own state (own CSV, own session) -- there's no shared state between a
BTC run and a USDT run. Running both simultaneously just means two
separate process invocations, e.g. in two terminals (or backgrounded):

```sh
cd market_maker
python run.py --profile btc  --mode demo &
python run.py --profile usdt --mode demo &
```

**Simplification, stated plainly:** demo fills assume the full requested
amount executes instantly at the quoted price -- no partial fills, no
slippage, no queue position or latency modeling. This is a reasonable
first approximation for exercising the strategy against real prices, not
a fill-probability model. See `exchange/demo.py`'s docstring for the
upgrade path if that matters for your use case.

Only `--algorithm algo1` is meaningful today (the current goal); `algo2`
still raises `NotImplementedError` in demo mode exactly as in real mode.

---

## 2. Automated tests (no account, no money, no database)

**The only mode that's actually safe to run without a real Nobitex account
or MySQL server.** This is how to validate changes to the quoting logic.

```sh
cd market_maker/tests
python3 -m pytest -q
```

60 tests, all pure Python -- no network, no database, nothing touches
Nobitex. See README.md's "Testing" section for what's covered (both
algorithms' strategy logic, env-var config sourcing, `DemoExchangeClient`'s
simulated fills, the `persist_to_db` flag, the dashboard's CSV/session
handling) and what isn't (`run()`, `NobitexExchangeClient.send_order`,
`save_to_db` -- anything that's inherently live).

---

## 3. History dashboard

```sh
cd market_maker
python3 dashboard.py
```

Serves a live-updating dashboard at `http://localhost:8050` covering all
four tracks this project can produce -- BTC/USDT × real/demo -- each
clearly labeled so real trading history is never confused with paper
runs. Read-only against the CSVs; doesn't touch Nobitex or MySQL itself,
and auto-refreshes every 20 seconds so it's safe to leave open while a
real `run.py` or `--mode demo` session is writing to the same file.

Pick a **track** (which CSV) and a **session** (a specific run, or "All
sessions" to compare multiple runs on one chart), then review:

* **Portfolio wealth** over time (colored by session when comparing).
* **Inventory & cash** over time.
* **Selected depth levels & derived spreads** — `Bid Level`/`Ask Level`
  are logged directly; `Bid Spread`/`Ask Spread` are computed from
  already-logged `Best Bid`/`Best Ask`/`Bid Price Sent`/`Ask Price Sent`
  using the same formulas as README.md's Algorithm 1 "Step 5: Reported
  spreads" section -- nothing new is collected, this is a view over
  existing columns.
* **Order activity table** — a sortable, filterable, timestamp-descending
  table of every round's bid/ask levels, prices sent, and buy/sell
  size/status, for line-by-line audit review.

If a track's CSV doesn't exist yet (e.g. you haven't run `--mode demo`
yet), the dashboard says so plainly instead of erroring.

Replaces the old `live_plot.py` (a single live PnL line for one
hardcoded CSV) -- superseded by this dashboard, no functional loss.

---

## 4. Research notebooks (read-only)

```text
market_maker/research/
  algo2.ipynb              empty -- was meant to hold Algorithm 2's implementation
  kappa_estimation.ipynb   estimates Algorithm 2's kappa parameter from historical data
  sigma_estimation.ipynb   estimates Algorithm 2's sigma parameter from historical data
```

Open with `jupyter notebook` or `jupyter lab`. These are exploratory --
`kappa_estimation.ipynb` and `sigma_estimation.ipynb` reference hardcoded
local file paths from the original author's machine and won't run as-is
without pointing them at your own historical trade/order-book CSVs. They
don't place orders or touch any live account.

---

## 5. What demo mode does not do

Kept here for anyone comparing against the state before `--mode demo`
existed, and as an honest limitations list:

- No fill-probability, slippage, or queue-position modeling -- see §1b.
- No simulated wallet balance -- `wallet_balance()` in demo mode returns
  the real account's real balance (read-only, no risk), it isn't tracked
  as a simulated portfolio separately from `inv_initial`/`cash_initial`.
- Algorithm 2 is still just a stub in demo mode, same as real mode.
- No built-in way to run more than one profile in a single process --
  see §1b's "run both at once" for the two-process approach.
