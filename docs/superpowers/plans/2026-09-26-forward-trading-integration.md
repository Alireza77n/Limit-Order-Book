# Forward Trading Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the teammate's isolated BTC/IRT and USDT/IRT real-forward system into the tracked project, repair its observed defects, deploy it safely, and complete a monitored 10-minute real-trading validation.

**Architecture:** Preserve the existing Demo stack and add a parallel forward control plane. The uncredentialed Flask admin writes commands to an isolated queue; a credentialed agent validates commands, starts fixed real-mode worker commands, reads wallets, and owns worker cleanup. The dashboard reads only the isolated result directory, and Nginx exposes only the admin and dashboard.

**Tech Stack:** Python 3.10+, Flask/Gunicorn, requests, Ed25519 via `cryptography`, pytest, systemd, Nginx, Nobitex HTTP API.

## Global Constraints

- Import all production-relevant teammate changes from the reviewed `/opt/market-maker-forward` snapshot.
- Do not import `venv`, runtime/results, credentials, `.pyc`, caches, `run.py.before-no-mysql`, or `test_nobitex_api.ipynb`.
- Preserve existing Demo behavior and deployment.
- Keep API credentials only in `/etc/market-maker-forward.env`, root-owned mode `0600`.
- Do not expose credentials to admin, dashboard, Git, logs, or command output.
- Forward BUY and SELL exposure guards remain mandatory and capped at `0.25`.
- Do not start real workers until unit, deployment, DNS, TLS, wallet, and no-order checks pass.
- First real validation runs BTC/IRT and USDT/IRT for 10 minutes, then stops both and verifies cancellation and lock cleanup.

---

### Task 1: Import the teammate forward implementation

**Files:**
- Create: `market_maker/exchange/nobitex_auth.py`
- Create: `market_maker/forward_config.py`
- Create: `market_maker/templates/admin/forward.html`
- Create: `market_maker/templates/admin/forward_config.html`
- Create: `deploy/forward_control_agent.py`
- Create: `deploy/admin-forward.env.example`
- Create: `deploy/forward.env.example`
- Create: `deploy/market-maker-forward-agent.service`
- Create: `deploy/market-maker-forward-admin.service`
- Create: `deploy/market-maker-forward-dashboard.service`
- Create: `FORWARD_TEST.md`
- Modify: `market_maker/admin_app.py`
- Modify: `market_maker/config.py`
- Modify: `market_maker/control_queue.py`
- Modify: `market_maker/dashboard.py`
- Modify: `market_maker/demo_config.py`
- Modify: `market_maker/demo_entrypoint.py`
- Modify: `market_maker/engine.py`
- Modify: `market_maker/exchange/nobitex.py`
- Modify: `market_maker/order_status.py`
- Modify: `market_maker/portfolio.py`
- Modify: `market_maker/run.py`
- Modify: `market_maker/templates/admin/base.html`
- Modify: `requirements.txt`
- Test: `market_maker/tests/test_nobitex_api_key_auth.py`
- Test: `market_maker/tests/test_config_env.py`
- Test: `market_maker/tests/test_demo_config.py`
- Test: `market_maker/tests/test_demo_entrypoint.py`
- Test: `market_maker/tests/test_demo_safety.py`
- Test: `market_maker/tests/test_real_exchange_defenses.py`

**Interfaces:**
- Consumes: existing `TRADING_PROFILES`, `control_queue`, Flask authentication/CSRF, exchange abstractions, and runtime paths.
- Produces: `signed_headers()`, `desired_config()`, `validate_forward_config()`, forward admin routes, and the forward control agent.

- [ ] **Step 1: Copy only the reviewed source files and hunks**

Use `/tmp/lob-forward-review.Ixtc45` as the read-only teammate snapshot. Apply new files and shared-file diffs without copying excluded artifacts. Review each shared-file diff before staging.

- [ ] **Step 2: Verify the imported authentication contract**

Run:

```bash
python3 -m pytest -q market_maker/tests/test_nobitex_api_key_auth.py
```

Expected: all signing, exact-body, wallet, order, cancellation, and portfolio authentication tests pass.

- [ ] **Step 3: Run focused integration tests**

```bash
python3 -m pytest -q \
  market_maker/tests/test_config_env.py \
  market_maker/tests/test_demo_config.py \
  market_maker/tests/test_demo_entrypoint.py \
  market_maker/tests/test_demo_safety.py \
  market_maker/tests/test_real_exchange_defenses.py
```

Expected: all pass and existing Demo behavior remains intact.

- [ ] **Step 4: Confirm excluded artifacts are absent**

```bash
git status --short
git ls-files | rg '(^|/)(venv|results|__pycache__|\.pytest_cache)(/|$)|run\.py\.before-no-mysql|test_nobitex_api\.ipynb|\.env$'
```

