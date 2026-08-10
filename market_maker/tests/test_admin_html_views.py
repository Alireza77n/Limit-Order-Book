"""HTML rendering / content-negotiation layer for the admin control panel.

The JSON API (admin_app.py's existing routes) is already fully covered by
test_admin_actions.py / test_admin_readonly_views.py / test_admin_auth.py --
this file only covers the gap: browsers must get real HTML pages with
working forms, while existing JSON callers (anything that doesn't send an
Accept header, exactly like Flask's test client and this project's own
existing tests) must keep getting byte-for-byte the same JSON as before.

Negotiation rule under test: Accept: text/html -> HTML; no Accept header or
Accept: application/json -> JSON (see admin_app._wants_html).
"""

import json
import re
from pathlib import Path

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

import admin_app as admin_app_module
from admin_app import create_app
from config import TRADING_PROFILES, demo_csv_path
from demo_config import desired_digest, load_desired_config, write_runtime_snapshot

HTML = {"Accept": "text/html"}

EXPECTED_CONFIG_FIELDS = {
    "order_size", "inv_max", "inv_min", "cancel_after_minutes",
    "depth_max", "time_step", "demo_inv_initial", "cash_initial",
    "commission_perc",
    "buy_locked_cash_limit_enabled", "buy_locked_cash_limit_ratio",
    "sell_locked_inventory_limit_enabled", "sell_locked_inventory_limit_ratio",
}
REMOVED_CONFIG_FIELDS = {
    "max_rounds", "demo_level1_fast_fill_enabled",
    "demo_level1_fill_delay_seconds", "demo_level1_fill_fraction",
    "demo_queue_enabled", "no_quote_alert_rounds", "no_quote_alert_seconds",
    "no_quote_alert_cooldown_seconds", "no_quote_recovery_alert",
    "algo1_diagnostics_enabled",
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
def client(app):
    return app.test_client()


@pytest.fixture
def fake_agent(monkeypatch):
    def _fake_await_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {"command_id": command_id, "ok": True, "detail": {}, "finished_at": "now"}
    monkeypatch.setattr(admin_app_module, "await_result", _fake_await_result)


def _commands_written(app):
    commands_dir = Path(app.config["CONTROL_DIR"]) / "commands"
    if not commands_dir.exists():
        return []
    return [json.loads(p.read_text()) for p in commands_dir.glob("*.json")]


def _login(client):
    login_page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True)).group(1)
    client.post("/admin/login", data={"username": "admin", "password": "correct horse", "csrf_token": token})
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _html(response):
    return response.get_data(as_text=True)


# ---------- Login page is always HTML ----------

def test_login_page_is_html_with_form():
    from admin_app import create_app as _create
    app = _create({"ADMIN_USERNAME": "a", "ADMIN_PASSWORD_HASH": "x", "SECRET_KEY": "s"})
    response = app.test_client().get("/admin/login")
    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    body = _html(response)
    assert "<form" in body
    assert 'name="csrf_token"' in body


def test_login_page_keeps_static_assets_under_admin_proxy():
    app = create_app({"TESTING": True})

    response = app.test_client().get("/admin/login")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'href="/admin/static/admin/admin.css"' in body
    assert 'src="/admin/static/admin/admin.js"' in body


# ---------- Overview: HTML for browsers, JSON preserved by default ----------

def test_overview_default_still_returns_json(client):
    """Regression guard: every pre-existing test calls routes with no Accept
    header and expects JSON -- this must keep working unchanged."""
    _login(client)
    response = client.get("/admin/")
    assert response.status_code == 200
    assert response.get_json()["workers"] == {"btc": False, "usdt": False}


def test_overview_html_for_browser_accept_header(client):
    token = _login(client)
    response = client.get("/admin/", headers=HTML)
    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    body = _html(response)
    assert "DEMO" in body.upper() and "PAPER" in body.upper()
    # worker controls reachable and CSRF-protected
    assert 'action="/admin/workers/start"' in body or "admin_workers" not in body  # rendered url, not endpoint name
    assert 'name="csrf_token"' in body


