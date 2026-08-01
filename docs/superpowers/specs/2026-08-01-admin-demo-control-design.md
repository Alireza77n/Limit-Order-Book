# Admin Demo Control — Design Spec

Status: approved by operator (2026-08-01). Demo/paper-trading only. No Real Mode surface anywhere in this feature.

## Goal

Give an authorized admin full **application-level** control over the Demo (paper-trading) system — worker
lifecycle, per-pair configuration, sessions, order cancellation, backup/restore, reset, and recovery — through
a page separate from the existing read-only dashboard, without ever granting shell, Docker socket, or host
access, and without ever touching Real Mode, exchange credentials, or Sarkhatti.

## Architecture

```
Browser --HTTPS/BasicAuth--> Nginx /admin/ --> lob-admin container (Flask, new)
                                                     |
                                                     | writes command files, reads results/audit
                                                     v
                                     runtime/control/{commands,results,audit.log}
                                                     ^
                                                     | polls, executes fixed allow-listed actions
                                                     |
                                    deploy/control_agent.py (host-level systemd service,
                                    NOT run in a container, NOT installed by this session)
                                                     |
                                                     v
                                     docker compose -p limit-order-book {start,stop,restart} ...
                                     + direct file operations on runtime/state, runtime/results,
                                       runtime/backups (session, ledgers, fee state, archives,
                                       demo CSV/SQLite)
```

Three components, three trust levels:

1. **`lob-admin`** (new Compose service, container `lob-admin`) — Flask app, admin UI + auth + CSRF +
   read-only status/audit/export views. Mounts `runtime/state:ro`, `runtime/results:ro`,
   `runtime/control:rw` (queue + audit log only). Never mutates Demo state directly — every mutating
   action is a command file written to the queue. Never gets a Docker socket, never shells out.
2. **`deploy/control_agent.py`** — a host-level Python script (systemd-managed, deployed later, not by
   this session) that is the **only** component with Docker/file mutation authority. It polls
   `runtime/control/commands/`, matches each file's `action` against a **fixed dict of handler
   functions** (no dynamic dispatch, no string-built shell/argv), executes the handler, writes a result
   file. Runs as the same unprivileged `ubuntu` user that already runs `docker compose` today (per
   `deploy/rollback.sh`) — no new privilege.
3. **Existing `lob-dashboard`** — untouched. Still read-only, still monitoring-only.

This keeps the web-facing, most-exposed component (`lob-admin`) at the lowest privilege: it can read
Demo state and enqueue a fixed set of named actions, nothing else. All actual mutation logic lives in
one small, host-level, individually unit-tested module.

## Command queue

