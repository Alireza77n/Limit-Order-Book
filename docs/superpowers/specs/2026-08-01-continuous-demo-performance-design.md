# Continuous Demo Performance and No-Quote Monitoring Design

Date: 2026-08-01
Status: approved
Scope: Demo persistence, paper retention, round scheduling, no-quote operations, dashboard/report evidence, and Docker soak validation.

## Goals

1. Preserve Algorithm 1 quote-selection behavior exactly while making sustained no-quote episodes observable and actionable.
2. Remove round-time work proportional to the entire accumulated CSV history.
3. Bound hot paper-ledger and fee-idempotency state while retaining an append-only audit archive.
4. Make configured round intervals start-to-start targets based on monotonic time.
5. Preserve explicit CSV/dashboard compatibility, restart safety, accounting, Telegram idempotence, shared sessions, and zero real-order capability in Demo Mode.

## Non-goals

- Do not force quotes or orders when Algorithm 1 rejects every candidate.
- Do not alter Algorithm 1 candidate generation, sorting, profitability threshold, fallback, or selection.
- Do not enable real credentials, real order/cancel endpoints, wallet access, or MySQL in Demo Mode.
- Do not migrate Real-mode reporting or fee scopes in this change.

## Architecture

### DemoResultStore module

Introduce a deep `DemoResultStore` module backed by one SQLite database per symbol/session-compatible results path. Its interface is:

- `append_round(row: Mapping[str, object]) -> int`
- `update_orders(changed_order_ids, lifecycle_fields, financial_fields) -> None`
- `load_view(limit: int | None = None) -> pandas.DataFrame`
- `export_bounded_csv(path: str, max_rows: int) -> None`
- `compact(retain_rows: int) -> int`

SQLite is the canonical mutable result store. Round insertion and order lifecycle updates use indexed transactions rather than rewriting all historical rows. The table stores the existing explicit Demo columns plus normalized order-ID indexes and no-quote fields. A schema metadata table records version and ordered export columns.

CSV remains a compatibility export. The store rewrites only a configurable bounded recent-row export, never the entire accumulated history. Dashboard loading prefers SQLite and falls back to existing CSV files, retaining legacy Real compatibility. Existing CSV data is imported once under a migration marker; duplicate imports are prevented by a deterministic source fingerprint and unique round key.

Each symbol uses a distinct database file, avoiding BTC/USDT writer contention. SQLite uses WAL mode, bounded busy timeout, and explicit transactions. Database or export errors are logged and do not mutate paper accounting.

### NoQuoteMonitor module

Introduce `NoQuoteMonitor`, persisted per session and symbol in the result store. Its interface is:

- `record_no_quote(round_no, now, reason, diagnostics) -> NoQuoteDecision`
- `record_quote(round_no, now) -> NoQuoteDecision`
- `mark_alert_delivered(decision, message_id) -> None`
- `mark_recovery_delivered(decision, message_id) -> None`
- `snapshot() -> NoQuoteSnapshot`

The persisted state contains:

- consecutive round count;
- episode start and latest timestamps;
- exact reason and previous reason;
- candidate bid, ask, spread, last price, commission, expected-return minimum, and required threshold;
- alert eligibility, pending delivery, last delivered timestamp, and Telegram message ID;
- recovery-notification state.

Either a configurable consecutive-round threshold or duration threshold triggers eligibility. The first eligible state sends one alert. A reason change creates a state-change alert. Otherwise another alert is permitted only after the configurable cooldown. Failed Telegram sends remain pending and are retried; delivered state advances only after strict transport success returns a message ID.

A valid quote resets the episode atomically. If the episode had produced an alert, one recovery notification reports total count and duration. BTC and USDT state is isolated by the `(session_id, symbol)` key.

Algorithm 1 remains unchanged. The monitor consumes only the existing observer methods `diagnostics()` and `no_quote_reason()`.

### Paper hot-state retention and archives

The hot ledger retains:

- all active orders;
- all orders with pending Telegram submission/lifecycle/recovery delivery;
- the newest configurable number of terminal orders.

Older safe terminal orders are appended to size-rotated JSONL archives before removal from hot JSON. Archives are scoped by session and symbol and carry a schema version. Rotation uses a configurable maximum byte count and deterministic segment numbers. Appends are flushed and synchronized before hot-state deletion. Startup completes or safely repeats interrupted compaction using archive record IDs.

Shared fee idempotency changes from one key per cumulative-fill event to one cumulative high-watermark per `(session, symbol, order)`. The record stores the maximum processed cumulative amount and aggregate quote fee. A duplicate or lower cumulative update is a no-op; a higher cumulative update applies only the delta. When an order is archived, its checkpoint is appended to the archive and removed from hot fee state only after the pair ledger can no longer replay that order. Aggregate session/pair/all-time totals remain in the hot fee document.

