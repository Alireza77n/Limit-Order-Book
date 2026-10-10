import json
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

import admin_app as admin_app_module
from admin_app import create_app
from config import TRADING_PROFILES
from control_queue import ControlLock
from demo_config import config_status, desired_digest, load_desired_config


EXPECTED_FIELDS = {
    "inventory_sizing_enabled", "inventory_sizing_strength",
    "inventory_sizing_min_fraction",
    "order_size", "inv_max", "inv_min", "cancel_after_minutes",
    "depth_max", "time_step", "demo_inv_initial", "cash_initial",
    "commission_perc", "exp_ret_min",
    "buy_locked_cash_limit_enabled", "buy_locked_cash_limit_ratio",
    "sell_locked_inventory_limit_enabled", "sell_locked_inventory_limit_ratio",
    "grid_min_perc", "grid_ratio", "grid_max_perc", "skew_perc",
    "one_sided_lo", "one_sided_hi", "cost_basis_reference",
    "ev_min", "adverse_selection_perc", "hysteresis_ticks", "refresh_seconds",
    "fill_table_enabled", "fill_table_max_age_hours", "fill_table_min_samples",
    "fill_kappa", "fill_lambda_tau", "fill_queue_volume_ref",
}


@pytest.fixture
def app(tmp_path):
    for name in ("control", "state", "results", "backups"):
        (tmp_path / name).mkdir()
    application = create_app({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": generate_password_hash("correct horse", method="pbkdf2:sha256"),
        "SECRET_KEY": "test-secret",
        "CONTROL_DIR": str(tmp_path / "control"),
        "STATE_DIR": str(tmp_path / "state"),
        "RESULTS_DIR": str(tmp_path / "results"),
        "BACKUPS_DIR": str(tmp_path / "backups"),
        "TESTING": True,
    })
    return application


@pytest.fixture
def fake_agent(monkeypatch):
    """Stub out await_result so tests don't need a real control_agent
    process consuming the queue -- returns a canned success immediately.
    Commands are still written for real by submit_command, so tests can
    inspect the queue directory to assert what was enqueued."""
    calls = []

    def _fake_await_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        calls.append(command_id)
        return {"command_id": command_id, "ok": True, "detail": {}, "finished_at": "now"}

    monkeypatch.setattr(admin_app_module, "await_result", _fake_await_result)
    return calls


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client):
    login_page = client.get("/admin/login")
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True)).group(1)
    client.post("/admin/login", data={"username": "admin", "password": "correct horse", "csrf_token": token})
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _commands_written(app):
    commands_dir = Path(app.config["CONTROL_DIR"]) / "commands"
    if not commands_dir.exists():
        return []
    return [json.loads(p.read_text()) for p in commands_dir.glob("*.json")]


def _runtime_snapshot(desired, *, applied=None, pair="btc", status="applied", **updates):
    document = {
        "schema_version": 1,
        "pair": pair,
        "session_id": "session-1",
        "applied": dict(desired if applied is None else applied),
        "pending_new_session": {},
        "desired_digest": desired_digest(desired),
        "status": status,
        "error": None,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    document.update(updates)
    return document


def test_pair_both_enqueues_both_pairs(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/workers/start", data={"pair": "both", "csrf_token": token})
    assert response.status_code == 200
    written = _commands_written(app)
    pairs = sorted(c["pair"] for c in written)
    assert pairs == ["btc", "usdt"]
    assert all(c["action"] == "start_worker" for c in written)


def test_single_pair_enqueues_one_command(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/workers/stop", data={"pair": "btc", "csrf_token": token})
    assert response.status_code == 200
    written = _commands_written(app)
    assert len(written) == 1
    assert written[0]["pair"] == "btc"
    assert written[0]["action"] == "stop_worker"


def test_invalid_pair_rejected(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/workers/stop", data={"pair": "eth", "csrf_token": token})
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_config_apply_validates_before_enqueueing(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/config/btc", data={
        "commission_perc": "5.0",  # out of [0,1] range
        "csrf_token": token,
    })
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_config_apply_rejects_equal_inventory_bounds_without_enqueueing(
    client, app, fake_agent,
):
    token = _login(client)

    response = client.post("/admin/config/btc", data={
        "inv_min": "2.0",
        "inv_max": "2.0",
        "csrf_token": token,
    })

    assert response.status_code == 400
    assert b"inv_min must be < inv_max" in response.data
    assert _commands_written(app) == []


def test_config_apply_valid_enqueues_apply_config(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/config/btc", data={
        "order_size": "3.5",
        "csrf_token": token,
    })
    assert response.status_code == 200
    written = _commands_written(app)
    assert len(written) == 1
    assert written[0]["action"] == "apply_config"
    submitted = written[0]["payload"]["overrides"]
    assert set(submitted) == EXPECTED_FIELDS
    assert submitted["order_size"] == 3.5
    assert submitted["depth_max"] == TRADING_PROFILES["btc"]["depth_max"]


def test_config_apply_accepts_inventory_sizing_fields(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/config/usdt", data={
        "inventory_sizing_enabled": "false",
        "inventory_sizing_strength": "0.5",
        "inventory_sizing_min_fraction": "0.2",
        "csrf_token": token,
    })
    assert response.status_code == 200
    submitted = _commands_written(app)[0]["payload"]["overrides"]
    assert submitted["inventory_sizing_enabled"] is False
    assert submitted["inventory_sizing_strength"] == 0.5
    assert submitted["inventory_sizing_min_fraction"] == 0.2


def test_config_apply_rejects_inventory_sizing_strength_above_one(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/config/usdt", data={
        "inventory_sizing_strength": "1.5",
        "csrf_token": token,
    })
    assert response.status_code == 400
    assert _commands_written(app) == []


