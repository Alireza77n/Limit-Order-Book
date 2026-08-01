# Admin Demo Control — Implementation Plan

Spec: `docs/superpowers/specs/2026-08-01-admin-demo-control-design.md`. Branch:
`parsaoryani/admin-demo-control` (off `parsaoryani/algo1-refactor-testability`). Demo-only; Algorithm 1
(`strategy/algo1.py`) must not change. Never touch `main`.

Work TDD, one task per commit, run `pytest -q` after each task before moving on.

## Task 1 — Demo config overlay
Files: `market_maker/demo_config.py` (new), `market_maker/tests/test_demo_config.py` (new), `market_maker/run.py` (modify: `make_cfg` merges overlay when `mode=="demo"`).
- Field whitelist per spec. `load_overrides`, `validate_overrides` (reject unknown/out-of-range keys), `write_overrides` (atomic tmp+`os.replace`, preserves `.previous` before overwrite), `rollback_overrides`.
- Tests: validation rejects identity/secret/unknown keys and out-of-range values; write/read round-trip; `.previous` created and restorable; `make_cfg` applies overlay only for demo mode, real mode untouched.

## Task 2 — Control queue primitives
Files: `market_maker/control_queue.py` (new), `market_maker/tests/test_control_queue.py` (new).
- `submit_command(control_dir, action, pair, requested_by) -> command_id` (validates against closed enums before writing).
- `await_result(control_dir, command_id, timeout)` (poll for result file).
- `ControlLock` (flock wrapper reusing the `session.py` lease pattern) with a non-blocking acquire that raises a distinct `OperationInProgress` error.
- `append_audit(control_dir, entry)` (JSONL append).
- Tests: enum rejection, lock contention returns `OperationInProgress`, audit entries are well-formed JSONL, result polling times out cleanly.

## Task 3 — Control agent handlers (pure logic, no Docker/subprocess in tests)
Files: `deploy/control_agent.py` (new — handler functions + a `main()` poll loop), `market_maker/tests/test_control_agent.py` (new).
- `resolve_worker_argv(action, pair) -> list[str]`: fixed dict lookup, `KeyError`/explicit rejection for anything outside `{start,stop,restart} x {btc,usdt}`.
- `handle_apply_config`, `handle_new_session` (wraps `session.py`), `handle_cancel_orders` (before/after running-state logic per spec Adjustment 1 — running-state detection via worker lock file presence, no Docker call needed to *check* state, only to act), `handle_create_backup` (SQLite `backup()` API + `shutil.copy2` for JSON/JSONL per spec's consistent-snapshot section), `handle_reset_demo`, `handle_restore_backup` (path-traversal-safe name validation, pre-restore safety snapshot), `handle_recover_demo` (stale-lock-only auto-fix; returns a structured report and never calls `handle_reset_demo` itself).
- Docker-invoking handlers (`start/stop/restart_worker`) take an injectable `run_argv` function (defaults to `subprocess.run`) so tests can stub it and assert exact argv without invoking Docker.
- Tests: one per handler per spec's risk/behavior table, including the "already stopped → no-op, don't start" case for `cancel_orders`, the "never auto-reset" case for `recover_demo`, and the `_demo`-suffix-only deletion assertion for `reset_demo`.

## Task 4 — Admin app skeleton, auth, CSRF
Files: `market_maker/admin_app.py` (new), `market_maker/tests/test_admin_auth.py` (new).
- Flask app factory, login/logout, session cookie, CSRF token helper + decorator, `@login_required` on every `/admin/*` route except login.
- Tests: unauthenticated access to every mutating/status route redirects to login; wrong password rejected; missing/invalid CSRF token on a POST is rejected (400); valid login + CSRF succeeds against a stubbed no-op action.

## Task 5 — Admin routes: status, config, workers, session, cancel
Files: `market_maker/admin_app.py` (extend), `market_maker/tests/test_admin_actions.py` (new).
- Wires Task 1/2 primitives into routes per spec's route list (status, config edit/apply/revert, worker start/stop/restart with btc/usdt/both pair selection, new session, cancel orders).
- Tests: pair selection ("both") enqueues both pairs' commands; config apply validates before enqueueing and surfaces validation errors without enqueueing; one audit entry per completed action; concurrent second request while locked gets the "in progress" response, not a second enqueue.

## Task 6 — Backup, restore, reset, recovery, export routes
Files: `market_maker/admin_app.py` (extend), `market_maker/tests/test_admin_actions.py` (extend).
- Destructive routes require server-checked typed confirmation and render an affected-files preview.
- Export streams a zip built from a fresh `create_backup` snapshot (never live files) per spec Adjustment 2.
- Tests: reset/restore rejected without the exact confirm token; export response is built only from `runtime/backups/*`, never `runtime/results`/`runtime/state` directly (assert via a monkeypatched snapshot dir); recovery route only exposes the auto-safe fix, and the destructive-fix path requires the same confirm flow as reset.

## Task 7 — Read-only views: orders, balances, fees, PnL, logs, audit
Files: `market_maker/admin_app.py` (extend).
- Reuses `dashboard.py`'s existing pure data functions (`load_track`, `activity_records`, etc.) where they already do exactly this, rather than re-implementing — import, don't duplicate.
- Tests: rendered data matches what `dashboard.py`'s functions already return for the same fixture files (parity, not new logic).

## Task 8 — Zero-real-call safety proof
Files: `market_maker/tests/test_admin_demo_safety.py` (new, mirrors existing `test_demo_safety.py`/`test_demo_end_to_end.py` pattern).
- Assert every admin/agent code path's reachable call graph never imports/calls `NobitexExchangeClient` in trading-enabled form, never references `MYSQL_*`/`TELEGRAM_*`, and that `demo_entrypoint.py`'s guard still wraps the admin container's launch command.

## Task 9 — Deployment artifacts (docs + compose, not executed)
Files: `deploy/docker-compose.production.yml` (add `admin` service), `deploy/nginx/lob-dashboard-https.conf` (add `location /admin/` + separate htpasswd reference), `deploy/admin.env.example` (new), `.gitignore` (add `deploy/admin.env`), `deploy/systemd/lob-control-agent.service` (new), `deploy/rollback.sh` (extend to tear down `admin`), `market_maker/tests/test_deployment_contract.py` (extend: admin service loopback-only port, no new public exposure, admin.env has no secrets committed).
- `bash -n` the systemd unit's any embedded shell, and the updated `rollback.sh`.

## Task 10 — Documentation
Files: `docs/deployment/ADMIN_PAGE.md` (new — design summary, API/route behavior, security controls, test list, step-by-step deploy instructions for a separate session to execute), `docs/deployment/DEPLOYMENT_PROGRESS.md` (append a checkpoint entry: branch, commit, tests, files changed, unfinished items, exact next step), `README.md`/`usage.md` (short pointers to the new admin page, matching existing doc conventions).

## Task 11 — Final verification
- `pytest -q` full suite, `python -m compileall -q market_maker`, `docker compose ... config` re-validated (must still parse with the new `admin` service), `bash -n` on all new/changed shell.
- Record the exact tested commit SHA in `DEPLOYMENT_PROGRESS.md` as "the commit to deploy."
