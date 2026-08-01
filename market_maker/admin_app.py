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

import fcntl
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, redirect, request, session as flask_session, url_for
from werkzeug.security import check_password_hash

from control_queue import ControlLock, OperationInProgress, await_result, submit_command
import control_queue
from demo_config import (
    FIELD_SPECS,
    OverlayValidationError,
    has_previous,
    load_overrides,
    overlay_path,
    validate_overrides,
)

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
PAIRS = ("btc", "usdt")
PAIR_LOCK_SUFFIX = {"btc": "btcirt", "usdt": "usdtirt"}


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
    return enqueue_batch(app, action, [pair], payload=payload)[0]


def enqueue_batch(app: Flask, action: str, pairs, payload=None):
    """Same as enqueue_and_wait but for one or more pairs under a single
    held lock (used for the "both" pair selector) -- one audit entry per
    completed sub-action. Raises OperationInProgress if the lock is held
    by a concurrent request; nothing is enqueued in that case."""
    control_dir = app.config["CONTROL_DIR"]
    requested_by = flask_session.get("user", "unknown")
    results = []
    with ControlLock(control_dir):
        for pair in pairs:
            command_id = submit_command(
                control_dir, action, pair, requested_by=requested_by, payload=payload,
            )
            result = await_result(control_dir, command_id, timeout=app.config["COMMAND_TIMEOUT"])
            control_queue.append_audit(control_dir, {
                "command_id": command_id, "action": action, "pair": pair,
                "requested_by": requested_by, "ok": result.get("ok"),
            })
            results.append(dict(result, pair=pair))
    return results


def _resolve_pairs(selector: Optional[str]):
    if selector == "both":
        return list(PAIRS)
    if selector in PAIRS:
        return [selector]
    return None


def _worker_running(app: Flask, pair: str) -> bool:
    lock_path = Path(app.config["STATE_DIR"]) / f".worker_{PAIR_LOCK_SUFFIX[pair]}.lock"
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("r") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _control_lock_held(app: Flask) -> bool:
    lock_path = Path(app.config["CONTROL_DIR"]) / "control.lock"
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _session_id(app: Flask) -> Optional[str]:
    path = Path(app.config["STATE_DIR"]) / "stack_session.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("session_id")
    except (OSError, ValueError):
        return None


def _recent_audit(app: Flask, limit: int = 20):
    path = Path(app.config["CONTROL_DIR"]) / "audit.log"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _register_routes(app: Flask) -> None:
    @app.route("/admin/")
    @login_required
    def admin_index():
        status = {
            "session_id": _session_id(app),
            "control_lock_held": _control_lock_held(app),
            "workers": {pair: _worker_running(app, pair) for pair in PAIRS},
            "recent_audit": _recent_audit(app),
        }
        return jsonify(status)

    @app.route("/admin/config/<pair>", methods=["GET", "POST"])
    @login_required
    def admin_config(pair):
        if pair not in PAIRS:
            return ("unknown pair", 404)
        path = overlay_path(app.config["STATE_DIR"], pair)
        if request.method == "GET":
            return jsonify({
                "pair": pair,
                "overrides": load_overrides(path),
                "has_previous": has_previous(path),
                "fields": sorted(FIELD_SPECS),
                "csrf_token": get_csrf_token(),
            })
        return _admin_config_post(app, pair, path)

    @app.route("/admin/workers/<action>", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_workers(action):
        if action not in ("start", "stop", "restart"):
            return ("unknown worker action", 400)
        pairs = _resolve_pairs(request.form.get("pair"))
        if pairs is None:
            return ("invalid pair (expected btc, usdt, or both)", 400)
        try:
            results = enqueue_batch(app, f"{action}_worker", pairs)
        except OperationInProgress as exc:
            return (str(exc), 409)
        return jsonify({"results": results})

    @app.route("/admin/session/new", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_new_session():
        try:
            result = enqueue_and_wait(app, "new_session")
        except OperationInProgress as exc:
            return (str(exc), 409)
        return jsonify(result)

    @app.route("/admin/orders/cancel", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_cancel_orders():
        pairs = _resolve_pairs(request.form.get("pair"))
        if pairs is None:
            return ("invalid pair (expected btc, usdt, or both)", 400)
        try:
            results = enqueue_batch(app, "cancel_orders", pairs)
        except OperationInProgress as exc:
            return (str(exc), 409)
        return jsonify({"results": results})


@csrf_protected
def _admin_config_post(app: Flask, pair: str, path):
    if request.form.get("action") == "revert":
        try:
            result = enqueue_and_wait(app, "rollback_config", pair)
        except OperationInProgress as exc:
            return (str(exc), 409)
        return jsonify(result)

    raw = {k: v for k, v in request.form.items() if k in FIELD_SPECS}
    overrides = {}
    for key, value in raw.items():
        expected_type = FIELD_SPECS[key][0]
        if expected_type is bool:
            overrides[key] = value in ("true", "on", "1", "True")
            continue
        try:
            overrides[key] = float(value)
        except ValueError:
            return (f"invalid value for {key}", 400)
    try:
        validated = validate_overrides(overrides)
    except OverlayValidationError as exc:
        return (str(exc), 400)  # rejected before enqueueing -- nothing submitted

    try:
        result = enqueue_and_wait(app, "apply_config", pair, payload={"overrides": validated})
    except OperationInProgress as exc:
        return (str(exc), 409)
    return jsonify(result)