@pytest.mark.parametrize("field", [
    "max_rounds",
    "demo_level1_fast_fill_enabled",
    "demo_level1_fill_delay_seconds",
    "demo_level1_fill_fraction",
    "no_quote_alert_rounds",
    "no_quote_alert_seconds",
    "no_quote_alert_cooldown_seconds",
    "no_quote_recovery_alert",
    "algo1_diagnostics_enabled",
    "demo_queue_enabled",
    "demo_max_fill_per_observation_orders",
    "demo_adverse_fill_enabled",
    "demo_adverse_fill_probability",
    "demo_non_adverse_fill_probability",
    "not_a_real_config_field",
])
def test_config_submission_outside_pair_schema_is_rejected_without_enqueue(
    client, app, fake_agent, field,
):
    token = _login(client)
    response = client.post(
        "/admin/config/btc", data={field: "1", "csrf_token": token},
    )

    assert response.status_code == 400
    assert _commands_written(app) == []


def test_config_revert_with_outside_schema_field_is_rejected_without_enqueue(
    client, app, fake_agent,
):
    token = _login(client)

    response = client.post("/admin/config/btc", data={
        "action": "revert",
        "no_quote_recovery_alert": "true",
        "csrf_token": token,
    })

    assert response.status_code == 400
    assert _commands_written(app) == []


