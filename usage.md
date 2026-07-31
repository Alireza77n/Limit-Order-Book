# Usage

Every way to run or exercise this project, in one place. For what the
project *is* and how it's architected, see [README.md](README.md).

## Quick reference

| Mode | Command | Real Nobitex account? | Real money? | Needs live MySQL? |
|---|---|---|---|---|
| [Live trading](#1-live-trading-real-money-real-account) | `python run.py --profile btc\|usdt` | **Yes** | **Yes** | **Yes** |
| [Automated tests](#2-automated-tests-no-account-no-money-no-database) | `pytest -q` | No | No | No |
| [Live PnL chart](#3-live-pnl-chart) | `python live_plot.py` | No (reads a local CSV) | No | No |
| [Research notebooks](#4-research-notebooks-read-only) | open in Jupyter | No | No | No |

**There is no demo/paper/sandbox account mode.** This is true both here and
on `main`: `demo_btc.py`/`demo_usdt.py` (this branch's `run.py --profile
btc/usdt`) were never a simulated account -- "demo" in their old filenames
meant "example script," not "demo account." Every run against Nobitex uses
one real API token and places real orders. See
[§5](#5-why-theres-no-demo-mode-today) if you want one added.

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
--profile {btc,usdt}     Currency-pair configuration to run. Required.
--algorithm {algo1,algo2}  Quoting strategy. Default: algo1.
--exchange {nobitex}       Exchange to trade on. Default: nobitex.
```

Adding a new profile/algorithm/exchange is a registry entry in `run.py`,
not a new file -- see README.md's "Extending" section.

### 1.6 Stopping

`Ctrl-C` triggers the `KeyboardInterrupt` path: it saves the current state
to MySQL, then (in `finally`) cancels all open orders on Nobitex before
exiting. Killing the process any other way (`kill -9`, terminal close)
skips this cleanup and can leave open orders on the exchange.

---

## 2. Automated tests (no account, no money, no database)

**The only mode that's actually safe to run without a real Nobitex account
or MySQL server.** This is how to validate changes to the quoting logic.

```sh
cd market_maker/tests
python3 -m pytest -q
```

46 tests, all pure Python -- no network, no database, nothing touches
Nobitex. See README.md's "Testing" section for what's covered (both
algorithms' strategy logic, env-var config sourcing) and what isn't
(`run()`, `send_order`, `save_to_db` -- anything that's inherently live).

---

## 3. Live PnL chart

```sh
cd market_maker
python3 live_plot.py
```

Reads whatever CSV a `run.py` session wrote (`usdt_track.csv` by default
in the current script) and serves a live-updating Dash chart at
`http://localhost:8050`. Read-only against the CSV; doesn't touch Nobitex
or MySQL itself. Only useful while (or after) a real `run.py` session has
produced data.

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

## 5. Why there's no demo mode today

Two separate things would need to exist for a real paper-trading mode,
neither does yet:

1. **A simulated exchange** -- an `ExchangeClient` implementation that
   fakes fills against a recorded/replayed order book instead of calling
   Nobitex, so `--exchange demo` (or similar) could sit next to
   `--exchange nobitex` with zero engine changes (the `ExchangeClient`
   interface already supports this -- see README.md's "Extending"
   section).
2. **A persistence path that doesn't require MySQL** -- `SimpleMarketMaker.__init__`
   unconditionally opens a real MySQL connection today, so even a fully
   simulated exchange couldn't be constructed without one.

Both are additive (new code, new classes) and wouldn't touch Algorithm 1's
existing quoting logic or the real Nobitex path at all. Not built here --
ask if you want this scoped as its own piece of work.
