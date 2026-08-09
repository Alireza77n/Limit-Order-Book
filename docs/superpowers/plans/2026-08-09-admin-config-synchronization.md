# Admin Configuration Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Admin edits persist once, apply according to explicit hot/new-session semantics, and show the same desired/applied state in Admin, workers, and Dashboard.

**Architecture:** `demo_config.py` becomes the sole schema, validation, desired-resolution, and runtime-snapshot module. Workers reload safe values at round boundaries and publish applied snapshots; session-owned values remain pending until a fresh ledger is created. The existing host control agent remains the only writer/controller and gains an explicit local mode.

**Tech Stack:** Python 3, Flask/Jinja, Dash, pytest, atomic JSON files, Docker Compose, existing command queue/control agent.

## Global Constraints

- Demo/paper mode only; do not mutate Real Mode configuration.
- Do not change Algorithm 1 decision logic.
- Admin and Dashboard never receive Docker socket or host process authority.
- Overlay JSON is the only writable desired source; runtime snapshots are worker-owned acknowledgements.
- Work in the current dirty checkout and preserve all unrelated edits.
- Stage explicit files/hunks only; never commit runtime files, screenshots, credentials, or unrelated changes.

---

### Task 1: Central pair-aware schema and strict desired resolver

**Files:**
- Modify: `market_maker/demo_config.py`
- Modify: `market_maker/tests/test_demo_config.py`

**Interfaces:**
- Produces: `ApplicationMode`, `FieldDefinition`, `editable_schema(pair, profiles)`, `load_desired_config(pair, state_dir, profiles)`, `desired_digest(values)`, and strict `OverlayLoadError`.
- Consumes: existing `TRADING_PROFILES` passed by callers, avoiding a `demo_config` → `config` import cycle.

- [ ] **Step 1: Write failing schema tests**

Add tests asserting the exact Admin fields and modes:

```python
EXPECTED = {
    "order_size": "hot", "inv_max": "hot", "inv_min": "hot",
    "cancel_after_minutes": "hot", "depth_max": "hot", "time_step": "hot",
    "demo_inv_initial": "new_session", "cash_initial": "new_session",
    "commission_perc": "new_session",
}

@pytest.mark.parametrize("pair", ["btc", "usdt"])
def test_editable_schema_is_pair_profile_intersection(pair):
    schema = editable_schema(pair, config.TRADING_PROFILES)
    assert {key: field.mode.value for key, field in schema.items()} == EXPECTED
    assert set(schema) <= set(config.TRADING_PROFILES[pair])

def test_obsolete_and_continuous_only_fields_are_not_editable():
    assert "no_quote_recovery_alert" not in editable_schema("btc", config.TRADING_PROFILES)
    assert "max_rounds" not in editable_schema("btc", config.TRADING_PROFILES)
```

- [ ] **Step 2: Run the schema tests and verify RED**

Run: `python3 -m pytest -q market_maker/tests/test_demo_config.py -k 'editable_schema or obsolete'`

Expected: import/name failures because the pair-aware API does not exist.

- [ ] **Step 3: Add strict persistence/resolution tests**

Cover base+overlay merge, pair isolation, cross-field validation, legacy-key cleanup, and explicit corrupt-file failure:

```python
def test_load_desired_config_layers_only_pair_editable_values(tmp_path):
    write_overrides(overlay_path(tmp_path, "btc"), {"order_size": 0.25}, pair="btc", profiles=PROFILES)
    desired = load_desired_config("btc", tmp_path, PROFILES)
    assert desired["order_size"] == 0.25
    assert desired["time_step"] == PROFILES["btc"]["time_step"]
    assert load_desired_config("usdt", tmp_path, PROFILES)["order_size"] == PROFILES["usdt"]["order_size"]

def test_corrupt_overlay_is_an_explicit_error(tmp_path):
    overlay_path(tmp_path, "btc").write_text("{")
    with pytest.raises(OverlayLoadError, match="invalid JSON"):
        load_desired_config("btc", tmp_path, PROFILES)

@pytest.mark.parametrize("values", [
    {"inv_min": 2, "inv_max": 1},
    {"order_size": 0},
])
def test_cross_field_validation_rejects_invalid_desired_config(tmp_path, values):
    with pytest.raises(OverlayValidationError):
        write_overrides(overlay_path(tmp_path, "btc"), values, pair="btc", profiles=PROFILES)
```

- [ ] **Step 4: Implement the minimal centralized schema and resolver**

Use immutable metadata and require pair context for new writes:

```python
from dataclasses import dataclass
from enum import Enum

class ApplicationMode(str, Enum):
    HOT = "hot"
    RESTART = "restart"
    NEW_SESSION = "new_session"

@dataclass(frozen=True)
class FieldDefinition:
    value_type: type
    minimum: Optional[float]
    maximum: Optional[float]
    mode: ApplicationMode
    help_text: str

FIELD_DEFINITIONS = {
    "order_size": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Base-asset amount per order round."),
    "inv_max": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Maximum inventory the worker will hold."),
    "inv_min": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Minimum inventory the worker will hold."),
    "cancel_after_minutes": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Age before resting paper orders are cancelled."),
    "depth_max": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Maximum order-book depth used by the strategy."),
    "time_step": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Seconds between quoting rounds."),
    "demo_inv_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting inventory for a new Demo session."),
    "cash_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting quote cash for a new Demo session."),
    "commission_perc": FieldDefinition(float, 0, 1, ApplicationMode.NEW_SESSION, "Simulated fee fraction for a new Demo session."),
}

def editable_schema(pair, profiles):
    profile = profiles[pair]
    return {key: definition for key, definition in FIELD_DEFINITIONS.items() if key in profile}

def load_desired_config(pair, state_dir, profiles):
    base = {key: profiles[pair][key] for key in editable_schema(pair, profiles)}
    return dict(base, **load_overrides(overlay_path(state_dir, pair), pair=pair, profiles=profiles))
```

Keep backward-compatible read migration only for the known ten removed keys; unknown keys fail closed.

- [ ] **Step 5: Run Task 1 tests and verify GREEN**

Run: `python3 -m pytest -q market_maker/tests/test_demo_config.py market_maker/tests/test_run_make_cfg.py`

Expected: all tests pass.

---

### Task 2: Worker-owned applied snapshots and safe hot reload

**Files:**
- Modify: `market_maker/demo_config.py`
- Modify: `market_maker/config.py`
- Modify: `market_maker/run.py`
- Modify: `market_maker/engine.py`
- Create: `market_maker/tests/test_runtime_config_sync.py`

**Interfaces:**
- Consumes: Task 1 `load_desired_config()` and schema modes.
- Produces: `runtime_snapshot_path()`, `read_runtime_snapshot()`, `write_runtime_snapshot()`, and `RuntimeConfigSynchronizer.reload_at_round_boundary()`.

- [ ] **Step 1: Write failing hot-reload behavior tests**

Build one existing Demo bot, update its overlay, call the wished-for reload seam, and assert the same `cfg`/strategy object changes atomically:

```python
@pytest.mark.parametrize("field,value", [
    ("order_size", 2.0), ("inv_max", 20.0), ("inv_min", 1.0),
    ("cancel_after_minutes", 3.0), ("depth_max", 4), ("time_step", 2),
])
def test_hot_field_updates_existing_engine(field, value, runtime_bot):
    bot, state_dir = runtime_bot
    original_cfg = bot.cfg
    write_overrides(overlay_path(state_dir, "usdt"), {field: value}, pair="usdt", profiles=TRADING_PROFILES)
    result = bot.config_sync.reload_at_round_boundary()
    assert result.ok
    assert bot.cfg is original_cfg
    assert getattr(bot.cfg, field) == value
    assert bot.strategy.cfg is original_cfg
```

Add a behavior assertion per consumer: order sizing, inventory cap/skew, stale cancellation age, depth cap, and next scheduling interval. Add one invalid-batch test proving no field changes.

- [ ] **Step 2: Run hot-reload tests and verify RED**

Run: `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py -k hot`

Expected: `config_sync`/snapshot APIs are missing.

- [ ] **Step 3: Add failing snapshot tests**

```python
def test_runtime_snapshot_reports_applied_and_pending_values(runtime_bot):
    bot, state_dir = runtime_bot
    write_overrides(overlay_path(state_dir, "usdt"), {
        "order_size": 2.0, "commission_perc": 0.02,
    }, pair="usdt", profiles=TRADING_PROFILES)
    bot.config_sync.reload_at_round_boundary()
    snapshot = read_runtime_snapshot(state_dir, "usdt")
    assert snapshot["applied"]["order_size"] == 2.0
    assert snapshot["pending_new_session"]["commission_perc"] == 0.02
    assert snapshot["session_id"] == str(bot.session)
```

- [ ] **Step 4: Implement runtime synchronization**