def test_config_revert_html_derives_status_from_shared_runtime_snapshot(
    client, app, monkeypatch,
):
    from demo_config import overlay_path, rollback_overrides, write_overrides

    token = _login(client)
    state_dir = Path(app.config["STATE_DIR"])
    path = overlay_path(state_dir, "btc")
    previous = load_desired_config("btc", state_dir, TRADING_PROFILES)
    previous["order_size"] = 1.0
    current = dict(previous, order_size=2.0)
    write_overrides(path, previous, pair="btc", profiles=TRADING_PROFILES)
    write_overrides(path, current, pair="btc", profiles=TRADING_PROFILES)
    (state_dir / "runtime_config_btc.json").write_text(
        json.dumps(_runtime_snapshot(current))
    )
    (state_dir / "stack_session.json").write_text(
        json.dumps({"session_id": "session-1"})
    )
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda *_args: True)

    def execute_rollback(_app, action, pair, payload=None):
        assert action == "rollback_config"
        rollback_overrides(path, pair=pair, profiles=TRADING_PROFILES)
        return {"ok": True, "detail": {"status": "saved"}}

    monkeypatch.setattr(admin_app_module, "enqueue_and_wait", execute_rollback)

    response = client.post(
        "/admin/config/btc",
        data={"action": "revert", "csrf_token": token},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert b"Saved; waiting for running worker" in response.data


def test_config_status_reports_applied_digest_match():
    desired = {"order_size": 1.0, "commission_perc": 0.001}

    view = config_status("btc", desired, _runtime_snapshot(desired), True)

    assert view["state"] == "applied"
    assert view["label"] == "Applied now"
    assert view["fields"]["order_size"] == {
        "desired": 1.0,
        "applied": 1.0,
        "mode": "hot",
        "state": "applied",
        "label": "Applied now",
    }


def test_config_status_reports_hot_mismatch_waiting_for_running_worker():
    desired = {"order_size": 2.0}
    snapshot = _runtime_snapshot(
        {"order_size": 1.0},
        applied={"order_size": 1.0},
    )

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "waiting"
    assert view["fields"]["order_size"]["state"] == "waiting"
    assert view["fields"]["order_size"]["label"] == "Saved; waiting for running worker"


def test_config_status_requires_digest_ack_even_when_values_happen_to_match():
    desired = {"order_size": 1.0}
    snapshot = _runtime_snapshot(
        {"order_size": 2.0},
        applied={"order_size": 1.0},
    )

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "waiting"
    assert view["fields"]["order_size"]["state"] == "waiting"


def test_config_status_reports_stopped_worker_without_claiming_application():
    desired = {"order_size": 2.0}
    snapshot = _runtime_snapshot(
        {"order_size": 1.0},
        applied={"order_size": 1.0},
    )

    view = config_status("btc", desired, snapshot, False)

    assert view["state"] == "stopped"
    assert view["fields"]["order_size"]["label"] == "Worker stopped; applies when started"


def test_config_status_reports_new_session_value_as_pending():
    desired = {"commission_perc": 0.02}
    snapshot = _runtime_snapshot(
        desired,
        applied={"commission_perc": 0.001},
        pending_new_session={"commission_perc": 0.02},
    )

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "pending_new_session"
    assert view["fields"]["commission_perc"]["mode"] == "new_session"
    assert view["fields"]["commission_perc"]["label"] == "Saved; applies next session"


def test_config_status_uses_current_desired_value_for_unacknowledged_pending_session():
    desired = {"commission_perc": 0.02}
    snapshot = _runtime_snapshot(
        {"commission_perc": 0.001},
        applied={"commission_perc": 0.001},
    )

    view = config_status("btc", desired, snapshot, True)

    assert view["pending_new_session"] == {"commission_perc": 0.02}
    assert view["fields"]["commission_perc"]["state"] == "pending_new_session"


@pytest.mark.parametrize("snapshot", [
    None,
    {"schema_version": 0, "pair": "btc"},
    {"schema_version": 1, "pair": "usdt"},
])
def test_config_status_treats_missing_stale_or_mismatched_snapshot_as_stale(snapshot):
    view = config_status("btc", {"order_size": 1.0}, snapshot, True)

    assert view["state"] == "stale"
    assert view["label"] == "Runtime status unknown/stale"
    assert view["fields"]["order_size"]["state"] == "stale"


@pytest.mark.parametrize(("status", "error"), [
    ("applied", "unexpected worker error"),
    ("error", None),
    ("error", ""),
])
def test_config_status_rejects_inconsistent_snapshot_status_and_error(status, error):
    desired = {"order_size": 1.0}
    snapshot = _runtime_snapshot(desired, status=status, error=error)

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "stale"
    assert view["label"] == "Runtime status unknown/stale"
    assert view["error"] is None


@pytest.mark.parametrize("updates", [
    {"applied": {"order_size": "one"}},
    {"applied": {"order_size": -1.0}},
    {"applied": {"order_size": 1.0, "not_a_real_field": 1}},
    {"pending_new_session": {"order_size": 1.0}},
    {"pending_new_session": {"commission_perc": 2.0}},
    {"pending_new_session": {"not_a_real_field": 1}},
])
def test_config_status_rejects_snapshot_values_outside_pair_schema(updates):
    desired = {"order_size": 1.0}
    snapshot = _runtime_snapshot(desired, **updates)

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "stale"
    assert view["applied"] == {}


def test_config_status_rejects_snapshot_from_another_stack_session():
    desired = {"order_size": 1.0}
    snapshot = _runtime_snapshot(desired, session_id="old-session")

    view = config_status(
        "btc", desired, snapshot, True, current_session_id="current-session",
    )

    assert view["state"] == "stale"


def test_config_status_reports_worker_snapshot_error_as_failed():
    desired = {"order_size": 1.0}
    snapshot = _runtime_snapshot(
        desired,
        status="error",
        error="OverlayLoadError: invalid JSON",
    )

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "failed"
    assert view["label"] == "Failed to apply"
    assert view["error"] == "OverlayLoadError: invalid JSON"


def test_config_status_reports_preserved_old_digest_error_as_failed():
    desired = {"order_size": 2.0}
    snapshot = _runtime_snapshot(
        {"order_size": 1.0},
        applied={"order_size": 1.0},
        status="error",
        error="OSError: snapshot publish failed",
    )

    view = config_status("btc", desired, snapshot, True)

    assert view["state"] == "failed"
    assert view["label"] == "Failed to apply"
    assert view["error"] == "OSError: snapshot publish failed"
    assert view["fields"]["order_size"]["state"] == "failed"


def test_config_json_separates_desired_applied_and_status(client, app, monkeypatch):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    (state_dir / "runtime_config_btc.json").write_text(json.dumps(_runtime_snapshot(desired)))
    (state_dir / "stack_session.json").write_text(json.dumps({"session_id": "session-1"}))
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda _app, _pair: True)
    _login(client)

    body = client.get("/admin/config/btc").get_json()

    assert body["desired"] == desired
    assert body["applied"] == desired
    assert body["status"]["state"] == "applied"
    assert set(body["schema"]) == EXPECTED_FIELDS