Compaction runs after terminal transitions and once during load. It never prunes active orders, pending notification state, or the configured recent terminal window.

### Monotonic scheduling

Inject `monotonic_provider` alongside the existing wall-clock `now_provider`. At loop start, set the first target to the current monotonic time. After each round, advance the target by `time_step` and sleep only `max(0, target - monotonic_now)`.

If work overruns one or more intervals, advance the target past the current monotonic time without executing catch-up bursts. Wall-clock timestamps remain unchanged for reports and Tehran display. Interrupts during monotonic sleep continue through the existing cleanup path.

### Dashboard and reporting

Add explicit view/export fields:

- `No Quote Consecutive Count`
- `No Quote Episode Seconds`
- `No Quote Alert State`
- `No Quote Reason`
- `No Quote Candidate Diagnostics`

Dashboard summary and activity views display count, reason, episode duration, alert state, commission, expected-return minimum, required threshold, and candidate spread details. Existing CSV aliases remain readable.

All audit, fix, issue, task, validation, and live-performance reports receive implementation evidence, exact tests, migration behavior, Docker soak metrics, limitations, and commit hashes.

## Configuration

Add Demo defaults with environment/config overrides:

- `demo_result_store_enabled = True`
- `demo_csv_export_max_rows = 10_000`
- `demo_hot_terminal_order_limit = 1_000`
- `demo_archive_max_bytes = 10_000_000`
- `no_quote_alert_rounds = 30`
- `no_quote_alert_seconds = 300`
- `no_quote_alert_cooldown_seconds = 1_800`
- `no_quote_recovery_alert = True`

Tests use smaller explicit values. Defaults are conservative and do not change quote decisions.

## Failure handling

- SQLite transactions roll back on error; paper accounting remains canonical and unaffected.
- CSV export failure is non-fatal and retried on a later round.
- Archive append/flush failure aborts compaction without deleting hot records.
- Interrupted archive compaction is idempotent by archived order ID.
- Telegram failure preserves pending no-quote/recovery delivery state.
- Invalid/corrupt store metadata fails closed with a clear error and preserves source files.
- Monotonic overruns skip missed deadlines and never produce a tight loop.

## Testing

Use mandatory red-green TDD for each behavior:

1. SQLite append does not rewrite previous rows; lifecycle updates target indexed order rows.
2. One-time CSV migration is idempotent; bounded export never exceeds its limit.
3. Dashboard store preference and CSV fallback produce the same explicit view.
4. Retention preserves active/pending/recent terminal orders and archives older terminal orders before deletion.
5. Archive failure leaves hot state unchanged; restart does not duplicate archives.
6. Fee cumulative high-watermarks preserve 4-then-10 as 4-plus-6 and remain idempotent after compaction/recovery.
7. No-quote count, duration trigger, round trigger, reason-change alert, cooldown, failed-send retry, valid-quote reset, recovery, restart, and BTC/USDT isolation.
8. Diagnostic records contain candidate prices/spreads, commission, expected-return minimum, and required threshold.
9. Monotonic scheduling subtracts processing time, skips overruns without catch-up bursts, and remains interruptible.
10. Existing Algorithm 1 baseline/source-parity tests remain unchanged and green.
11. Full deterministic suite, compile, diff check, Docker build/source identity, and credential-empty live soak.

## Docker acceptance

Run a credential-empty live public-data soak for at least ten minutes after the full suite passes. Capture:

- memory range and slope per service;
- CPU samples and round-processing latency;
- SQLite, bounded CSV, hot ledger, fee state, and archive size growth;
- expected BTC/USDT start-to-start cadence;
- no-quote count/reason/diagnostics and alert state with Telegram disabled;
- accounting, fee scopes, active/history/archive counts, and shared session identity;
- health, restarts, OOM, logs, and clean SIGTERM reconciliation.

The result is accepted only if memory remains stable over the window, bounded files respect configured limits, no hot-state invariant fails, both workers retain one shared session, no real/authenticated operation is possible, and shutdown leaves zero active paper orders.

## Self-review

- Placeholder scan: no placeholders or deferred implementation decisions.
- Consistency: SQLite is canonical for mutable round/report state; JSON remains canonical for paper accounting; archives are append-only audit storage; CSV is bounded compatibility output.
- Scope: limited to the four approved issues and required reporting/validation.
- Ambiguity: USDT profitability rejection remains mandatory; alerts never force quotes.
