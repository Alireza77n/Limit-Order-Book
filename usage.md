# Usage

Every way to run or exercise this project, in one place. For what the
project *is* and how it's architected, see [README.md](README.md).

## Quick reference

| Mode | Command | Real market data? | Real orders placed? | Needs live MySQL? |
|---|---|---|---|---|
| [Live trading](#1-live-trading-real-money-real-account) | `python run.py --profile btc\|usdt` | Yes | **Yes -- real money** | **Yes** |
| [Demo / paper trading](#1b-demo-mode-real-market-data-simulated-orders) | `python run.py --profile btc\|usdt --mode demo` | **Yes** | No -- simulated | No |
| [Automated tests](#2-automated-tests-no-account-no-money-no-database) | `pytest -q` | No | No | No |
| [History dashboard](#3-history-dashboard) | `python dashboard.py` | No (reads local SQLite/CSV results) | No | No |
| [Research notebooks](#4-research-notebooks-read-only) | open in Jupyter | No | No | No |
| [Docker](#6-docker) | `docker compose up -d --build --wait` | Yes (demo mode) | No -- simulated | No |

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
| `TELEGRAM_BOT_TOKEN` | no | none | Enables round-by-round Telegram notifications (§1.7) if set together with `TELEGRAM_CHAT_ID` |
| `TELEGRAM_CHAT_ID` | no | none | Telegram chat/channel to notify |

Without `NOBITEX_API_TOKEN`, wallet/order calls print "not configured" and
no-op instead of crashing -- but `run()` still requires MySQL to even
start (see [§5](#5-what-demo-mode-does-not-do)). Without both Telegram
variables, notifications are silently skipped -- they're optional and
never required to run the bot.

### 1.2 Run: BTC, Algorithm 1 (the common case)

```sh
cd market_maker
python run.py --profile btc
```

Equivalent to old `demo_btc.py`: `BTCIRT` pair, `depth_max=10`,
`inv_max=0.000472`, `order_size=0.000015`, 60s between rounds, up to 500
rounds, logs to `market_maker/results/btc_track.csv`.

### 1.3 Run: USDT, Algorithm 1

```sh
cd market_maker
python run.py --profile usdt
```

Equivalent to old `demo_usdt.py`: `USDTIRT` pair, `inv_max=50`,
`order_size=1`, 10s between rounds, up to 50000 rounds, logs to
`market_maker/results/usdt_track.csv`.

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
--continuous               run until intentionally stopped; ignore profile
                           round/time-horizon limits.
```

Adding a new currency pair is a `config.TRADING_PROFILES` entry; adding a
new algorithm/exchange is a `run.py` registry entry -- neither needs a new
file. See README.md's "Extending" section.

### 1.6 Stopping

`Ctrl-C` triggers the `KeyboardInterrupt` path: it saves the current state
to MySQL, then (in `finally`) cancels all open orders on Nobitex before
exiting. Killing the process any other way (`kill -9`, terminal close)
skips this cleanup and can leave open orders on the exchange.

### 1.7 Telegram notifications & transaction-cost tracking

Optional, off by default. Set both `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` in `.env` to get:

- A **startup message** when `run()` begins: symbol, algorithm, commission
  rate, session id.
- Persisted paper-order submission and lifecycle messages. Each round's
  summary message is the parent thread; submitted-order, fill, and
  cancellation updates for that round reply to it when Telegram returns a
  usable message ID (see §6.8 for the exact reason vocabulary).
- A sustained **no-quote alert** after the configured count or duration,
  containing the reason, candidate spreads/prices, commission,
  `exp_ret_min`, and required threshold. Cooldown and state-change rules
  prevent repeated alert spam; a later valid quote resets the episode and
  can emit a recovery message.

Every notification opens with a prominent
🧪 **DEMO MODE** / 🔴 **LIVE MODE** badge as its first line, so real and
paper-trading messages can never be mistaken for each other while
scrolling a chat that has both. Section headers (Quote / Market /
Portfolio / Orders / Costs) keep each round's message scannable rather
than one dense paragraph. All timestamps (Telegram messages, CSV rows,
console logs) are in **Asia/Tehran local time** explicitly, regardless of
the host or container's own system timezone -- Docker containers default
to UTC, so this is computed with `pytz`, not left to the OS.

Cumulative Demo fees are stored in the locked
`market_maker/results/paper_fee_scopes.json` document. It records pair,
session, and all-symbol totals and uses per-order cumulative high-watermarks
so recovered partial fills cannot be charged twice.

Telegram delivery is best-effort: a network hiccup is persisted as pending
and retried without mutating paper accounting.

---

## 1b. Demo mode (real market data, simulated orders)

`--mode demo` runs the exact same Algorithm 1 decision logic against the
exact same real, live Nobitex order book -- the only thing that changes is
what happens after a quote is decided:

- **Order book, prices, spreads: real.** `DemoExchangeClient` delegates
  only public order-book reads to the real Nobitex client. It does not
  delegate wallet access. Nothing about how a quote is computed changes.
- **Orders: simulated, never sent to Nobitex.** Instead of calling
  Nobitex's order-placement endpoint, an open paper order is recorded in
  the canonical local ledger. Configured level-1 delay/fraction, price
  touch, explicit trade evidence, optional queue-ahead, partial fills,
  stale cancellation, and shutdown cancellation drive its lifecycle. No
  real order is ever placed; no real money moves.
- **Transaction cost: simulated realistically.** Each simulated fill's fee
  is `amount * commission_perc` (buys, base currency) or
  `amount * price * commission_perc` (sells, quote currency) -- the same
  `commission_perc` the profile already uses for the spread-feasibility
  check, not a hardcoded zero. See §1.7 for how costs are reported and
  accumulated.
- **Inventory starts from a configurable value, not a hardcoded one.**
  `run.py --mode demo` sets `inv_initial` from each profile's own
  `demo_inv_initial` (an `MMConfig` field, default `0.0`) -- paper trading
  starts flat by default, but BTC and USDT can each be tuned independently
  by editing `TRADING_PROFILES["btc"]["demo_inv_initial"]` /
  `TRADING_PROFILES["usdt"]["demo_inv_initial"]` **in `config.py`** (not
  `run.py` -- all per-pair values, including this one, live in one place).
  Real mode never reads this field -- it always uses the profile's real
  `inv_initial`. `cash_initial` is unchanged in demo mode (you still need
  starting capital to size orders against).
- **No MySQL required.** `persist_to_db=False` in demo mode -- the engine
  never opens a database connection.
- **Results are durable and bounded.** SQLite is the canonical append/upsert
  store (`btc_track_demo.sqlite3` / `usdt_track_demo.sqlite3`). The familiar
  Demo CSV files are atomic, bounded dashboard exports rather than an
  unbounded history rewrite. Paper state is persisted separately and old
  terminal orders are archived before hot-state pruning.
- **Profitability protection remains authoritative.** If Algorithm 1 finds
  no candidate above `commission_perc + exp_ret_min`, Demo records a
  `no_quote` round and sends no paper order. The monitor records the exact
  reason and candidate diagnostics; it never forces a quote.

```sh
cd market_maker
python run.py --profile btc  --mode demo    # paper trade BTCIRT
python run.py --profile usdt --mode demo    # paper trade USDTIRT
```

**Run both natively:** each `run()` is a blocking worker with isolated
per-symbol order state. Without `DEMO_SESSION_FILE`, native invocations
generate their own session IDs. Running both simultaneously means two
process invocations, for example:

```sh
cd market_maker
python run.py --profile btc  --mode demo &
python run.py --profile usdt --mode demo &
```

Or use Docker Compose (§6), which runs both workers plus the shared-session
initializer and dashboard with one command.

The simulator remains an approximation, not an exchange matching engine.
Its level-1 fast-fill and optional queue model are deterministic operating
assumptions; they do not model every source of latency, slippage, or queue
priority on Nobitex.

Only `--algorithm algo1` is meaningful today (the current goal); `algo2`
still raises `NotImplementedError` in demo mode exactly as in real mode.

---

## 2. Automated tests

From the repository root:

```sh
python3 -m pytest -q
```

The current suite has 171 deterministic tests. It uses mocks and replay
fixtures for exchange, Telegram, lifecycle, restart, accounting, retention,
dashboard, and two-symbol end-to-end behavior. It does not require a
Nobitex account, public network access, Telegram, or MySQL, and it does not
place real orders.

---

## 3. History dashboard

```sh
cd market_maker
python3 dashboard.py
```

Serves a live-updating dashboard at `http://localhost:8050` covering all
four tracks this project can produce -- BTC/USDT × real/demo -- each
clearly labeled so real trading history is never confused with paper
runs. For Demo tracks it prefers the sibling SQLite result store and
falls back to CSV; Real tracks remain CSV-compatible. It does not touch
Nobitex or MySQL and auto-refreshes every 20 seconds.

**Layout:** a toolbar card (track picker, session picker, a colored
real/demo status badge with icon + label -- never color alone) sits above
five chart cards and an activity-table card, all on a shared light
surface with consistent spacing, a system-UI font, and a fixed
categorical color palette (never Plotly's default color cycle). Every
line chart uses `hovermode="x unified"` for a crosshair-style tooltip.

Pick a **track** (which CSV) and a **session** (a specific run, or "All
sessions (compare)" to overlay multiple runs, colored by session), then
review five separate charts -- each on its own single axis, since mixing
differently-scaled measures on one axis (e.g. BTC amounts vs. IRT cash,
or depth-level integers vs. price spreads) makes both unreadable:

* **Portfolio wealth** over time (colored by session when comparing).
* **Inventory** over time (base currency, e.g. BTC amount).
* **Cash** over time (quote currency, e.g. IRT amount) -- its own chart,
  not sharing an axis with inventory.
* **Selected depth levels** — `Bid Level`/`Ask Level`, logged directly.
* **Quoted spreads** — `Bid Spread`/`Ask Spread`, computed from
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

- The deterministic paper fill/queue model is not a complete exchange
  matching or slippage model -- see §1b.
- No authenticated wallet access. Demo portfolio state comes from the
  configured initial cash/inventory and canonical paper ledger.
- Algorithm 2 is still just a stub in demo mode, same as real mode.
- No built-in way to run more than one profile in a single process --
  see §1b's "run both at once" for the two-process approach, or §6 for
  the Docker Compose equivalent.

---

## 6. Docker

Docker Compose runs the whole operational Demo stack: one session
initializer, continuous BTC and USDT paper workers, and the dashboard.
All services use the root `Dockerfile` and share the results bind mount.

### 6.1 Requirements

- Docker Desktop or Docker Engine with Compose v2.
- Outbound access to Nobitex's public order-book endpoint.
- Port 8050 available for the dashboard.
- No Nobitex order token or MySQL server is required for Demo mode.

Run every command below from the repository root:

```sh
cd /Users/parsaoryani/Documents/Limit-Order-Book
```

`market_maker/.env` is optional. Create it only when you want Telegram
notifications or need to override documented environment settings:

```sh
cp market_maker/.env.example market_maker/.env
# Edit TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID if notifications are wanted.
# Do not add a real Nobitex order token for a Demo-only deployment.
```

The file is supplied at container start and excluded from the image by
`.dockerignore`.

### 6.2 Build and start the entire project

Build all images without starting containers:

```sh
docker compose build
```

Build changed images, start the full stack in the background, and wait for
health checks:

```sh
docker compose up -d --build --wait
```

Expected services:

| Service | Command/role | Expected state | Port |
|---|---|---|---|
| `session-init` | creates or reuses the shared stack session | exited (0) | none |
| `btc-demo` | `run.py --profile btc --mode demo --continuous` | healthy | none |
| `usdt-demo` | `run.py --profile usdt --mode demo --continuous` | healthy | none |
| `dashboard` | `dashboard.py` | healthy | 8050 |

Check them:

```sh
docker compose ps -a
```

Open the dashboard at <http://localhost:8050>.

### 6.3 Monitor the running stack

Follow all application logs:

```sh
docker compose logs -f btc-demo usdt-demo dashboard
```

Follow one worker or show its last 200 lines:

```sh
docker compose logs -f usdt-demo
docker compose logs --tail 200 btc-demo
```

Watch CPU, memory, network, and process counts:

```sh
docker stats
```

Confirm health and restart state:

```sh
docker compose ps
```

Compose container names include the project-directory name and can vary.
Use `docker compose ps -q btc-demo` when a command needs the exact ID:

```sh
docker inspect "$(docker compose ps -q btc-demo)" \
  --format 'health={{.State.Health.Status}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}'
```

Do not open a live bind-mounted SQLite WAL directly from macOS while the
containers are writing. On Docker Desktop, inspect it from a Linux
container instead, or stop the workers first. The dashboard already reads
the stores safely from inside the Compose environment. Never move, rename,
copy over, or delete a live `.sqlite3`, `-wal`, or `-shm` file; stop both
workers first and confirm they have exited before handling those files.

### 6.4 Results and retention

Normal Compose writes to `market_maker/results/`:

```text
stack_session.json                         shared BTC/USDT session
btc_track_demo.sqlite3                     canonical BTC round store
usdt_track_demo.sqlite3                    canonical USDT round store
btc_track_demo.csv / usdt_track_demo.csv   bounded dashboard exports
paper_<session>_<symbol>.json               hot paper ledger/account
paper_<session>_<symbol>_archive*.jsonl     rotated terminal-order archives
paper_fee_scopes.json                       fee totals/high-watermarks
```

Default limits are 10,000 rows in each CSV export, 100,000 durable result
rows per symbol store, 1,000 hot terminal paper orders, and 10,000,000
bytes per archive segment. Adjust the corresponding `MMConfig` fields in
`market_maker/config.py` if another operational envelope is required.

USDT may legitimately show `no_quote` for every round. Algorithm 1 refuses
paper orders when no candidate clears the configured profitability
threshold. The dashboard exposes the consecutive count, duration, reason,
alert state, and exact candidate diagnostics. A valid quote resets the
episode.

### 6.5 Restart and session behavior

Restart one worker while preserving the current shared session:

```sh
docker compose restart usdt-demo
docker compose ps usdt-demo
```

The worker resumes its persisted round sequence and no-quote episode.
Starting a duplicate worker for the same symbol fails closed through the
worker lease.

Stopping every worker and later starting the full stack creates a new
stack session. Previous sessions remain queryable in SQLite/dashboard
history.

### 6.6 Credential-empty validation stack

For audits and safe live-public-data testing, combine the normal Compose
file with the validation override:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.validation.yml \
  up -d --build --wait
```

This explicitly empties Nobitex order, Telegram, and MySQL credentials and
writes to `.docker-validation-results/` instead of the normal results
directory.

Use the same file combination for status, logs, stop, and teardown:

```sh
docker compose -f docker-compose.yml -f docker-compose.validation.yml ps -a
docker compose -f docker-compose.yml -f docker-compose.validation.yml logs -f
docker compose -f docker-compose.yml -f docker-compose.validation.yml stop
docker compose -f docker-compose.yml -f docker-compose.validation.yml down
```

### 6.7 Stop and remove the stack

Gracefully stop workers while retaining the containers:

```sh
docker compose stop
```

SIGTERM/SIGINT is routed through typed coordinated cleanup. Local open paper
orders are cancelled with the exact reason `coordinated_stack_shutdown`,
final state is persisted, worker leases are released, and workers exit
normally. An unexpected worker exception does **not** cancel open paper
orders; Docker can restart the same session and restore/reconcile them.

Stop and remove containers and the Compose network while preserving the
bind-mounted result files:

```sh
docker compose down
```

Rebuild after code or dependency changes:

```sh
docker compose up -d --build --wait
```

**No real (`--mode real`) service is defined.** The supplied Compose stack
cannot place real Nobitex orders and does not connect to production MySQL.
Real deployment requires a separate, deliberately reviewed service and is
outside this Demo stack.

The image sets `PYTHONUNBUFFERED=1`, so worker output appears promptly in
`docker compose logs`.

### 6.8 Demo Telegram order messages and cancellation reasons

Each completed Demo round sends one main summary containing the quote,
market, portfolio, submitted/not-submitted orders, and fee scopes. `BUY
OPENED`, `SELL OPENED`, partial-fill, fill, and cancellation messages reply
to that round summary, so one round stays together in Telegram.

| Reason | Meaning |
|---|---|
| `stale_timeout` | configured paper-order lifetime expired |
| `manual_cancel` | operator/API explicitly cancelled one paper order |
| `session_end` | finite Demo round limit or time horizon ended normally |
| `coordinated_stack_shutdown` | installed SIGTERM/SIGINT handler initiated cleanup |
| `worker_fatal_exit` | logged exit classification; active orders are preserved for same-session retry, not automatically cancelled |

Legacy state may contain the old `stack_shutdown` value. New code never
creates it. Structured cancellation logs include the initiator, call site,
session, symbol, process ID, timestamp, order ID, and released lock
value/unit.

For a buy, released lock is quote-currency cash: `remaining amount × limit
price`. Cancelling a 1 USDT paper buy at 90 IRT therefore releases `90.00
IRT`. For a sell, released lock is base-currency inventory and is reported
as USDT or BTC.