Add internal `profile_name` and `demo_config_state_dir` fields to `MMConfig`.
Construct the synchronizer in `make_bot()` only for Demo mode. At the top of
each `_run_demo()` loop iteration call:

```python
reload_result = self.config_sync.reload_at_round_boundary()
if not reload_result.ok:
    self._record_config_reload_error(reload_result.error)
```

The synchronizer validates the whole desired document, copies only `HOT`
fields, compares new-session desired values with ledger-owned effective values,
and atomically writes the snapshot. It retains the previous applied values on
any load/validation error.

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

Run: `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py market_maker/tests/test_demo_engine_lifecycle.py market_maker/tests/test_worker_lifecycle.py`

Expected: all tests pass without changing Algorithm 1 baselines.

---

### Task 3: Session-bound truth across restart and session rotation

**Files:**
- Modify: `market_maker/run.py`
- Modify: `market_maker/paper.py` only if a read-only effective-config accessor is needed
- Modify: `market_maker/tests/test_runtime_config_sync.py`
- Modify: `market_maker/tests/test_demo_engine_lifecycle.py`

**Interfaces:**
- Consumes: Task 2 runtime synchronizer/snapshot.
- Produces: a same-session restart that keeps ledger commission authoritative and a new session that consumes all pending values.

- [ ] **Step 1: Write the failing same-session restart test**

```python
def test_same_session_restart_keeps_commission_pending(tmp_path, monkeypatch):
    first = make_runtime_bot(tmp_path, session="s1", commission=0.001)
    first.exchange.ledger._persist()
    save_desired(tmp_path, "usdt", commission_perc=0.02)
    restarted = make_runtime_bot(tmp_path, session="s1")
    assert restarted.cfg.commission_perc == 0.001
    assert restarted.exchange.ledger.commission_perc == 0.001
    assert snapshot(tmp_path, "usdt")["pending_new_session"]["commission_perc"] == 0.02
```

- [ ] **Step 2: Run it and verify RED**

Run: `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py::test_same_session_restart_keeps_commission_pending`

Expected: cfg and loaded ledger disagree or pending status is absent.

- [ ] **Step 3: Write the failing new-session test**

```python
def test_new_session_consumes_pending_inventory_cash_and_commission(tmp_path):
    save_desired(tmp_path, "usdt", demo_inv_initial=7, cash_initial=900, commission_perc=0.02)
    bot = make_runtime_bot(tmp_path, session="s2")
    ledger = bot.exchange.ledger
    assert (ledger.inventory, ledger.cash, ledger.commission_perc) == (7, 900, 0.02)
    assert snapshot(tmp_path, "usdt")["pending_new_session"] == {}
```

- [ ] **Step 4: Reconcile cfg with loaded ledger and publish truth**

When `make_bot()` loads an existing ledger, explicitly set
`cfg.commission_perc = ledger.commission_perc` before constructing the strategy
and snapshot. Initial cash/inventory are reported from ledger account state;
they are never rewritten mid-session.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run: `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py market_maker/tests/test_demo_safety.py market_maker/tests/test_demo_engine_lifecycle.py`

Expected: all tests pass.

---

### Task 4: Truthful control-agent apply and local mode

**Files:**
- Modify: `deploy/control_agent.py`
- Modify: `market_maker/tests/test_control_agent.py`
- Modify: `deploy/systemd/lob-control-agent.service` only if CLI defaults require an explicit production flag

**Interfaces:**
- Consumes: Task 1 validation/digest/modes.
- Produces: `build_agent_paths(mode, app_root)`, fixed production/local Compose argv maps, apply results without unnecessary restart, and exact lifecycle failure propagation.

- [ ] **Step 1: Write failing apply-mode and lifecycle tests**

```python
def test_apply_config_persists_without_restarting_worker(paths):
    calls = []
    result = handle_apply_config(paths, "btc", {"overrides": {"order_size": 0.2}}, lambda argv, **kw: calls.append(argv))
    assert result.ok
    assert calls == []
    assert result.detail["modes"] == {"order_size": "hot"}

def test_new_session_aborts_if_a_running_worker_cannot_stop(paths, running_workers):
    result = handle_new_session(paths, None, None, failing_stop_runner)
    assert not result.ok
    assert result.detail["phase"] == "stop"
```

- [ ] **Step 2: Write failing local-path/argv tests**

