import json
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

import admin_app as admin_app_module
from admin_app import create_app
from control_queue import ControlLock


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
    assert written[0]["payload"]["overrides"] == {"order_size": 3.5}


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
