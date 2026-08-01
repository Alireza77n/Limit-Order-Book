"""Task 7 -- admin read-only views must reuse dashboard.py's pure data
functions rather than reimplementing them: assert parity on the same
fixture file."""

import json
import os
from pathlib import Path

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

from admin_app import create_app
from dashboard import activity_records, load_track
from config import TRADING_PROFILES, demo_csv_path


ROW = {
    "Session_id": "s1", "Timestamp": "2026-08-01 12:00:00", "Round": 1,
    "Quote Status": "quote_generated", "Best Bid": 99.0, "Best Ask": 100.0,
    "Bid Price Sent": 98.0, "Ask Price Sent": 101.0,
    "Buy Order ID": "b1", "Buy Status": "open", "Sell Order ID": "s1o",
    "Sell Status": "open",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("control", "state", "results", "backups"):
        (tmp_path / name).mkdir()
    csv_name = os.path.basename(demo_csv_path(TRADING_PROFILES["btc"]["output_csv"]))
    demo_path = str(tmp_path / "results" / csv_name)
    pd.DataFrame([ROW]).to_csv(demo_path, index=False)

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
    application.config["_demo_csv_path"] = demo_path
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client):
    import re
    login_page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True)).group(1)
    client.post("/admin/login", data={"username": "admin", "password": "correct horse", "csrf_token": token})


def _normalize_nan(value):
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, dict):
        return {k: _normalize_nan(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_nan(v) for v in value]
    return value


def test_orders_route_matches_dashboard_activity_records(client, app):
    _login(client)
    response = client.get("/admin/orders?pair=btc")
    assert response.status_code == 200
    df = load_track(app.config["_demo_csv_path"])
    expected = _normalize_nan(json.loads(json.dumps(activity_records(df), default=str)))
    got = _normalize_nan(response.get_json()["orders"])
    # Timestamp round-trips through two different serializers (Flask's
    # jsonify vs this test's json.dumps(default=str)) with different but
    # equivalent string formats -- compare parsed values, not formatting.
    for row in (expected, got):
        for record in row:
            stamp = pd.Timestamp(record["Timestamp"])
            if stamp.tzinfo is not None:
                stamp = stamp.tz_localize(None)
            record["Timestamp"] = stamp.isoformat()
    assert got == expected


def test_balances_route_reads_ledger_account(client, app):
    ledger_path = Path(app.config["STATE_DIR"]) / "paper_s1_btcirt.json"
    ledger_path.write_text(json.dumps({
        "account": {"inventory": 0.1, "cash": 500.0, "valuation_price": 100.0, "fees_quote": 1.5},
    }))
    _login(client)
    response = client.get("/admin/balances")
    assert response.status_code == 200
    assert response.get_json()["btc"]["cash"] == 500.0


def test_audit_route_returns_recent_entries(client, app):
    from control_queue import append_audit
    append_audit(app.config["CONTROL_DIR"], {"action": "start_worker", "ok": True})
    _login(client)
    response = client.get("/admin/audit")
    assert response.status_code == 200
    entries = response.get_json()["audit"]
    assert entries[-1]["action"] == "start_worker"


def test_readonly_routes_require_login(client):
    for path in ("/admin/orders", "/admin/balances", "/admin/logs", "/admin/audit"):
        response = client.get(path)
        assert response.status_code == 302
