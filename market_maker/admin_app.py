"""Admin management page for the Demo (paper-trading) stack.

Plain Flask, server-rendered Jinja, no Dash. Lowest-privilege component of
the admin surface (see docs/superpowers/specs/2026-08-01-admin-demo-control-design.md):
it can read Demo state (ro mounts in production) and enqueue a fixed set of
named actions via `control_queue.py` -- it never shells out, never touches
a Docker socket, and never writes directly to runtime/state or
runtime/results. All mutation is performed by `deploy/control_agent.py`.

No Real Mode surface anywhere in this file, by construction: every route
here only ever reads/writes Demo-prefixed files and enqueues Demo-only
control-queue actions (see control_queue.ACTIONS).
"""

from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import Flask, redirect, request, session as flask_session, url_for
from werkzeug.security import check_password_hash

from control_queue import ControlLock, OperationInProgress, await_result, submit_command
import control_queue

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)


def create_app(config: Optional[dict] = None) -> Flask:
    app = Flask(__name__)
    cfg = {
        "ADMIN_USERNAME": os.environ.get("ADMIN_USERNAME", ""),
        "ADMIN_PASSWORD_HASH": os.environ.get("ADMIN_PASSWORD_HASH", ""),
        "SECRET_KEY": os.environ.get("ADMIN_SESSION_SECRET", secrets.token_hex(32)),
        "CONTROL_DIR": os.environ.get("LOB_CONTROL_DIR", "runtime/control"),
        "STATE_DIR": os.environ.get("LOB_STATE_DIR", "runtime/state"),
        "RESULTS_DIR": os.environ.get("LOB_RESULTS_DIR", "runtime/results"),
        "BACKUPS_DIR": os.environ.get("LOB_BACKUPS_DIR", "runtime/backups"),
        "COMMAND_TIMEOUT": 30.0,
    }
    if config:
        cfg.update(config)
    app.config.update(cfg)
    app.secret_key = cfg["SECRET_KEY"]
    app.permanent_session_lifetime = SESSION_IDLE_TIMEOUT

    _register_auth(app)
    _register_routes(app)
    return app


# --- auth + CSRF ------------------------------------------------------

def _is_authenticated() -> bool:
    if not flask_session.get("user"):
        return False
    last_seen = flask_session.get("last_seen")
    if not last_seen:
        return False
    try:
        seen_at = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    if datetime.now(timezone.utc) - seen_at > SESSION_IDLE_TIMEOUT:
        flask_session.clear()
        return False
    flask_session["last_seen"] = datetime.now(timezone.utc).isoformat()
    return True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_authenticated():
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def get_csrf_token() -> str:
    token = flask_session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        flask_session["csrf_token"] = token
    return token


def _valid_csrf(submitted: Optional[str]) -> bool:
    expected = flask_session.get("csrf_token")
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)


def csrf_protected(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _valid_csrf(request.form.get("csrf_token")):
            return ("CSRF token missing or invalid", 400)
        return view(*args, **kwargs)
    return wrapped


def _register_auth(app: Flask) -> None:
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            expected_user = app.config["ADMIN_USERNAME"]
            expected_hash = app.config["ADMIN_PASSWORD_HASH"]
            valid = bool(expected_user) and hmac.compare_digest(username, expected_user)
            valid = valid and bool(expected_hash) and check_password_hash(expected_hash, password)
            if valid:
                flask_session.clear()
                flask_session.permanent = True
                flask_session["user"] = username
                flask_session["last_seen"] = datetime.now(timezone.utc).isoformat()
                get_csrf_token()
                return redirect(url_for("admin_index"))
            error = "invalid credentials"
        token = get_csrf_token()
        return _render_login(error, token)

    @app.route("/admin/logout", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_logout():
        flask_session.clear()
        return redirect(url_for("admin_login"))


def _render_login(error, token) -> str:
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""
<!doctype html><title>Demo Admin Login</title>
{error_html}
<form method="post">
  <input type="hidden" name="csrf_token" value="{token}">
  <label>Username <input name="username"></label>
  <label>Password <input name="password" type="password"></label>
  <button type="submit">Log in</button>
</form>
"""


# --- control-queue helper used by every mutating route ----------------

def enqueue_and_wait(app: Flask, action: str, pair=None, payload=None):
    """Acquire the control lock, submit a command, wait for its result,
    write one audit entry, release the lock. Raises OperationInProgress
    if another management operation is already running."""
    control_dir = app.config["CONTROL_DIR"]
    requested_by = flask_session.get("user", "unknown")
    with ControlLock(control_dir):
        command_id = submit_command(
            control_dir, action, pair, requested_by=requested_by, payload=payload,
        )
        result = await_result(control_dir, command_id, timeout=app.config["COMMAND_TIMEOUT"])
        control_queue.append_audit(control_dir, {
            "command_id": command_id, "action": action, "pair": pair,
            "requested_by": requested_by, "ok": result.get("ok"),
        })
        return result


def _register_routes(app: Flask) -> None:
    @app.route("/admin/")
    @login_required
    def admin_index():
        return "<!doctype html><title>Demo Admin</title><p>Demo admin control page.</p>"