def test_overview_explicit_json_accept_returns_json(client):
    _login(client)
    response = client.get("/admin/", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.get_json()["workers"] == {"btc": False, "usdt": False}


def test_unauthenticated_html_request_redirects_to_login(client):
    response = client.get("/admin/", headers=HTML)
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


# ---------- Workers page ----------

def test_workers_page_renders_controls(client):
    _login(client)
    response = client.get("/admin/workers", headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    assert 'action="/admin/workers/start"' in body
    assert 'action="/admin/workers/stop"' in body
    assert 'action="/admin/workers/restart"' in body
    assert 'name="csrf_token"' in body


def test_worker_start_html_rerenders_with_result_and_still_enqueues(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/workers/start", data={"pair": "btc", "csrf_token": token}, headers=HTML)
    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    written = _commands_written(app)
    assert len(written) == 1
    assert written[0]["action"] == "start_worker"


def test_worker_start_json_unchanged_when_not_html(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/workers/start", data={"pair": "btc", "csrf_token": token})
    assert response.status_code == 200
    assert response.get_json()["results"][0]["ok"] is True


def test_failed_worker_lifecycle_html_reports_agent_error_without_success_flash(
    client, monkeypatch,
):
    def _failed_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {
            "command_id": command_id,
            "ok": False,
            "detail": {"error": "systemctl stop failed", "phase": "stop"},
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", _failed_result)
    token = _login(client)

    response = client.post(
        "/admin/workers/stop",
        data={"pair": "btc", "csrf_token": token},
        headers=HTML,
    )

    assert response.status_code == 502
    body = _html(response)
    assert "systemctl stop failed" in body
    assert "stop" in body
    assert "flash-error" in body
    assert "Stop requested" not in body
    assert "flash-success" not in body


def test_failed_new_session_html_reports_agent_error_without_success_flash(
    client, monkeypatch,
):
    def _failed_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {
            "command_id": command_id,
            "ok": False,
            "detail": {"error": "worker restart failed", "phase": "start"},
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", _failed_result)
    token = _login(client)

    response = client.post(
        "/admin/session/new", data={"csrf_token": token}, headers=HTML,
    )

    assert response.status_code == 502
    body = _html(response)
    assert "worker restart failed" in body
    assert "start" in body
    assert "flash-error" in body
    assert "New demo session created" not in body
    assert "flash-success" not in body


def test_failed_worker_lifecycle_json_contract_is_unchanged(client, monkeypatch):
    def _failed_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {
            "command_id": command_id,
            "ok": False,
            "detail": {"error": "systemctl stop failed", "phase": "stop"},
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", _failed_result)
    token = _login(client)

    response = client.post(
        "/admin/workers/stop", data={"pair": "btc", "csrf_token": token},
    )

    assert response.status_code == 200
    assert response.get_json()["results"][0]["ok"] is False


QUEUE_BACKED_HTML_ROUTES = [
    ("post", "/admin/workers/stop", {"pair": "btc"}, "stop_worker"),
    ("post", "/admin/session/new", {}, "new_session"),
    ("post", "/admin/orders/cancel", {"pair": "btc"}, "cancel_orders"),
    ("post", "/admin/backups/create", {}, "create_backup"),
    (
        "post", "/admin/backups/manual-1/restore",
        {"confirm": "RESTORE"}, "restore_backup",
    ),
    ("post", "/admin/reset", {"confirm": "RESET"}, "reset_demo"),
    ("post", "/admin/recover/apply", {}, "recover_demo"),
    ("get", "/admin/export", {}, "create_backup"),
    ("post", "/admin/config/btc", {"order_size": "3.5"}, "apply_config"),
    ("post", "/admin/config/btc", {"action": "revert"}, "rollback_config"),
]


@pytest.mark.parametrize(
    ("method", "path", "data", "expected_action"),
    QUEUE_BACKED_HTML_ROUTES,
    ids=[
        "worker", "new-session", "cancel", "backup-create", "backup-restore",
        "reset", "recover", "export", "config-apply", "config-rollback",
    ],
)
def test_every_queue_backed_html_route_reports_failed_control_result(
    client, app, monkeypatch, method, path, data, expected_action,
):
    def _failed_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {
            "command_id": command_id,
            "ok": False,
            "detail": {
                "error": f"simulated {expected_action} failure",
                "phase": "execute",
            },
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", _failed_result)
    token = _login(client)
    submitted = dict(data)
    if method == "post":
        submitted["csrf_token"] = token

    response = client.open(path, method=method.upper(), data=submitted, headers=HTML)

    assert response.status_code == 502
    body = _html(response)
    assert f"simulated {expected_action} failure" in body
    assert "execute" in body
    assert "flash-success" not in body
    assert _commands_written(app)[0]["action"] == expected_action


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/admin/orders/cancel", {"pair": "btc"}),
        ("/admin/backups/create", {}),
        ("/admin/backups/manual-1/restore", {"confirm": "RESTORE"}),
        ("/admin/reset", {"confirm": "RESET"}),
        ("/admin/recover/apply", {}),
    ],
    ids=["cancel", "backup-create", "backup-restore", "reset", "recover"],
)
def test_failed_remaining_mutating_routes_keep_json_contract(
    client, monkeypatch, path, data,
):
    def _failed_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {
            "command_id": command_id,
            "ok": False,
            "detail": {"error": "agent failed", "phase": "execute"},
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", _failed_result)
    submitted = dict(data, csrf_token=_login(client))

    response = client.post(path, data=submitted)

    assert response.status_code == 200
    body = response.get_json()
    result = body["results"][0] if "results" in body else body
    assert result["ok"] is False


# ---------- Config pages ----------

@pytest.mark.parametrize("pair", ["btc", "usdt"])
def test_config_page_renders_exact_pair_schema(client, pair):
    _login(client)
    response = client.get(f"/admin/config/{pair}", headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    for field in EXPECTED_CONFIG_FIELDS:
        assert f'name="{field}"' in body
    for field in REMOVED_CONFIG_FIELDS:
        assert f'name="{field}"' not in body
    assert 'name="csrf_token"' in body


def test_config_page_shows_effective_profile_defaults_when_no_overrides_set(client):
    """Before this fix, unoverridden fields rendered blank (load_overrides()
    is sparse -- only ever contains fields an admin has explicitly changed).
    The form must show the actual value a worker is running with -- the
    profile default -- not blank, even with zero overrides on disk."""
    _login(client)
    response = client.get("/admin/config/btc", headers=HTML)
    body = _html(response)
    match = re.search(r'name="order_size"[^>]*value="([^"]*)"', body)
    assert match and match.group(1) != ""
    assert float(match.group(1)) == pytest.approx(TRADING_PROFILES["btc"]["order_size"])
    match = re.search(r'name="commission_perc"[^>]*value="([^"]*)"', body)
    assert float(match.group(1)) == pytest.approx(TRADING_PROFILES["btc"]["commission_perc"])


def test_config_page_renders_zero_defaults_instead_of_blank(client):
    _login(client)

    response = client.get("/admin/config/btc", headers=HTML)
    body = _html(response)

    for field in ("demo_inv_initial", "inv_min"):
        match = re.search(rf'name="{field}"[^>]*value="([^"]*)"', body)
        assert match and match.group(1) != ""
        assert float(match.group(1)) == pytest.approx(0.0)


def test_config_page_layers_override_on_top_of_profile_default(client, app):
    """One field overridden -- that field shows the override, every other
    field still shows its profile default, not blank."""
    from demo_config import overlay_path, write_overrides
    path = overlay_path(app.config["STATE_DIR"], "btc")
    write_overrides(
        path, {"order_size": 0.0002}, pair="btc", profiles=TRADING_PROFILES,
    )
    _login(client)
    response = client.get("/admin/config/btc", headers=HTML)
    body = _html(response)
    match = re.search(r'name="order_size"[^>]*value="([^"]*)"', body)
    assert float(match.group(1)) == pytest.approx(0.0002)
    match = re.search(r'name="depth_max"[^>]*value="([^"]*)"', body)
    assert float(match.group(1)) == pytest.approx(TRADING_PROFILES["btc"]["depth_max"])


def test_config_apply_html_success_rerenders_with_success_message(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/config/btc", data={
        "order_size": "3.5", "csrf_token": token,
    }, headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    assert "Configuration saved." in body
    assert "flash-success" in body
    written = _commands_written(app)
    assert written[0]["action"] == "apply_config"
    assert set(written[0]["payload"]["overrides"]) == EXPECTED_CONFIG_FIELDS
    assert written[0]["payload"]["overrides"]["order_size"] == 3.5


def test_config_apply_html_validation_error_rerenders_with_error(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/config/btc", data={
        "commission_perc": "5.0", "csrf_token": token,
    }, headers=HTML)
    assert response.status_code == 400
    assert response.content_type.startswith("text/html")
    body = _html(response)
    assert "commission_perc" in body
    assert _commands_written(app) == []


def test_config_page_shows_modes_statuses_and_separate_json_views(
    client, app, monkeypatch,
):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    snapshot = {
        "schema_version": 1,
        "pair": "btc",
        "session_id": "session-1",
        "applied": dict(desired),
        "pending_new_session": {},
        "desired_digest": desired_digest(desired),
        "status": "applied",
        "error": None,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    write_runtime_snapshot(state_dir, "btc", snapshot)
    (state_dir / "stack_session.json").write_text(json.dumps({"session_id": "session-1"}))
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda _app, _pair: True)
    _login(client)

    body = _html(client.get("/admin/config/btc", headers=HTML))

    assert "Desired configuration" in body
    assert "Applied runtime configuration" in body
    assert "Applied now" in body
    assert 'data-mode="hot"' in body
    assert 'data-mode="new_session"' in body


@pytest.mark.parametrize("session_document", [None, {"session_id": ""}], ids=["missing", "invalid"])
def test_config_page_shows_stale_without_current_session_identity(
    client, app, monkeypatch, session_document,
):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    snapshot = {
        "schema_version": 1,
        "pair": "btc",
        "session_id": "session-1",
        "applied": dict(desired),
        "pending_new_session": {},
        "desired_digest": desired_digest(desired),
        "status": "applied",
        "error": None,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    write_runtime_snapshot(state_dir, "btc", snapshot)
    if session_document is not None:
        (state_dir / "stack_session.json").write_text(json.dumps(session_document))
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda _app, _pair: True)
    _login(client)

    body = _html(client.get("/admin/config/btc", headers=HTML))

    assert 'data-config-status="stale"' in body
    assert "Runtime status unknown/stale" in body
    assert "Applied now" not in body


def test_config_page_shows_old_digest_worker_error_as_failed(
    client, app, monkeypatch,
):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    old_desired = dict(desired, order_size=desired["order_size"] * 2)
    snapshot = {
        "schema_version": 1,
        "pair": "btc",
        "session_id": "session-1",
        "applied": dict(old_desired),
        "pending_new_session": {},
        "desired_digest": desired_digest(old_desired),
        "status": "error",
        "error": "OSError: snapshot publish failed",
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    write_runtime_snapshot(state_dir, "btc", snapshot)
    (state_dir / "stack_session.json").write_text(json.dumps({"session_id": "session-1"}))
    monkeypatch.setattr(admin_app_module, "_worker_running", lambda _app, _pair: True)
    _login(client)

    body = _html(client.get("/admin/config/btc", headers=HTML))

    assert 'data-config-status="failed"' in body
    assert "Failed to apply" in body
    assert "OSError: snapshot publish failed" in body


@pytest.mark.parametrize(("status", "error"), [
    ("applied", "unexpected worker error"),
    ("error", None),
])
def test_config_page_shows_inconsistent_snapshot_status_and_error_as_stale(
    client, app, status, error,
):
    state_dir = Path(app.config["STATE_DIR"])
    desired = load_desired_config("btc", state_dir, TRADING_PROFILES)
    snapshot = {
        "schema_version": 1,
        "pair": "btc",
        "session_id": "session-1",
        "applied": dict(desired),
        "pending_new_session": {},
        "desired_digest": desired_digest(desired),
        "status": status,
        "error": error,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    write_runtime_snapshot(state_dir, "btc", snapshot)
    _login(client)

    body = _html(client.get("/admin/config/btc", headers=HTML))

    assert 'data-config-status="stale"' in body
    assert "unexpected worker error" not in body


def test_failed_config_command_reports_agent_error_without_success_flash(
    client, app, monkeypatch,
):
    def _failed_result(control_dir, command_id, timeout=30.0, poll_interval=0.1):
        return {
            "command_id": command_id,
            "ok": False,
            "detail": {
                "error": "overlay write failed: disk full",
                "pair": "btc",
                "phase": "persist",
            },
            "finished_at": "now",
        }

    monkeypatch.setattr(admin_app_module, "await_result", _failed_result)
    token = _login(client)

    response = client.post(
        "/admin/config/btc",
        data={"order_size": "3.5", "csrf_token": token},
        headers=HTML,
    )

    assert response.status_code >= 400
    body = _html(response)
    assert "overlay write failed: disk full" in body
    assert "persist" in body
    assert 'data-config-status="failed"' in body
    assert "Failed to apply" in body
    assert "Configuration applied" not in body
    assert "Configuration saved" not in body
    assert "flash-success" not in body


# ---------- Orders: server-side filtering the template relies on ----------

ORDERS_ROWS = [
    {"Session_id": "s1", "Timestamp": "2026-08-01 12:00:00", "Round": 1,
     "Best Bid": 99.0, "Best Ask": 100.0, "Bid Price Sent": 98.0, "Ask Price Sent": 101.0,
     "Buy Order ID": "b1", "Buy Status": "open", "Sell Order ID": None, "Sell Status": None},
    {"Session_id": "s1", "Timestamp": "2026-08-01 12:01:00", "Round": 2,
     "Best Bid": 99.0, "Best Ask": 100.0, "Bid Price Sent": 98.0, "Ask Price Sent": 101.0,
     "Buy Order ID": None, "Buy Status": None, "Sell Order ID": "s1o", "Sell Status": "filled"},
]


@pytest.fixture
def orders_app(tmp_path):
    for name in ("control", "state", "results", "backups"):
        (tmp_path / name).mkdir()
    csv_name = Path(demo_csv_path(TRADING_PROFILES["btc"]["output_csv"])).name
    pd.DataFrame(ORDERS_ROWS).to_csv(tmp_path / "results" / csv_name, index=False)
    return create_app({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": generate_password_hash("correct horse", method="pbkdf2:sha256"),
        "SECRET_KEY": "test-secret",
        "CONTROL_DIR": str(tmp_path / "control"),
        "STATE_DIR": str(tmp_path / "state"),
        "RESULTS_DIR": str(tmp_path / "results"),
        "BACKUPS_DIR": str(tmp_path / "backups"),
        "TESTING": True,
    })


def test_orders_status_filter_narrows_results(orders_app):
    client = orders_app.test_client()
    _login(client)
    all_response = client.get("/admin/orders?pair=btc")
    assert len(all_response.get_json()["orders"]) == 2
    filtered = client.get("/admin/orders?pair=btc&status=filled")
    orders = filtered.get_json()["orders"]
    assert len(orders) == 1
    assert orders[0]["Sell Status"] == "filled"


def test_orders_html_renders_table(orders_app):
    client = orders_app.test_client()
    _login(client)
    response = client.get("/admin/orders?pair=btc", headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    assert "<table" in body
    assert "s1" in body


# ---------- Balances ----------

def test_balances_html_includes_fee_scopes(app):
    client = app.test_client()
    (Path(app.config["STATE_DIR"]) / "stack_session.json").write_text(json.dumps({"session_id": "s1"}))
    fee_path = Path(app.config["STATE_DIR"]) / "paper_fee_scopes.json"
    fee_path.write_text(json.dumps({
        # An older, no-longer-active session's fees -- must NOT appear.
        "sessions": {"s0-stale": {"BTCIRT": 999.99}, "s1": {"BTCIRT": 12.5}},
        "all_time": {"BTCIRT": 1012.49},
    }))
    ledger_path = Path(app.config["STATE_DIR"]) / "paper_s1_btcirt.json"
    ledger_path.write_text(json.dumps({
        "account": {"inventory": 0.1, "cash": 500.0, "valuation_price": 100.0, "fees_quote": 12.5},
    }))
    _login(client)
    response = client.get("/admin/balances", headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    assert "12.50" in body
    assert "999.99" not in body


def test_balances_ignores_stale_session_ledger_file(app):
    """A ledger left over from a session that isn't the current one anymore
    must not be shown as if it were current -- this was the actual bug."""
    client = app.test_client()
    (Path(app.config["STATE_DIR"]) / "stack_session.json").write_text(json.dumps({"session_id": "s-new"}))
    # Old session's ledger, written (and thus most-recently-modified) before
    # the new session started -- must be ignored now, not shown as current.
    stale = Path(app.config["STATE_DIR"]) / "paper_s-old_btcirt.json"
    stale.write_text(json.dumps({"account": {"inventory": 99.0, "cash": 99.0}}))
    _login(client)
    response = client.get("/admin/balances")
    assert response.status_code == 200
    assert response.get_json()["btc"] is None


def test_balances_html_shows_starting_amount_before_any_round(app):
    """No ledger yet for the current session (fresh session, worker hasn't
    completed a round) must show the configured starting cash/inventory,
    not a bare empty state -- the JSON API keeps returning null there."""
    client = app.test_client()
    (Path(app.config["STATE_DIR"]) / "stack_session.json").write_text(json.dumps({"session_id": "brand-new"}))
    _login(client)

    html_response = client.get("/admin/balances", headers=HTML)
    assert html_response.status_code == 200
    body = _html(html_response)
    assert "No ledger data available" not in body
    assert f"{TRADING_PROFILES['btc']['cash_initial']:.2f}" in body
    assert "Starting balance" in body

    json_response = client.get("/admin/balances")
    assert json_response.get_json()["btc"] is None


# ---------- Backups: create, restore confirmation, per-backup download ----------

def test_backups_page_html(client):
    _login(client)
    response = client.get("/admin/backups", headers=HTML)
    assert response.status_code == 200
    assert 'action="/admin/backups/create"' in _html(response)


def test_backup_restore_still_requires_exact_confirm_via_html(client, app, fake_agent):
    token = _login(client)
    (Path(app.config["BACKUPS_DIR"]) / "manual-1").mkdir()
    response = client.post("/admin/backups/manual-1/restore",
                            data={"confirm": "nope", "csrf_token": token}, headers=HTML)
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_backup_download_serves_named_backup_not_a_fresh_one(client, app, monkeypatch):
    """Regression test for the pre-existing bug: the per-row Download link
    called admin_export (which always creates a brand-new backup) instead
    of downloading the backup actually listed in that row."""
    _login(client)
    backups_dir = Path(app.config["BACKUPS_DIR"])
    named = backups_dir / "manual-1"
    named.mkdir()
    (named / "paper_x_btcirt.json").write_text('{"a": 1}')

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("download must not create a new backup via the control queue")
    monkeypatch.setattr(admin_app_module, "submit_command", _fail_if_called)

    response = client.get("/admin/backups/manual-1/download")
    assert response.status_code == 200
    import io
    import zipfile
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    assert archive.namelist() == ["paper_x_btcirt.json"]


def test_backup_download_unknown_name_404s(client):
    _login(client)
    response = client.get("/admin/backups/does-not-exist/download")
    assert response.status_code == 404


# ---------- Recovery / reset ----------

def test_recovery_html_shows_reset_preview(app):
    client = app.test_client()
    (Path(app.config["STATE_DIR"]) / "btc_track_demo.csv").write_text("x\n")
    _login(client)
    response = client.get("/admin/recover", headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    assert "btc_track_demo.csv" in body


def test_reset_wrong_confirm_html_shows_error_no_command(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/reset", data={"confirm": "nope", "csrf_token": token}, headers=HTML)
    assert response.status_code == 400
    assert _commands_written(app) == []


def test_reset_exact_confirm_html_succeeds(client, app, fake_agent):
    token = _login(client)
    response = client.post("/admin/reset", data={"confirm": "RESET", "csrf_token": token}, headers=HTML)
    assert response.status_code == 200
    written = _commands_written(app)
    assert written[0]["action"] == "reset_demo"


# ---------- Logs / audit ----------

def test_logs_html_escapes_message_content(app):
    client = app.test_client()
    archive_path = Path(app.config["STATE_DIR"]) / "paper_s1_btcirt_archive.jsonl"
    archive_path.write_text(json.dumps({
        "timestamp": "2026-08-01T12:00:00", "level": "INFO",
        "message": "<script>alert(1)</script>",
    }) + "\n")
    _login(client)
    response = client.get("/admin/logs?pair=btc", headers=HTML)
    assert response.status_code == 200
    body = _html(response)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_audit_html_renders_entries(client, app):
    from control_queue import append_audit
    append_audit(app.config["CONTROL_DIR"],
                 {"action": "start_worker", "pair": "btc", "ok": True, "command_id": "cmd-123"})
    _login(client)
    response = client.get("/admin/audit", headers=HTML)
    assert response.status_code == 200
    assert "start_worker" in _html(response)


# ---------- Audit-entry-per-action invariant holds over the HTML path too ----------

def test_one_audit_entry_per_action_via_html(client, app, fake_agent):
    token = _login(client)
    client.post("/admin/workers/start", data={"pair": "both", "csrf_token": token}, headers=HTML)
    audit_lines = (Path(app.config["CONTROL_DIR"]) / "audit.log").read_text().splitlines()
    assert len(audit_lines) == 2


# ---------- Friendly error pages ----------

def test_unknown_admin_route_404_is_friendly_html(client):
    _login(client)
    response = client.get("/admin/does-not-exist", headers=HTML)
    assert response.status_code == 404
    assert response.content_type.startswith("text/html")


def test_csrf_failure_html_is_friendly_not_plain_text(client):
    _login(client)
    response = client.post("/admin/logout", data={}, headers=HTML)
    assert response.status_code == 400
    assert response.content_type.startswith("text/html")


def test_operation_in_progress_html_is_friendly(client, app, fake_agent):
    from control_queue import ControlLock
    token = _login(client)
    lock = ControlLock(app.config["CONTROL_DIR"])
    lock.acquire()
    try:
        response = client.post("/admin/workers/start", data={"pair": "btc", "csrf_token": token}, headers=HTML)
        assert response.status_code == 409
        assert response.content_type.startswith("text/html")
    finally:
        lock.release()