```python
def test_local_mode_uses_root_compose_and_market_maker_results(repo_root):
    paths = build_agent_paths("local", repo_root)
    assert paths.state_dir == repo_root / "market_maker/results"
    assert resolve_worker_argv("restart_worker", "btc", mode="local")[-2:] == ["restart", "btc-demo"]
    assert "deploy/docker-compose.production.yml" not in resolve_worker_argv("restart_worker", "btc", mode="local")
```

- [ ] **Step 3: Run Task 4 tests and verify RED**

Run: `python3 -m pytest -q market_maker/tests/test_control_agent.py`

Expected: current handler restarts and local mode does not exist.

- [ ] **Step 4: Implement fixed local/production agent modes and failure propagation**

Add `--mode {production,local}` with production as the systemd-safe default.
Both modes use fixed argument arrays. `handle_apply_config` returns persisted
digest/modes and never claims worker application. `handle_new_session` checks
every stop and start result, returning `ok=False` with pair/action details for
partial failure.

- [ ] **Step 5: Run Task 4 tests and verify GREEN**

Run: `python3 -m pytest -q market_maker/tests/test_control_agent.py market_maker/tests/test_deployment_contract.py`

Expected: all tests pass.

---

### Task 5: Pair-aware Admin fields and effective statuses

**Files:**
- Modify: `market_maker/admin_app.py`
- Modify: `market_maker/templates/admin/config.html`
- Modify: `market_maker/tests/test_admin_actions.py`
- Modify: `market_maker/tests/test_admin_html_views.py`

**Interfaces:**
- Consumes: Task 1 desired resolver/schema and Task 2 runtime snapshots.
- Produces: `config_status(pair, desired, snapshot, worker_running)` and truthful status UI/JSON.

- [ ] **Step 1: Write failing field-contract tests**

```python
@pytest.mark.parametrize("pair", ["btc", "usdt"])
def test_admin_renders_exact_pair_schema(auth_client, pair):
    body = auth_client.get(f"/admin/config/{pair}", headers=HTML).get_data(as_text=True)
    for field in EXPECTED_FIELDS:
        assert f'name="{field}"' in body
    for field in REMOVED_FIELDS:
        assert f'name="{field}"' not in body

def test_removed_field_submission_is_rejected_without_enqueue(auth_client):
    response = post_config(auth_client, "btc", {"no_quote_recovery_alert": "true"})
    assert response.status_code == 400
    assert not queued_commands()
```

- [ ] **Step 2: Write failing status tests**

Cover applied digest match, hot mismatch/worker running, stopped worker,
pending-new-session, stale snapshot, and failed command. Assert a failed queue
result never renders the success flash.

- [ ] **Step 3: Run Task 5 tests and verify RED**

Run: `python3 -m pytest -q market_maker/tests/test_admin_actions.py market_maker/tests/test_admin_html_views.py -k 'config'`

Expected: obsolete fields still render and status metadata is absent.

- [ ] **Step 4: Replace global UI iteration with pair schema and status rows**

The GET path reads desired and applied separately. The POST path rejects any
submitted editable-looking key outside the selected schema, validates the
complete submitted set, and reports queue failure verbatim. Render each field's
mode badge and effective status; label the JSON blocks “Desired configuration”
and “Applied runtime configuration”.

- [ ] **Step 5: Run Task 5 tests and verify GREEN**

Run: `python3 -m pytest -q market_maker/tests/test_admin_auth.py market_maker/tests/test_admin_actions.py market_maker/tests/test_admin_html_views.py market_maker/tests/test_admin_demo_safety.py`

Expected: all tests pass.

---

### Task 6: Dashboard effective-config display and Compose state visibility

**Files:**
- Modify: `market_maker/dashboard.py`
- Modify: `market_maker/tests/test_dashboard.py`
- Modify: `docker-compose.yml`
- Modify: `deploy/docker-compose.production.yml`
- Modify: `market_maker/tests/test_deployment_contract.py`

**Interfaces:**
- Consumes: shared desired resolver and runtime snapshot reader.
- Produces: `load_effective_demo_config(track_key)` and a Dash panel refreshed by the existing interval.

- [ ] **Step 1: Write failing Dashboard data tests**

```python
def test_dashboard_config_uses_selected_pair_and_reports_pending(tmp_path, monkeypatch):
    configure_state_dir(monkeypatch, tmp_path)
    save_desired(tmp_path, "btc", order_size=0.2, commission_perc=0.02)
    write_snapshot(tmp_path, "btc", applied={"order_size": 0.2, "commission_perc": 0.001})
    view = dashboard.load_effective_demo_config("btc_demo")
    assert view["pair"] == "btc"
    assert view["applied"]["order_size"] == 0.2
    assert view["pending_new_session"]["commission_perc"] == 0.02

def test_real_track_has_no_demo_config_panel():
    assert dashboard.load_effective_demo_config("btc_real") is None
```