- `runtime/control/commands/<command_id>.json`: `{"command_id", "action", "pair"?, "requested_by",
  "requested_at", "payload"?}`. `action` and `pair` are validated against closed enums before the file
  is even written by `lob-admin`; the agent validates them again independently (never trusts the queue
  file's shape blindly).
- `runtime/control/results/<command_id>.json`: `{"command_id", "ok", "detail", "finished_at"}`.
- `runtime/control/control.lock`: a single `flock` (same pattern as `session.py`'s existing lock files)
  held by `lob-admin` for the full round-trip of one management operation. A second request while the
  lock is held gets an immediate "operation already in progress" response — this is the concurrency /
  duplicate-click guard.
- `runtime/control/audit.log`: append-only JSONL, written by `lob-admin` after each completed operation
  (one entry per user-initiated action, including the agent's result).

## Agent action allow-list

Each action is a separate, individually testable pure-ish handler `handle_<action>(paths, pair=None) ->
AgentResult(ok: bool, detail: dict)`. No handler ever executes a caller-supplied string; pair values are
restricted to the closed set `{"btc", "usdt"}` and mapped through a fixed dict to container/service
names, never interpolated.

| Action | Behavior |
|---|---|
| `start_worker` / `stop_worker` / `restart_worker` | `docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env {start\|stop\|restart} <service>` via a fully-hardcoded argv array, one per (action, pair) pair — 6 arrays total, chosen by dict lookup. |
| `apply_config` | Validate the submitted overlay (already validated by `lob-admin` before enqueueing; the agent re-validates independently). **Before writing**, copy the current overlay file (if any) to `runtime/state/demo_config_<pair>.json.previous` (single-slot rollback, overwritten each time — see Adjustment 4 below). Write the new overlay. `restart_worker` for that pair so the change takes effect. |
| `rollback_config` | Restore `demo_config_<pair>.json` from `.previous` if it exists; `restart_worker`. |
| `new_session` | Snapshot which of the enabled workers were currently running (lock-file presence). `stop_worker` for each. `session.initialize_stack_session(force_new=True)`. `start_worker` for exactly the ones that were running before. Old session's per-session files are untouched (they're named `paper_<old-session>_<pair>.json`) — automatic history. |
| `cancel_orders` | **Adjustment 1.** Only cancels active Demo orders; does not force a restart. Implementation: if the worker for a pair is currently running, request a graceful stop (`stop_worker`) — this reuses the existing, already-tested `_finish_demo_session` → `exchange.cancel_all_open_orders` shutdown path in `engine.py`, which is the only place Demo order cancellation logic already lives — then, **only if the worker was running before the action was requested**, restart it so the running/stopped state ends exactly as it started. If the worker was already stopped, this action is a no-op beyond confirming there are zero active orders (nothing to cancel), and it reports that plainly rather than starting a worker that wasn't running. The result explicitly reports the before/after running state. |
| `create_backup` | Archive current demo state+result files (`paper_*.json`, `paper_*_archive.jsonl`, `paper_fee_scopes.json`, the shared session file, `*_demo.csv`, `*_demo.sqlite3`) into `runtime/backups/manual-<timestamp>/` (copy, not move). Does not require workers to be stopped — see "consistent snapshot" below. |
| `reset_demo` | `stop_worker` for both pairs → `create_backup` (mandatory, into `runtime/backups/demo-reset-<timestamp>/`) → delete only the live `_demo`-suffixed files → `new_session` → restart the workers that were running before. Requires a prior explicit confirmation flag in the command payload (checked by `lob-admin` server-side before enqueueing, and re-checked by the agent). |
| `restore_backup` | Validate the named backup directory exists directly under `runtime/backups/` and contains only expected filenames (path-traversal-safe: reject anything with `..` or a non-matching parent). `stop_worker` for both pairs. `create_backup` of the *current* live state first (safety snapshot, so a bad restore is itself reversible) into `runtime/backups/pre-restore-<timestamp>/`. Copy the named backup's files back over the live paths. Restart the workers that were running before. |
| `recover_demo` | **Adjustment 3.** Diagnostic + *safe, targeted* repairs only — never a silent alias for `reset_demo`. Checks: (a) stale worker lock file with no corresponding live container process — clear only that lock file; (b) session file present but a per-pair ledger file's `session_id` doesn't match it — this is reported, not auto-fixed, because rewriting a ledger's session id destroys data-lineage; (c) malformed/corrupt JSON in a state file — reported, not auto-repaired. If the only available fix for a detected problem is data-clearing (corrupt ledger, session/ledger mismatch), `recover_demo` **stops and returns a report requiring the admin to explicitly confirm and run `reset_demo` themselves** — it never escalates to reset_demo on its own. |

## Consistent export/backup snapshot (Adjustment 2)

Workers may be actively writing to `*_demo.sqlite3` (WAL mode, per `result_store.py`) and the JSON
ledger/fee/archive files while a backup or export is requested. `create_backup` (used by both
`reset_demo`, `restore_backup`'s safety snapshot, and manual on-demand backups) is the **only** path that
reads these files, and it does so through the agent, not through the always-running `lob-admin` process:

- SQLite: since `result_store.py` already sets `PRAGMA journal_mode=WAL`, the agent takes a **read-only
  SQLite backup** via `sqlite3`'s own `backup()` API (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`
  → `dest.backup(source)`), which is consistent by construction even against a concurrent writer — no
  file-copy race, no need to stop the worker just to read data.
- JSON/JSONL state files: copied with `shutil.copy2` after opening the destination with `os.replace`
  from a temp file (atomic per-file), which is sufficient because each of these files is itself written
  atomically by the existing code (`session.py`'s temp+`os.replace` pattern; `paper.py`'s ledger persists
  the same way) — a copy can only ever observe one fully-written generation of the file, never a partial
  write.

`lob-admin`'s **export** feature does not read `runtime/results`/`runtime/state` live at all. It enqueues
`create_backup` (or reuses the most recent one if fresh enough — kept simple: always takes a fresh
snapshot on export request, since Demo data volume is small) and then streams the resulting snapshot
directory as a zip from `runtime/backups/`, which by definition is no longer being written to.

## Configuration rollback (Adjustment 4)

`apply_config`'s handler always preserves the prior overlay as `demo_config_<pair>.json.previous` before
writing the new one (single-slot, not a history stack — good enough for "undo my last change," which is
the actual need; a full version history is noted as a future improvement, not built now). A
`rollback_config` action restores it. `lob-admin`'s config page shows whether a `.previous` file exists
and offers a "revert last change" button that maps to this action.

## Configurable fields (per pair, Demo-only overlay — unchanged from prior design)

`order_size, inv_max, inv_min, demo_inv_initial, cash_initial, commission_perc, cancel_after_minutes,
depth_max, time_step, max_rounds, demo_level1_fast_fill_enabled, demo_level1_fill_delay_seconds,
demo_level1_fill_fraction, demo_queue_enabled, no_quote_alert_rounds, no_quote_alert_seconds,
no_quote_alert_cooldown_seconds, no_quote_recovery_alert, algo1_diagnostics_enabled`

Excluded (identity/secrets, never editable or displayed): `pair, currency_1, currency_2, algorithm_id,
token, url_send_order, url_wallet_balance`. `run.py:make_cfg` merges the overlay on top of
`TRADING_PROFILES[profile]` **only when `mode == "demo"`** — real-mode behavior and `strategy/algo1.py`
are untouched by this feature entirely.

## Admin app (`market_maker/admin_app.py`)

Plain Flask (already a transitive dependency via Dash — no new package). Server-rendered Jinja
templates, not Dash callbacks — total separation from the dashboard's code path.

- **Auth**: `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` (`werkzeug.security.generate_password_hash` /
  `check_password_hash`) + `ADMIN_SESSION_SECRET`, supplied via a new gitignored `deploy/admin.env` (a
  committed `deploy/admin.env.example` documents the shape, mirroring `market_maker/.env.example`).
  Flask's signed session cookie, 30-minute idle timeout.
- **CSRF**: hand-rolled per-session token (`secrets.token_urlsafe(32)`), hidden field on every form,
  `hmac.compare_digest` check on every POST — no new dependency.
- **Routes**: `GET/POST /admin/login`, `POST /admin/logout`, `GET /admin/` (status: pair
  enablement/health, current session id, lock state, last N audit entries), `GET/POST
  /admin/config/<pair>` (edit + validate + apply, with "revert last change" when a `.previous` exists),
  `POST /admin/workers/<action>` (`action` ∈ start/stop/restart, `pair` ∈ btc/usdt/both), `POST
  /admin/session/new`, `POST /admin/orders/cancel`, `GET /admin/backups` (list + inspect), `POST
  /admin/backups/create`, `POST /admin/backups/<name>/restore` (typed confirmation), `POST /admin/reset`
  (typed confirmation), `GET /admin/recover` (diagnostic report), `POST /admin/recover/apply` (only the
  safe repairs `recover_demo` identified), `GET /admin/export` (streams a fresh snapshot zip), `GET
  /admin/audit`, `GET /admin/orders`, `GET /admin/balances`, `GET /admin/logs` — all read-only views
  sourced from the ro-mounted `runtime/results`/`runtime/state`.
- Destructive routes (`reset`, `restore_backup`) render an "affected files/records" preview before the
  confirm step and require a server-checked `confirm=RESET` / `confirm=RESTORE` field.

## Security summary

- HTTPS via existing Nginx cert (`dashboard.185-206-95-141.nip.io`), new `location /admin/` block.
- Layer 1: Nginx Basic Auth, separate `.htpasswd` realm from the dashboard's.
- Layer 2: app-level login + session.
- CSRF on every mutating route.
- `control.lock` serializes all management operations; concurrent/duplicate requests are rejected, not
  queued.
- Full allow-list (not a blocklist) for both config fields and agent actions — nothing new can leak or
  execute by omission.
- No Docker socket anywhere. No shell/exec surface in `lob-admin`. The agent itself only ever runs
  fully-hardcoded argv arrays or calls fixed Python functions — never a caller-supplied string.
- `demo_entrypoint.py`'s existing fail-closed guard still wraps `lob-admin`'s own container launch
  command.
- Audit log is append-only JSONL, not tamper-evident (no hash chaining) — explicitly deferred, see below.

## Explicitly deferred (documented improvement points, not built now)

Per operator direction not to over-engineer a non-commercial team tool:

- Multi-admin accounts / RBAC (single shared admin credential is enough today).
- Tamper-evident (hash-chained) audit log.
- Config change history beyond one previous version.
- Async job-progress UI (management actions block synchronously up to a bounded timeout instead).
- Automated backup retention/rotation policy (backups accumulate under `runtime/backups/`; documented as
  an operational task).

## Risk table (what can interrupt workers or affect data)

| Action | Interrupts workers? | Data effect |
|---|---|---|
| start/stop/restart | yes, by design | none |
| apply_config | yes (restart) | none (ledger reloads from existing state file) |
| rollback_config | yes (restart) | none |
| new_session | yes (stop+start) | new session id; old session files preserved as history |
| cancel_orders | only if worker was running, and restored to prior state after | cancels open orders only |
| create_backup | no | none (read-only snapshot) |
| reset_demo | yes | **clears live demo state**, but only after a mandatory backup |
| restore_backup | yes | overwrites live state with backup contents, after a safety snapshot of the pre-restore state |
| recover_demo (auto part) | no | clears only a provably-stale lock file, nothing else |

## Testing plan

All deterministic, no Docker/network/server required:

- `test_demo_config.py` — overlay validation (reject unknown/out-of-range/identity/secret keys), atomic
  write/read, `.previous` rollback round-trip, `make_cfg` merge precedence, real-mode untouched.
- `test_control_queue.py` — command file schema/enum validation, lock acquire/release, "already in
  progress" under a held lock.
- `test_control_agent.py` — one test per handler: `resolve_worker_command` whitelist lookup rejects
  anything outside the fixed dict; `cancel_orders` before/after running-state preservation and the
  already-stopped no-op path; `reset_demo` backs up before clearing and only ever touches
  `_demo`-suffixed files, never `*_track.csv`; `restore_backup` rejects path-traversal/unknown backup
  names and snapshots current state first; `recover_demo` clears only a stale lock automatically and
  returns a "confirmation required" report (never calls reset) when the only fix is destructive;
  SQLite/JSON snapshot consistency helpers.
- `test_admin_auth.py` — login required on every `/admin/*` route except login; wrong password rejected;
  CSRF required and validated; session expiry.
- `test_admin_actions.py` — pair selection drives the correct command set; destructive routes require the
  typed confirm field server-side; audit log gets exactly one entry per completed action.
- Reuse the existing zero-real-call assertion pattern (`test_demo_safety.py`/`test_demo_end_to_end.py`)
  against the admin/agent code paths to prove no Nobitex/MySQL/Telegram call is ever reachable.
- `bash -n` on `deploy/control_agent.py`'s companion systemd unit file / any shell wrapper, matching
  `rollback.sh`'s existing check.

## Deployment (documentation only — not executed in this session)

New Compose service `admin` in `deploy/docker-compose.production.yml`; new Nginx `location /admin/` +
separate `.htpasswd`; new `deploy/admin.env` (gitignored) + `.example`; new systemd unit
`deploy/systemd/lob-control-agent.service` for the host poller; `deploy/rollback.sh` updated to also tear
down the `admin` service. Full step-by-step instructions land in
`docs/deployment/ADMIN_PAGE.md`, written so a separate deploy session ("Codex") can execute them without
needing this conversation's context.