@pytest.mark.parametrize("session_document", [None, {"session_id": ""}], ids=["missing", "invalid"])
def test_config_json_treats_snapshot_as_stale_without_current_session_identity(
    client, app, monkeypatch, session_document,
):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    (state_dir / "runtime_config_btc.json").write_text(json.dumps(_runtime_snapshot(desired)))
    if session_document is not None:
        (state_dir / "stack_session.json").write_text(json.dumps(session_document))
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda _app, _pair: True)
    _login(client)

    body = client.get("/admin/config/btc").get_json()

    assert body["status"]["state"] == "stale"
    assert body["status"]["label"] == "Runtime status unknown/stale"
    assert body["applied"] == {}


def test_config_json_reports_malformed_runtime_snapshot_as_stale(client, app):
    state_dir = Path(app.config["STATE_DIR"])
    (state_dir / "runtime_config_btc.json").write_text("{")
    _login(client)

    response = client.get("/admin/config/btc")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"]["state"] == "stale"
    assert "invalid JSON" in body["runtime_error"]


@pytest.mark.parametrize(("status", "error"), [
    ("applied", "unexpected worker error"),
    ("error", None),
])
def test_config_json_reports_inconsistent_snapshot_status_and_error_as_stale(
    client, app, status, error,
):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    snapshot = _runtime_snapshot(desired, status=status, error=error)
    (state_dir / "runtime_config_btc.json").write_text(json.dumps(snapshot))
    _login(client)

    body = client.get("/admin/config/btc").get_json()

    assert body["status"]["state"] == "stale"
    assert body["status"]["error"] is None


def test_config_json_reports_corrupt_desired_overlay_explicitly(client, app):
    state_dir = Path(app.config["STATE_DIR"])
    (state_dir / "demo_config_btc.json").write_text("{")
    _login(client)

    response = client.get("/admin/config/btc")

    assert response.status_code == 500
    assert "invalid JSON" in response.get_json()["error"]


def test_one_audit_entry_per_completed_action(client, app, fake_agent):
    token = _login(client)
    client.post("/admin/workers/start", data={"pair": "both", "csrf_token": token})
    audit_lines = (Path(app.config["CONTROL_DIR"]) / "audit.log").read_text().splitlines()
    assert len(audit_lines) == 2  # one per pair-action completed


def test_concurrent_request_while_locked_gets_in_progress_response(client, app, fake_agent):
    token = _login(client)
    lock = ControlLock(app.config["CONTROL_DIR"])
    lock.acquire()
    try:
        response = client.post("/admin/workers/start", data={"pair": "btc", "csrf_token": token})
        assert response.status_code == 409
        assert _commands_written(app) == []
    finally:
        lock.release()