Expected: the second command returns no newly imported teammate artifacts or secrets.

- [ ] **Step 5: Commit the reviewed import**

```bash
git add FORWARD_TEST.md requirements.txt deploy market_maker
git commit -m "feat: integrate isolated Nobitex forward trading"
```

---

### Task 2: Fix forward-admin storage isolation

**Files:**
- Modify: `market_maker/admin_app.py`
- Modify: `deploy/market-maker-forward-admin.service`
- Test: `market_maker/tests/test_admin_html_views.py`
- Test: `market_maker/tests/test_deployment_contract.py`

**Interfaces:**
- Consumes: `create_app(config=None)` and its `FORWARD_*` settings.
- Produces: forward-only mode where Demo mutation routes cannot touch relative storage.

- [ ] **Step 1: Add failing forward-only route tests**

Add a fixture with `FORWARD_ONLY=True`, an absolute `FORWARD_CONTROL_DIR`, and no `CONTROL_DIR`. Assert `/admin/workers/start` returns `404` or `403`, while `/admin/forward/start` submits through the forward control directory.

```python
def test_forward_only_admin_disables_demo_worker_mutations(client, app):
    app.config["FORWARD_ONLY"] = True
    response = authenticated_post(client, "/admin/workers/start", pair="btc")
    assert response.status_code in {403, 404}


def test_forward_action_uses_forward_control_directory(client, app, monkeypatch):
    seen = {}
    monkeypatch.setattr("admin_app.submit_command", lambda directory, **kw: seen.setdefault("directory", directory) or "id")
    authenticated_post(client, "/admin/forward/start", pair="btc", confirmation="START BTCIRT LIVE")
    assert seen["directory"] == app.config["FORWARD_CONTROL_DIR"]
```

- [ ] **Step 2: Run the tests and observe failure**

```bash
python3 -m pytest -q market_maker/tests/test_admin_html_views.py -k 'forward_only or forward_action'
```

Expected: failure because legacy Demo mutations remain reachable or use `runtime/control`.

- [ ] **Step 3: Implement explicit forward-only behavior**

Add `FORWARD_ONLY` to app config from `LOB_FORWARD_ONLY`. Reject or omit Demo mutation routes and Demo control-lock reads when true. Set `Environment=LOB_FORWARD_ONLY=1` in the forward-admin unit. Every forward route continues to use `FORWARD_CONTROL_DIR`, `FORWARD_CONFIG_DIR`, and `FORWARD_LOCK_DIR`.

- [ ] **Step 4: Verify isolation and service contract**

```bash
python3 -m pytest -q \
  market_maker/tests/test_admin_html_views.py \
  market_maker/tests/test_deployment_contract.py
```

Expected: pass; no forward unit relies on writable application-relative paths.

- [ ] **Step 5: Commit**

```bash
git add market_maker/admin_app.py market_maker/tests/test_admin_html_views.py \
  market_maker/tests/test_deployment_contract.py deploy/market-maker-forward-admin.service
git commit -m "fix: isolate forward admin storage"
```

---

### Task 3: Repair worker process and stale-lock lifecycle

**Files:**
- Modify: `deploy/forward_control_agent.py`
- Test: `market_maker/tests/test_forward_control_agent.py`

**Interfaces:**
- Consumes: `AgentPaths`, `_lock_path()`, `_worker_pid()`, `_start_worker()`, `_stop_worker()`, and `run_once()`.
- Produces: `pid_is_expected_worker(pid, pair) -> bool`, `clear_stale_lock(paths, pair) -> bool`, and child reaping during the agent loop.

- [ ] **Step 1: Add regression tests**

Create tests that cover a dead PID lock, a PID belonging to an unrelated process, a normally stopped child, and a child that exits during startup.

```python
def test_dead_pid_lock_is_removed(paths):
    lock = lock_path(paths, "usdt")
    lock.write_text("USDTIRT-live pid=99999999\n")
    assert clear_stale_lock(paths, "usdt") is True
    assert not lock.exists()


def test_stop_reaps_child_and_removes_lock(paths, fake_worker):
    start_worker(paths, "btc")
    stop_worker(paths, "btc")
    assert fake_worker.wait_called
    assert not lock_path(paths, "btc").exists()
```

- [ ] **Step 2: Run the tests and observe failure**

```bash
python3 -m pytest -q market_maker/tests/test_forward_control_agent.py
```

Expected: failures showing stale locks remain or children are not reaped.

- [ ] **Step 3: Implement PID validation, reaping, and lock cleanup**

Track child `Popen` objects by pair in the long-lived agent, call `poll()`/`wait()` to reap exits, validate `/proc/<pid>/cmdline` before signaling, unlink stale locks atomically, and remove locks in `finally` after stop/start failure. Never signal a PID that is not the expected forward worker.

