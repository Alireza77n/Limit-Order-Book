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
    "order_size", "inv_max", "inv_min", "cancel_after_minutes",
    "depth_max", "time_step", "demo_inv_initial", "cash_initial",
    "commission_perc",
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


@pytest.mark.parametrize("field", [
    "max_rounds",
    "demo_level1_fast_fill_enabled",
    "demo_level1_fill_delay_seconds",
    "demo_level1_fill_fraction",
    "demo_queue_enabled",
    "no_quote_alert_rounds",
    "no_quote_alert_seconds",
    "no_quote_alert_cooldown_seconds",
    "no_quote_recovery_alert",
    "algo1_diagnostics_enabled",
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


def test_config_json_separates_desired_applied_and_status(client, app, monkeypatch):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    (state_dir / "runtime_config_btc.json").write_text(json.dumps(_runtime_snapshot(desired)))
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda _app, _pair: True)
    _login(client)

    body = client.get("/admin/config/btc").get_json()

    assert body["desired"] == desired
    assert body["applied"] == desired
    assert body["status"]["state"] == "applied"
    assert set(body["schema"]) == EXPECTED_FIELDS


def test_config_json_reports_malformed_runtime_snapshot_as_stale(client, app):
    state_dir = Path(app.config["STATE_DIR"])
    (state_dir / "runtime_config_btc.json").write_text("{")
    _login(client)

    response = client.get("/admin/config/btc")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"]["state"] == "stale"
    assert "invalid JSON" in body["runtime_error"]


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