def test_control_agent_unreachable_reports_clear_timeout_not_500(client, app, monkeypatch):
    from control_queue import ResultTimeout

    def _timeout(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        raise ResultTimeout(f"no result for {command_id} within {timeout}s")

    monkeypatch.setattr(admin_app_module, "await_result", _timeout)
    token = _login(client)
    response = client.post("/admin/workers/start", data={"pair": "btc", "csrf_token": token})
    assert response.status_code == 504
    assert "control agent" in response.get_data(as_text=True).lower()


def test_status_route_reports_session_and_workers(client, app):
    (Path(app.config["STATE_DIR"]) / "stack_session.json").write_text(json.dumps({"session_id": "demo-123"}))
    _login(client)
    response = client.get("/admin/")
    assert response.status_code == 200
    body = response.get_json()
    assert body["session_id"] == "demo-123"
    assert body["workers"] == {"btc": False, "usdt": False}


def test_reset_rejected_without_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/reset", data={"confirm": "yes please", "csrf_token": token})
    assert response.status_code == 400
    assert _commands_written(app) == []
    assert "affected_files" in response.get_json()


def test_reset_accepted_with_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/reset", data={"confirm": "RESET", "csrf_token": token})
    assert response.status_code == 200
    written = _commands_written(app)
    assert len(written) == 1
    assert written[0]["action"] == "reset_demo"
    assert written[0]["payload"]["confirm"] == "RESET"


def test_restore_rejected_without_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/backups/manual-1/restore", data={"confirm": "no", "csrf_token": token})
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_restore_accepted_with_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post(
        "/admin/backups/manual-1/restore",
        data={"confirm": "RESTORE", "csrf_token": token},
    )
    assert response.status_code == 200
    written = _commands_written(app)
    assert written[0]["action"] == "restore_backup"
    assert written[0]["payload"]["backup_name"] == "manual-1"


def test_recover_get_is_readonly_and_does_not_enqueue(client, app):
    _login(client)
    response = client.get("/admin/recover")
    assert response.status_code == 200
    assert _commands_written(app) == []


def test_recover_apply_only_enqueues_recover_demo_never_reset(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/recover/apply", data={"csrf_token": token})
    assert response.status_code == 200
    written = _commands_written(app)
    assert len(written) == 1
    assert written[0]["action"] == "recover_demo"


def test_export_streams_zip_built_only_from_backups_dir(client, app, monkeypatch):
    """Prove export never reads runtime/results or runtime/state directly:
    monkeypatch await_result to fabricate a backup pointing at a directory
    we control, and assert the zip contents come only from there."""
    backups_dir = Path(app.config["BACKUPS_DIR"])
    snapshot_dir = backups_dir / "export-snap"
    snapshot_dir.mkdir()
    (snapshot_dir / "paper_x_btcirt.json").write_text('{"a":1}')
    # A file placed directly in RESULTS_DIR (never copied into the snapshot)
    # must not appear in the export -- proves the zip is backups-only.
    (Path(app.config["RESULTS_DIR"]) / "btc_track_demo.csv").write_text("live,data\n")

    def _fake_await_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {"command_id": command_id, "ok": True, "detail": {"backup": "export-snap"}, "finished_at": "now"}

    monkeypatch.setattr(admin_app_module, "await_result", _fake_await_result)
    _login(client)
    response = client.get("/admin/export")
    assert response.status_code == 200
    import zipfile
    archive = zipfile.ZipFile(__import__("io").BytesIO(response.get_data()))
    assert archive.namelist() == ["paper_x_btcirt.json"]


def test_delete_session_rejected_without_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post(
        "/admin/sessions/s1/delete", data={"confirm": "yes", "csrf_token": token},
    )
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_delete_session_accepted_with_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post(
        "/admin/sessions/s1/delete", data={"confirm": "DELETE", "csrf_token": token},
    )
    assert response.status_code == 200
    written = _commands_written(app)
    assert len(written) == 1
    assert written[0]["action"] == "delete_session"
    assert written[0]["pair"] is None
    assert written[0]["payload"] == {"session_id": "s1", "confirm": "DELETE"}


def test_delete_session_failure_reports_502(client, app, monkeypatch):
    def failing_await(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {"command_id": command_id, "ok": False, "detail": {"error": "session not found"}, "finished_at": "now"}

    monkeypatch.setattr(admin_app_module, "await_result", failing_await)
    token = _login(client)
    response = client.post(
        "/admin/sessions/never-existed/delete", data={"confirm": "DELETE", "csrf_token": token},
    )
    assert response.status_code == 502


def test_delete_sessions_bulk_rejected_without_exact_confirm(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/sessions/delete", data={
        "session_ids": ["s1", "s2"], "confirm": "yes", "csrf_token": token,
    })
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_delete_sessions_bulk_rejected_when_none_selected(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/sessions/delete", data={
        "confirm": "DELETE", "csrf_token": token,
    })
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_delete_sessions_bulk_enqueues_one_command_per_selected_session(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/sessions/delete", data={
        "session_ids": ["s1", "s2", "s3"], "confirm": "DELETE", "csrf_token": token,
    })
    assert response.status_code == 200
    written = _commands_written(app)
    assert len(written) == 3
    assert {w["payload"]["session_id"] for w in written} == {"s1", "s2", "s3"}
    assert all(w["action"] == "delete_session" for w in written)
    assert all(w["pair"] is None for w in written)
    assert all(w["payload"]["confirm"] == "DELETE" for w in written)


def test_delete_sessions_bulk_partial_failure_reports_502_but_processes_every_session(
    client, app, monkeypatch,
):
    calls = []

    def selective_failure(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        calls.append(command_id)
        # Fail every other one, so the batch has a genuine mix of outcomes.
        ok = len(calls) % 2 == 1
        return {
            "command_id": command_id, "ok": ok,
            "detail": {} if ok else {"error": "session not found"},
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", selective_failure)
    token = _login(client)
    response = client.post("/admin/sessions/delete", data={
        "session_ids": ["s1", "s2"], "confirm": "DELETE", "csrf_token": token,
    })
    assert response.status_code == 502
    assert len(calls) == 2  # both were attempted despite the first result