- [ ] **Step 4: Verify lifecycle tests**

```bash
python3 -m pytest -q market_maker/tests/test_forward_control_agent.py
```

Expected: all lifecycle tests pass and no zombie/stale-lock state remains.

- [ ] **Step 5: Commit**

```bash
git add deploy/forward_control_agent.py market_maker/tests/test_forward_control_agent.py
git commit -m "fix: clean up forward worker processes and locks"
```

---

### Task 4: Make fee assumptions explicit and reconcile actual fees

**Files:**
- Modify: `market_maker/config.py`
- Modify: `market_maker/engine.py`
- Modify: `market_maker/order_status.py`
- Modify: `market_maker/forward_config.py`
- Test: `market_maker/tests/test_forward_config.py`
- Test: `market_maker/tests/test_real_exchange_defenses.py`
- Test: `market_maker/tests/test_engine_real_order_scope.py`

**Interfaces:**
- Consumes: existing `commission_perc`, Nobitex order/trade responses, and forward config validation.
- Produces: conservative pre-trade fee assumptions and actual monetary-fee capture for completed fills.

- [ ] **Step 1: Add failing fee tests**

```python
def test_irt_forward_profiles_do_not_understate_base_taker_fee():
    assert TRADING_PROFILES["btc"]["commission_perc"] >= 0.0025
    assert TRADING_PROFILES["usdt"]["commission_perc"] >= 0.0025


def test_trade_fee_is_preserved_from_nobitex_response():
    trade = normalize_trade({"orderId": 7, "fee": "1250.5", "total": "500000"})
    assert trade["fee"] == pytest.approx(1250.5)
```

- [ ] **Step 2: Run focused tests and observe failure**

```bash
python3 -m pytest -q \
  market_maker/tests/test_forward_config.py \
  market_maker/tests/test_real_exchange_defenses.py \
  market_maker/tests/test_engine_real_order_scope.py
```

Expected: failure where BTC/IRT assumes `0.001` or actual fees are discarded.

- [ ] **Step 3: Implement conservative assumptions and reconciliation**

Set the IRT forward baseline to the documented base taker rate `0.0025` unless an explicitly verified account-specific override is saved. Preserve the teammate's order sizes, timing, inventory bounds, and strategy. Persist actual fee amounts returned by Nobitex alongside fill/order records without converting a received-asset fee into a false percentage.

- [ ] **Step 4: Verify fee and strategy behavior**

```bash
python3 -m pytest -q \
  market_maker/tests/test_forward_config.py \
  market_maker/tests/test_real_exchange_defenses.py \
  market_maker/tests/test_engine_real_order_scope.py \
  market_maker/tests/test_algo1_profitability.py
```

Expected: pass; profitability thresholds use the conservative assumption and fill records retain actual fees.

- [ ] **Step 5: Commit**

```bash
git add market_maker/config.py market_maker/engine.py market_maker/order_status.py \
  market_maker/forward_config.py market_maker/tests
git commit -m "fix: account for Nobitex forward trading fees"
```

---

### Task 5: Add Nginx exposure and deployment checks

**Files:**
- Create: `deploy/nginx/lob-forward-http.conf`
- Modify: `FORWARD_TEST.md`
- Test: `market_maker/tests/test_deployment_contract.py`

**Interfaces:**
- Consumes: localhost admin port `18653`, dashboard port `18652`, and existing hostname.
- Produces: authenticated forward UI and read-only charts through Nginx without exposing the agent.

- [ ] **Step 1: Add failing deployment-contract assertions**

