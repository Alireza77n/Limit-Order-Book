import re

import pytest
from werkzeug.security import generate_password_hash

from admin_app import create_app


@pytest.fixture
def app(tmp_path):
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


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, html
    return match.group(1)


def test_unauthenticated_index_redirects_to_login(client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_wrong_password_rejected(client):
    login_page = client.get("/admin/login")
    token = _csrf_token(login_page.get_data(as_text=True))
    response = client.post("/admin/login", data={
        "username": "admin", "password": "wrong", "csrf_token": token,
    })
    assert response.status_code == 200
    assert "invalid credentials" in response.get_data(as_text=True)


def test_valid_login_grants_access(client):
    login_page = client.get("/admin/login")
    token = _csrf_token(login_page.get_data(as_text=True))
    response = client.post("/admin/login", data={
        "username": "admin", "password": "correct horse", "csrf_token": token,
    })
    assert response.status_code == 302
    index = client.get("/admin/")
    assert index.status_code == 200


def test_logout_without_csrf_rejected(client):
    login_page = client.get("/admin/login")
    token = _csrf_token(login_page.get_data(as_text=True))
    client.post("/admin/login", data={
        "username": "admin", "password": "correct horse", "csrf_token": token,
    })
    response = client.post("/admin/logout", data={})
    assert response.status_code == 400


def test_logout_with_valid_csrf_succeeds_and_clears_session(client):
    login_page = client.get("/admin/login")
    token = _csrf_token(login_page.get_data(as_text=True))
    client.post("/admin/login", data={
        "username": "admin", "password": "correct horse", "csrf_token": token,
    })
    with client.session_transaction() as sess:
        post_login_token = sess["csrf_token"]
    response = client.post("/admin/logout", data={"csrf_token": post_login_token})
    assert response.status_code == 302
    index = client.get("/admin/")
    assert index.status_code == 302  # logged out again


def test_session_idle_timeout_forces_relogin(client, app, monkeypatch):
    from datetime import datetime, timedelta, timezone
    login_page = client.get("/admin/login")
    token = _csrf_token(login_page.get_data(as_text=True))
    client.post("/admin/login", data={
        "username": "admin", "password": "correct horse", "csrf_token": token,
    })
    with client.session_transaction() as sess:
        sess["last_seen"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    index = client.get("/admin/")
    assert index.status_code == 302