- [ ] **Step 2: Write failing deployment mount test**

Assert production Dashboard mounts `../runtime/state:/app/runtime/state:ro`,
local Dashboard sees `market_maker/results`, and neither web service mounts
`/var/run/docker.sock`.

- [ ] **Step 3: Run Task 6 tests and verify RED**

Run: `python3 -m pytest -q market_maker/tests/test_dashboard.py market_maker/tests/test_deployment_contract.py -k 'config or dashboard'`

Expected: loader/panel and production state mount are missing.

- [ ] **Step 4: Implement the read-only Dashboard panel**

Add a compact table with Field, Desired, Applied, Mode, and Status columns.
Drive it from the existing `tick` input and selected track. Reuse shared status
formatting data rather than importing Flask/Admin code.

- [ ] **Step 5: Run Task 6 tests and verify GREEN**

Run: `python3 -m pytest -q market_maker/tests/test_dashboard.py market_maker/tests/test_deployment_contract.py`

Expected: all tests pass.

---

### Task 7: Documentation, full regression, and Docker end-to-end validation

**Files:**
- Modify: `docs/deployment/ADMIN_PAGE.md`
- Modify: `usage.md`
- Modify: `README.md` only where its architecture/run summary is stale
- Create: `docs/reports/2026-08-09-admin-config-synchronization-validation.md` if reports are intentionally tracked for this repository; otherwise record results in the final response only

**Interfaces:**
- Consumes: all prior tasks.
- Produces: operator instructions, field/mode mapping, reproducible validation evidence, and a scoped Git update.

- [ ] **Step 1: Run focused configuration suites**

Run:

```bash
python3 -m pytest -q \
  market_maker/tests/test_demo_config.py \
  market_maker/tests/test_runtime_config_sync.py \
  market_maker/tests/test_control_agent.py \
  market_maker/tests/test_admin_auth.py \
  market_maker/tests/test_admin_actions.py \
  market_maker/tests/test_admin_html_views.py \
  market_maker/tests/test_dashboard.py \
  market_maker/tests/test_deployment_contract.py
```

Expected: zero failures.

- [ ] **Step 2: Run the full regression suite**

Run: `python3 -m pytest -q`

Expected: zero failures. If a failure predates this work, prove it against the
pre-change baseline before classifying it as unrelated.

- [ ] **Step 3: Validate Docker Compose definitions**

Run:

```bash
docker compose config --quiet
docker compose -f deploy/docker-compose.production.yml --env-file deploy/demo.env config --quiet
```

Expected: both commands exit 0.

- [ ] **Step 4: Execute the local end-to-end scenario**

Start Compose and the local host agent, then authenticate as `admin/admin` and:

1. Submit distinct BTC/USDT hot and new-session values.
2. Assert both overlay JSON files contain only their pair values.
3. Wait at most two round boundaries and assert runtime snapshots show hot
   values applied and new-session values pending.
4. Fetch Dashboard state/callback output and assert it matches snapshots.
5. Restart both worker services and assert desired values persist, hot values
   remain applied, and session values remain pending in the same session.
6. Submit `new_session`; assert the session id changes and new ledgers use the
   requested inventory, cash, and commission.
7. Recreate the Compose stack and assert overlays/snapshots remain coherent.

Capture exact commands, session ids, digests, HTTP statuses, and pass/fail
checks without recording credentials beyond the approved local `admin/admin`.

- [ ] **Step 5: Update operator documentation**

Document the nine fields, six hot/three new-session modes, local agent command,
status meanings, persistence rules, and the fact that no current field requires
a restart. Remove instructions claiming local config actions simply time out.

- [ ] **Step 6: Inspect and stage only scoped changes**

Run `git diff --check`, inspect `git diff -- <each touched path>`, and use
path/hunk-specific staging. Do not stage the pre-existing Algorithm 1, paper
fill, screenshot, Playwright, or runtime-control changes. If a touched file has
inseparable pre-existing hunks, leave it unstaged and report it rather than
committing someone else's work.

- [ ] **Step 7: Commit the scoped implementation/documentation update**

Use a commit message such as:

```text
fix: synchronize admin configuration with runtime
```

Then report root causes, changed files, removed fields, exact field mapping,
application modes, synchronization flow, automated results, Docker evidence,
and remaining limitations.