Assert the Nginx template targets only `127.0.0.1:18653` and `127.0.0.1:18652`, contains the approved hostname, and never proxies an agent port.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m pytest -q market_maker/tests/test_deployment_contract.py -k forward
```

- [ ] **Step 3: Add the Nginx template and operator checks**

Expose distinct forward admin and charts locations on `server-27920007-d64a-40b2-bf0e-50326d9d3d83.ir-thr-fr1.arvancompute.ir`, preserving existing routes. Document `nginx -t`, localhost probes, public-host probes, and rollback.

- [ ] **Step 4: Verify**

```bash
python3 -m pytest -q market_maker/tests/test_deployment_contract.py
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add deploy/nginx/lob-forward-http.conf FORWARD_TEST.md market_maker/tests/test_deployment_contract.py
git commit -m "ops: expose isolated forward trading interfaces"
```

---

### Task 6: Run complete local verification

**Files:**
- Modify only files required by failures attributable to this branch.

**Interfaces:**
- Consumes: integrated source and tests.
- Produces: release candidate commit with no credentials or excluded artifacts.

- [ ] **Step 1: Scan for credential material**

```bash
git diff main...HEAD -- . ':!docs/superpowers/**' | rg -n 'NOBITEX_(API|SECRET)_KEY=.*[^<]' || true
git grep -n 'dEPBM64pd\|BbhB_lLvn' -- . || true
```

Expected: no output.

- [ ] **Step 2: Run the full test suite**

```bash
python3 -m pytest -q
```

Expected: zero failures.

- [ ] **Step 3: Review final diff and dependency set**

```bash
git status --short
git diff --check main...HEAD
git diff --stat main...HEAD
```

Expected: only planned files, no whitespace errors, and no user-owned untracked file staged.

- [ ] **Step 4: Commit any verified repair**

Use a narrowly scoped commit message matching the repair; do not amend unrelated commits.

---

### Task 7: Back up and deploy to the server

**Files:**
- Server source: `/opt/limit-order-book`
- Server forward source: `/opt/market-maker-forward`
- Server environment: `/etc/market-maker-forward.env`
- Server systemd: `/etc/systemd/system/market-maker-forward-*.service`
- Server Nginx: `/etc/nginx/sites-available/limit-order-book`

**Interfaces:**
- Consumes: verified branch artifact and user-supplied Nobitex credentials.
- Produces: running forward agent/admin/dashboard with rollback material.

- [ ] **Step 1: Capture pre-deployment state and create a timestamped backup**

Record service status, open worker PIDs, open-order state, active Nginx config, and deployed commit. Copy the current forward source, units, Nginx site, and environment file to an explicit timestamped directory under `/opt/deployment-backups/` with root-only permissions.

- [ ] **Step 2: Correct DNS before deployment**

Verify:

```bash
getent ahostsv4 api.nobitex.ir
curl --fail --silent --show-error --max-time 10 https://api.nobitex.ir/v2/options >/dev/null
```

Expected: at least one address and successful TLS HTTP response. If not, repair the server resolver and re-run before continuing.

- [ ] **Step 3: Install source and dependencies**

Synchronize only tracked deployment files into `/opt/market-maker-forward`, preserve runtime directories, install `requirements.txt` into the dedicated virtual environment, and ensure ownership/permissions match the systemd users.

- [ ] **Step 4: Install credentials securely**

Write exact `KEY=value` assignments for `FORWARD_TRADING_ENABLED`, `NOBITEX_API_KEY`, and `NOBITEX_SECRET_KEY`; preserve only still-required non-secret settings. Set owner `root:root` and mode `0600`. Validate variable presence and decoded key lengths without printing values.

- [ ] **Step 5: Install and validate units and Nginx**

```bash
systemd-analyze verify /etc/systemd/system/market-maker-forward-*.service
nginx -t
systemctl daemon-reload
```

Expected: no errors.

- [ ] **Step 6: Restart services and verify no-order health**

```bash
systemctl restart market-maker-forward-agent market-maker-forward-admin market-maker-forward-dashboard
systemctl --no-pager --full status market-maker-forward-agent market-maker-forward-admin market-maker-forward-dashboard
```

Probe localhost and Nginx endpoints, inspect journals for tracebacks, read BTC/RLS/USDT wallets through the command queue, and confirm no real worker process is active yet.

- [ ] **Step 7: Roll back on any failed check**

Restore the timestamped source, env, units, and Nginx configuration; reload systemd/Nginx; restart the previous services; report the failing evidence.

---

### Task 8: Run and stop the monitored real-forward validation

**Files:**
- Runtime audit: `/var/lib/market-maker-forward/control/audit.log`
- Runtime results: `/var/lib/market-maker-forward/results`
- Runtime logs: `/var/log/market-maker-forward`

**Interfaces:**
- Consumes: authenticated forward control actions and teammate BTC/USDT configuration.
- Produces: a 10-minute evidence record of real order behavior and clean shutdown.

- [ ] **Step 1: Capture balances, configs, and open orders before start**

Record only non-secret balances, effective BTC/USDT configs, existing owned open orders, and current lock/process state.

- [ ] **Step 2: Start BTC/IRT and USDT/IRT through the control plane**

Use the existing confirmation phrases `START BTCIRT LIVE` and `START USDTIRT LIVE`. Confirm both start results are successful, lock PIDs are live expected workers, and logs show real mode with the correct symbols.

- [ ] **Step 3: Monitor for 10 minutes**

At intervals, inspect worker health, order ownership, fills, actual returned fees, risk-limit rejections, logs, and audit results. Stop both immediately on authentication, balance, repeated API, ownership, risk, or cancellation errors.

- [ ] **Step 4: Stop both workers through the control plane**

Confirm SIGTERM completes, owned open orders are cancelled, child processes are reaped, and both live lock files are removed.

- [ ] **Step 5: Produce the final deployment report**

Report branch commits, test count, deployed source identity, service/domain health, starting and ending balances, orders/fills/fees, shutdown cancellation results, and any remaining risk. Do not include credentials, signatures, or private account profile data.

