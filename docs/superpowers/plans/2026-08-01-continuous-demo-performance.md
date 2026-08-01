# Continuous Demo Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make continuous Demo persistence and scheduling bounded, add restart-safe no-quote operations without changing Algorithm 1, and prove the result with deterministic tests and a ten-minute safe Docker soak.

**Architecture:** Add two deep modules: `DemoResultStore` owns SQLite round/report persistence and bounded CSV compatibility exports; `NoQuoteMonitor` owns per-session/symbol episode state and alert decisions. Extend `PaperLedger` with archive-before-prune hot retention and per-order fee high-watermarks, and make the engine loop schedule against an injected monotonic deadline.

**Tech Stack:** Python 3.11, stdlib `sqlite3`, JSON/JSONL, pandas compatibility exports, pytest, Docker Compose, Dash, existing strict Telegram transport.

## Global Constraints

- Algorithm 1 selection behavior must remain unchanged from `main`; observer calls only.
- Demo Mode must have no real order/cancel/wallet/MySQL capability or credentials.
- Every production change follows RED → observed failure → minimal GREEN → broader verification.
- Active orders and pending notification records are never pruned.
- Archive data is durable before hot-state deletion.
- CSV remains readable but is bounded and is not canonical mutable storage.
- No push; create focused local commits only after verification.

---

### Task 1: SQLite Demo Result Store

**Files:**
- Create: `market_maker/result_store.py`
- Create: `market_maker/tests/test_result_store.py`
- Modify: `market_maker/config.py`

**Interfaces:**
- Produces: `DemoResultStore(db_path, columns, csv_export_path=None, csv_export_max_rows=10000)`
- Produces: `append_round(row)`, `update_order_rows(order_fields, financial_fields)`, `load_view(limit=None)`, `export_bounded_csv()`, `compact(retain_rows)`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_append_and_indexed_lifecycle_update_preserve_prior_rows(tmp_path):
    store = DemoResultStore(tmp_path/'btc.sqlite3', COLUMNS)
    store.append_round(row(round_no=1, buy_id='buy-1'))
    store.append_round(row(round_no=2, buy_id='buy-2'))
    store.update_order_rows({'buy-1': {'Buy Status': 'filled'}}, {'buy-1': {'cash_current': 90.0}})
    view = store.load_view()
    assert list(view['Round']) == [1, 2]
    assert view.loc[0, 'Buy Status'] == 'filled'
    assert view.loc[0, 'cash_current'] == 90.0

def test_bounded_export_and_compaction(tmp_path):
    store = DemoResultStore(tmp_path/'btc.sqlite3', COLUMNS, tmp_path/'btc.csv', 3)
    for number in range(1, 7):
        store.append_round(row(round_no=number))
    store.export_bounded_csv()
    assert list(pd.read_csv(tmp_path/'btc.csv')['Round']) == [4, 5, 6]
    assert store.compact(4) == 2
    assert list(store.load_view()['Round']) == [3, 4, 5, 6]
```

- [ ] **Step 2: Run RED**

Run: `pytest -q market_maker/tests/test_result_store.py`
Expected: collection failure because `result_store` does not exist.

- [ ] **Step 3: Implement minimal transactional store**

Use `sqlite3.connect`, `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, a `rounds` table with `session_id`, `symbol`, `round_no`, `payload_json`, `buy_order_id`, `sell_order_id`, unique `(session_id,symbol,round_no)`, and indexes on both order IDs. Update JSON payloads only for rows selected by indexed order ID. Export only the newest configured rows through a temporary file plus `os.replace`.

- [ ] **Step 4: Add idempotent legacy CSV migration tests and implementation**

```python
store.import_legacy_csv_once(csv_path)
store.import_legacy_csv_once(csv_path)
assert len(store.load_view()) == original_rows
```

Store a SHA-256 fingerprint in `metadata`; ignore an already imported fingerprint.

- [ ] **Step 5: Run GREEN and commit**

Run: `pytest -q market_maker/tests/test_result_store.py`
Expected: all pass.

Commit: `feat: add bounded sqlite demo result store`

---

### Task 2: No-Quote Monitor and Telegram Decisions

**Files:**
- Create: `market_maker/no_quote.py`
- Create: `market_maker/tests/test_no_quote_monitor.py`
- Modify: `market_maker/telegram_lifecycle.py`
- Modify: `market_maker/tests/test_telegram_lifecycle.py`
- Modify: `market_maker/config.py`

**Interfaces:**
- Produces: `NoQuoteMonitor(store, session_id, symbol, alert_rounds, alert_seconds, cooldown_seconds)`.
- Produces: immutable `NoQuoteDecision(kind, snapshot, pending)` and `NoQuoteSnapshot`.
- Adds reporter methods `no_quote_alert(decision)` and `no_quote_recovered(decision)` returning strict delivery success.

- [ ] **Step 1: Write failing count/reset/isolation tests**

