# Demo Admin Control — Deployment Guide

Design spec: `docs/superpowers/specs/2026-08-01-admin-demo-control-design.md`.
Implementation plan: `docs/superpowers/plans/2026-08-01-admin-demo-control.md`.
Branch: `parsaoryani/admin-demo-control`. This document is written so a
separate session ("Codex") can execute the actual server deployment without
needing the design conversation's context. **Nothing in this document has
been executed against the production host by the session that wrote it** —
it is documentation and local-only artifact validation (compose `config`,
`bash -n`), not a deployment.

## What this feature is

A second, separate management surface for the existing Demo/paper-trading
stack (`deploy/docker-compose.production.yml`) — full application-level
control (worker lifecycle, per-pair config, sessions, order cancellation,
backup/restore, reset, recovery, audit) without ever granting shell, Docker
socket, or host access, and without ever touching Real Mode, exchange
credentials, or Sarkhatti. Full architectural reasoning is in the spec.

Three components:

1. **`lob-admin`** (new Compose service) — `market_maker/admin_app.py`,
   plain Flask, server-rendered. Reads Demo state (ro mounts) and enqueues
   a fixed set of named actions to `runtime/control/`. Never shells out,
   never gets a Docker socket, never writes to `runtime/state`/`runtime/results`.
2. **`deploy/control_agent.py`** — host-level Python script (systemd
   service, **not** in a container). The only component with Docker/file
   mutation authority. Polls `runtime/control/commands/`, dispatches to a
   fixed dict of handler functions, writes results.
3. **`lob-dashboard`** — untouched, still read-only.

## Routes (`market_maker/admin_app.py`)