```python
btc.record_no_quote(1, T0, 'profitability_threshold', diagnostics)
btc.record_no_quote(2, T1, 'profitability_threshold', diagnostics)
assert btc.snapshot().consecutive_count == 2
btc.record_quote(3, T2)
assert btc.snapshot().consecutive_count == 0
assert usdt.snapshot().consecutive_count == 0
```

- [ ] **Step 2: Write failing trigger/cooldown/state-change/restart tests**

Assert the threshold round yields `kind='alert'`, the next round yields no delivery, cooldown expiry yields `kind='reminder'`, reason change yields `kind='state_change'`, persisted reload does not duplicate delivered alerts, and quote recovery yields one `kind='recovered'` decision.

- [ ] **Step 3: Run RED**

Run: `pytest -q market_maker/tests/test_no_quote_monitor.py`
Expected: collection failure because `no_quote` does not exist.

- [ ] **Step 4: Implement monitor persistence in SQLite**

Create a `no_quote_state` table keyed by `(session_id,symbol)`. Store timestamps as UTC ISO text, diagnostics as deterministic JSON, pending kind/payload, last alert timestamp/reason/message ID, and episode counters. `mark_delivered` clears pending only after a message ID.

- [ ] **Step 5: Add strict Telegram formatting/retry behavior**

Messages include symbol, count, duration, reason, commission, `exp_ret_min`, required threshold, and candidate bid/ask/spread lines. Disabled Telegram leaves no pending delivery; configured failure preserves pending state for later retry.

- [ ] **Step 6: Run GREEN and commit**

Run: `pytest -q market_maker/tests/test_no_quote_monitor.py market_maker/tests/test_telegram_lifecycle.py`
Expected: all pass.

Commit: `feat: monitor sustained no-quote episodes`

---

### Task 3: Paper Hot Retention and Fee High-Watermarks

**Files:**
- Modify: `market_maker/paper.py`
- Modify: `market_maker/run.py`
- Modify: `market_maker/config.py`
- Create: `market_maker/tests/test_paper_retention.py`
- Modify: `market_maker/tests/test_paper_accounting.py`

**Interfaces:**
- Extends `PaperLedger(..., hot_terminal_order_limit=1000, archive_max_bytes=10000000, archive_path=None)`.
- Adds `compact() -> int` and internal `_append_archive_record(document) -> None`.
- Fee document version 2 uses `order_high_watermarks[key] = {'cumulative_amount': x, 'fee_quote': y}`.

- [ ] **Step 1: Write failing retention tests**

Create active, pending-notification, recent terminal, and old terminal orders. Assert compaction archives only old safe terminal records, keeps the configured newest terminal window, and removes no active/pending records.

- [ ] **Step 2: Write failing archive crash/idempotency tests**

Patch archive append to fail and assert hot JSON remains unchanged. Reload and compact twice; assert each archived order ID occurs exactly once.

- [ ] **Step 3: Write failing fee high-watermark tests**

Apply cumulative fills 4 then 10, reload/compact, replay 10, then apply 12. Assert fee deltas are 4 + 6 + 0 + 2, aggregates reconcile, and hot checkpoint count respects retention.

- [ ] **Step 4: Run RED**

Run: `pytest -q market_maker/tests/test_paper_retention.py market_maker/tests/test_paper_accounting.py`
Expected: new retention/high-watermark assertions fail.

- [ ] **Step 5: Implement archive-before-delete and v1 migration**

Rotate archives at the configured byte limit; include `record_type`, schema version, session, symbol, order ID, and document. Load v1 `applied_events` without losing totals, and write v2 after the next fee update.

- [ ] **Step 6: Run GREEN and commit**

Run: `pytest -q market_maker/tests/test_paper_retention.py market_maker/tests/test_paper_accounting.py market_maker/tests/test_paper_orders.py`
Expected: all pass.

Commit: `feat: bound paper hot state with durable archives`

---

### Task 4: Engine Store, Monitoring, and Monotonic Scheduling Integration

**Files:**
- Modify: `market_maker/engine.py`
- Modify: `market_maker/run.py`
- Modify: `market_maker/tests/test_demo_engine_lifecycle.py`
- Modify: `market_maker/tests/test_worker_lifecycle.py`
- Modify: `market_maker/tests/test_demo_end_to_end.py`

**Interfaces:**
- Engine owns `result_store`, `no_quote_monitor`, and injected `monotonic_provider`.
- Adds `_sleep_until_next_round(target: float) -> float` returning the next target.

- [ ] **Step 1: Write failing result-store integration test**

Run two rounds and lifecycle updates; assert SQLite is canonical, bounded CSV contains the same recent view, and no full-history `DataFrame.to_csv` call occurs per round.

- [ ] **Step 2: Write failing no-quote integration tests**

Run deterministic USDT no-quotes through threshold, cooldown, reset, restart, and separate BTC store. Assert row fields contain count/reason/diagnostics/alert state and reporter receives exactly the expected decisions.

- [ ] **Step 3: Write failing monotonic scheduling tests**

```python
clock = FakeMonotonic([0, 2, 10, 13, 20])
bot.cfg.time_step = 10
bot.run()
assert sleeps == [8, 7]
```