| Route | Method | Purpose |
|---|---|---|
| `/admin/login` | GET/POST | Session login (username + password hash) |
| `/admin/logout` | POST | Clears session (CSRF-protected) |
| `/admin/health` | GET | Unauthenticated container healthcheck |
| `/admin/` | GET | Status: session id, control-lock state, per-pair worker health, recent audit |
| `/admin/config/<pair>` | GET/POST | View/apply/revert the pair's Demo config overlay |
| `/admin/workers/<start\|stop\|restart>` | POST | `pair` form field ∈ `btc`, `usdt`, `both` |
| `/admin/session/new` | POST | Stop running workers, rotate the shared session, restart |
| `/admin/orders/cancel` | POST | Cancel active orders for `pair` ∈ `btc`, `usdt`, `both` |
| `/admin/backups` | GET | List backups and their contents |
| `/admin/backups/create` | POST | Manual snapshot |
| `/admin/backups/<name>/restore` | POST | Requires `confirm=RESTORE`; previews affected files without it |
| `/admin/reset` | POST | Requires `confirm=RESET`; previews affected files without it |
| `/admin/recover` | GET | Read-only diagnostic report (stale locks / session mismatches / corrupt files) |
| `/admin/recover/apply` | POST | Enqueues `recover_demo` (auto-clears only stale locks; never resets) |
| `/admin/export` | GET | Streams a zip of a freshly created backup snapshot |
| `/admin/orders`, `/admin/balances`, `/admin/logs`, `/admin/audit` | GET | Read-only views (orders/balances reuse `dashboard.py`'s pure data functions where possible) |

Every mutating route is `@login_required` + `@csrf_protected`. Destructive
routes (`reset`, `restore_backup`) require the exact typed `confirm` value
server-side — a JS confirm dialog alone is not trusted.

## Security controls

- HTTPS via the existing Nginx cert; separate Basic Auth realm/`.htpasswd`
  for `/admin/` (Layer 1), app login (Layer 2), per-session CSRF token
  checked with `hmac.compare_digest` on every POST (Layer 3).
- `runtime/control/control.lock` serializes management operations; a
  concurrent request gets `409` (`OperationInProgress`), never a second
  enqueue.
- Closed enums for both config fields (`demo_config.FIELD_SPECS`) and
  agent actions (`control_queue.ACTIONS`) — allow-list, not a blocklist.
- No Docker socket anywhere in `lob-admin`. `deploy/control_agent.py` only
  ever runs fully-hardcoded argv arrays (`WORKER_ARGV`) or calls fixed
  Python handler functions — never a caller-built string, never
  `shell=True`.
- `market_maker/demo_entrypoint.py`'s fail-closed guard wraps the admin
  container's launch command exactly like every other service.
- Audit log (`runtime/control/audit.log`) is append-only JSONL, one entry
  per completed action. Not tamper-evident (no hash chaining) — deferred,
  see spec's "Explicitly deferred" section.

## Test list (all deterministic, no Docker/network/server required)

- `market_maker/tests/test_demo_config.py` — overlay validation, atomic
  write/`.previous` rollback round-trip, `make_cfg` demo-only merge.
- `market_maker/tests/test_control_queue.py` — enum validation, lock
  contention, audit JSONL, result polling.
- `market_maker/tests/test_control_agent.py` — one test per handler per
  the spec's risk table, including cancel_orders's before/after
  running-state cases and recover_demo's never-auto-reset guarantee.
- `market_maker/tests/test_admin_auth.py` — login/CSRF/session-timeout.
- `market_maker/tests/test_admin_actions.py` — pair selection, config
  validate-before-enqueue, one audit entry per action, lock contention,
  destructive-route confirmation, export-from-backups-only.
- `market_maker/tests/test_admin_readonly_views.py` — parity with
  `dashboard.py`'s own functions.
- `market_maker/tests/test_admin_demo_safety.py` — zero-real-call proof
  (mirrors `test_demo_safety.py`).
- `market_maker/tests/test_deployment_contract.py` — extended for the
  `admin` service (loopback-only port, no Docker socket, mounts, resource
  limits, healthcheck, `admin.env.example` has no committed secrets).

Run locally: `python3 -m pytest -q` from the repo root.

## Step-by-step deployment (for the executing session)

These steps mirror how `lob-dashboard` was deployed (see the
`DEPLOYMENT_PROGRESS.md` entries above this feature's checkpoint) — read
those first if anything here is ambiguous about the host's current state.

1. **Pull/checkout** this branch (`parsaoryani/admin-demo-control`) on the
   host at `/home/ubuntu/limit-order-book`, or merge it into whatever branch
   is actually deployed there. Confirm with `git log -1` that the commit
   SHA matches the one recorded as "the commit to deploy" at the bottom of
   `DEPLOYMENT_PROGRESS.md`.
2. **Create `deploy/admin.env`** (gitignored, not present in the repo) from
   `deploy/admin.env.example`:
   ```bash
   cp deploy/admin.env.example deploy/admin.env
   python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('CHOOSE-A-REAL-PASSWORD', method='pbkdf2:sha256'))"
   python3 -c "import secrets; print(secrets.token_hex(32))"
   # paste the two outputs into ADMIN_PASSWORD_HASH / ADMIN_SESSION_SECRET in deploy/admin.env
   chmod 600 deploy/admin.env
   ```
3. **Create the runtime directories** the new service needs (siblings of
   the existing `runtime/state`, `runtime/results`):
   ```bash
   mkdir -p runtime/control/commands runtime/control/results runtime/backups
   ```
4. **Rebuild and bring up the stack**, including the new `admin` service:
   ```bash
   cd /home/ubuntu/limit-order-book
   docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
     --env-file deploy/demo.env build \
     --build-arg LOB_REVISION="$(git rev-parse HEAD)" \
     --build-arg LOB_BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
     --env-file deploy/demo.env up -d --wait
   ```
5. **Install the host-level control agent** (NOT a container):
   ```bash
   sudo cp deploy/systemd/lob-control-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now lob-control-agent.service
   sudo systemctl status lob-control-agent.service   # confirm active/running
   ```
6. **Nginx**: create a separate `.htpasswd` for the admin realm (distinct
   credentials from the dashboard's), install the updated
   `deploy/nginx/lob-dashboard-https.conf` (it now contains a
   `location /admin/` block pointing at `127.0.0.1:8653`), test, then reload:
   ```bash
   sudo htpasswd -c /etc/nginx/lob-admin.htpasswd <admin-basic-auth-username>
   sudo cp deploy/nginx/lob-dashboard-https.conf /etc/nginx/sites-available/lob-dashboard
   sudo nginx -t
   sudo systemctl reload nginx
   ```
7. **Verify**:
   - `docker compose -p limit-order-book ps` shows `lob-admin` healthy.
   - `curl -u <basic-auth-user>:<pass> https://dashboard.185-206-95-141.nip.io/admin/login`
     returns the login page (200).
   - Log in with the app-level `ADMIN_USERNAME`/password from `admin.env`,
     confirm `/admin/` renders session/worker status.
   - `systemctl status lob-control-agent` stays active; `runtime/control/audit.log`
     gets an entry after a test action (e.g. a no-op `cancel_orders` on an
     already-stopped pair).
   - Confirm the pre-existing dashboard, both worker containers, and
     Sarkhatti are all unaffected (same check pattern as the dashboard's
     own deployment checkpoint above).
8. **Rollback**, if needed: `./deploy/rollback.sh` (now also removes the
   admin `.htpasswd` and stops `lob-control-agent.service` before tearing
   down the Compose project).

## Explicitly deferred (do not build unless asked)

Multi-admin/RBAC, tamper-evident audit log, config history beyond one
`.previous` slot, async job-progress UI, automated backup retention. See
the spec's "Explicitly deferred" section for the reasoning.