Add an overrun case proving missed intervals are skipped and no zero-sleep catch-up burst occurs.

- [ ] **Step 4: Run RED**

Run: `pytest -q market_maker/tests/test_demo_engine_lifecycle.py market_maker/tests/test_worker_lifecycle.py`
Expected: new store/monitor/scheduling assertions fail.

- [ ] **Step 5: Integrate modules without changing strategy calls**

Replace in-memory accumulated CSV mutation with store append/update/load calls. Call Algorithm diagnostics once for the round, feed it to the monitor only when `quote is None`, reset monitor when a quote exists, publish pending monitor decisions through the strict reporter, export bounded CSV, then sleep to the monotonic target.

- [ ] **Step 6: Run GREEN and commit**

Run: `pytest -q market_maker/tests/test_demo_engine_lifecycle.py market_maker/tests/test_worker_lifecycle.py market_maker/tests/test_demo_end_to_end.py market_maker/tests/test_algo1_diagnostics.py market_maker/tests/test_algo1_baseline.py market_maker/tests/test_strategy_algo1.py`
Expected: all pass and Algorithm 1 baseline remains identical.

Commit: `feat: integrate bounded demo operations loop`

---

### Task 5: Dashboard SQLite View and No-Quote Fields

**Files:**
- Modify: `market_maker/dashboard.py`
- Modify: `market_maker/tests/test_dashboard.py`

**Interfaces:**
- `load_track(path)` detects the sibling SQLite store, loads its bounded view, and falls back to CSV.
- Activity/summary records expose no-quote count, duration, alert state, reason, and candidate diagnostics.

- [ ] **Step 1: Write failing SQLite preference/fallback tests**

Create a stale CSV and newer SQLite row; assert SQLite wins. Remove SQLite; assert legacy CSV still loads.

- [ ] **Step 2: Write failing dashboard field tests**

Assert the explicit no-quote fields survive filtering/activity projection and candidate JSON is rendered deterministically.

- [ ] **Step 3: Run RED**

Run: `pytest -q market_maker/tests/test_dashboard.py`
Expected: new assertions fail.

- [ ] **Step 4: Implement store preference and display fields**

Keep all existing legacy aliases. Do not require SQLite for Real mode.

- [ ] **Step 5: Run GREEN and commit**

Run: `pytest -q market_maker/tests/test_dashboard.py market_maker/tests/test_result_store.py`
Expected: all pass.

Commit: `feat: expose no-quote operations in dashboard`

---

### Task 6: Migration, Full Verification, and Reports

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docs/reports/AUDIT_REPORT.md`
- Modify: `docs/reports/FIX_REPORT.md`
- Modify: `docs/reports/ISSUES.md`
- Modify: `docs/reports/TASKS.md`
- Modify: `docs/reports/VALIDATION_REPORT.md`
- Modify: `docs/reports/LIVE_DOCKER_PERFORMANCE_REPORT.md`

- [ ] **Step 1: Run static and full deterministic verification**

Run:

```text
python3 -m py_compile market_maker/*.py market_maker/exchange/*.py market_maker/strategy/*.py
pytest -q
git diff --check
docker compose -f docker-compose.yml -f docker-compose.validation.yml config --quiet
```

Expected: compilation/test/diff/config all exit 0.

- [ ] **Step 2: Rebuild and prove source identity**

Build all services and compare SHA-256 for engine, result store, no-quote monitor, paper, run, Telegram, safety, and session sources between host and image.

- [ ] **Step 3: Run ten-minute credential-empty live soak**

Start with `docker-compose.validation.yml`; sample every 30 seconds. Capture service health, CPU, memory, PIDs, I/O, round counts/latency/cadence, SQLite/CSV/hot JSON/archive sizes, no-quote episode state, accounting, fee scopes, shared session, logs, and auth emptiness.

- [ ] **Step 4: Stop and reconcile**

Send Docker stop, assert worker exit 0/OOM false, zero active paper orders, no open CSV/store lifecycle state, fee/accounting reconciliation, and intentionally remove the stack.

- [ ] **Step 5: Update every report**

Record root cause, files/symbols, before/after behavior, RED/GREEN/full test evidence, Docker metrics, safety ledger including PROCESS-001, remaining external limitations, and commit hashes. Mark PERF-001/002/003 fixed only if their acceptance checks passed. Classify OPS-001 as resolved by monitoring, not forced quoting.

- [ ] **Step 6: Final focused commit**

Commit implementation reports and any final validated adjustments locally; do not push.

## Plan self-review

- Spec coverage: tasks map to every approved persistence, retention, cadence, monitoring, dashboard, testing, Docker, and reporting requirement.
- Placeholder scan: no TBD/TODO or undefined implementation decisions.
- Type consistency: engine consumes the exact store/monitor interfaces created in Tasks 1–2; dashboard uses the store view; retention configuration flows through `MMConfig` and `make_bot`.
- Safety: all Docker steps use the credential-empty validation override; Algorithm 1 remains observer-only.
