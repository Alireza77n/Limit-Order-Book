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
import io
import json
import os
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session as flask_session, url_for
from werkzeug.security import check_password_hash

from control_queue import ControlLock, OperationInProgress, await_result, submit_command
import control_queue
from demo_config import (
    FIELD_SPECS,
    OverlayValidationError,
    apply_overrides,
    has_previous,
    load_overrides,
    overlay_path,
    validate_overrides,
)
from config import MMConfig, TRADING_PROFILES, demo_csv_path
from dashboard import activity_records, load_track

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
PAIRS = ("btc", "usdt")
PAIR_LOCK_SUFFIX = {"btc": "btcirt", "usdt": "usdtirt"}

# One-line help text shown next to each editable Demo config field --
# keys must exactly match demo_config.FIELD_SPECS.
FIELD_HELP = {
    "order_size": "Base-asset amount per order round.",
    "inv_max": "Maximum inventory (base asset) the worker will hold.",
    "inv_min": "Minimum inventory (base asset) the worker will hold.",
    "demo_inv_initial": "Starting inventory when a new demo session begins.",
    "cash_initial": "Starting cash (quote currency) when a new demo session begins.",
    "commission_perc": "Simulated trading fee, as a fraction of notional (0-1).",
    "cancel_after_minutes": "Cancel resting paper orders older than this many minutes.",
    "depth_max": "Maximum order-book depth level the strategy will quote into.",
    "time_step": "Seconds between quoting rounds.",
    "max_rounds": "Stop the worker after this many rounds.",
    "demo_level1_fast_fill_enabled": "Simulate near-instant fills at the top of book.",
    "demo_level1_fill_delay_seconds": "Delay (seconds) before a level-1 fast fill is applied.",
    "demo_level1_fill_fraction": "Fraction of the order filled per level-1 fast-fill tick.",
    "demo_queue_enabled": "Simulate queue position instead of instant fills.",
    "no_quote_alert_rounds": "Consecutive no-quote rounds before an alert fires.",
    "no_quote_alert_seconds": "Elapsed no-quote seconds before an alert fires.",
    "no_quote_alert_cooldown_seconds": "Minimum seconds between repeated no-quote alerts.",
    "no_quote_recovery_alert": "Send an alert when quoting recovers after a no-quote episode.",
    "algo1_diagnostics_enabled": "Log extra Algorithm 1 decision diagnostics.",
}


def create_app(config: Optional[dict] = None) -> Flask:
    app = Flask(__name__, static_url_path="/admin/static")
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
    _register_error_handlers(app)
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
            if _wants_html():
                return _error_page(400, "CSRF token missing or invalid. Go back and try again.")
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
        return render_template("admin/login.html", error=error, csrf_token=token)

    @app.route("/admin/logout", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_logout():
        flask_session.clear()
        return redirect(url_for("admin_login"))


def _wants_html() -> bool:
    """Check if the request prefers HTML over JSON (browser requests).
    Flask's test client sends no Accept header at all by default, which
    makes best_match return None here -- so every pre-existing JSON-only
    caller (this project's own tests, and any external automation) is
    completely unaffected. Real browsers list text/html explicitly and get
    HTML; `Accept: application/json` (or `?format=json`) always forces JSON.
    """
    if request.args.get("format") == "json":
        return False
    if request.args.get("format") == "html":
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "text/html"


def _error_page(code: int, message: str):
    return render_template("admin/error.html", code=code, message=message, csrf_token=get_csrf_token()), code


# --- control-queue helper used by every mutating route ----------------

def enqueue_and_wait(app: Flask, action: str, pair=None, payload=None):
    """Acquire the control lock, submit a command, wait for its result,
    write one audit entry, release the lock. Raises OperationInProgress
    if another management operation is already running."""
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


def _ledger_account(app: Flask, pair: str):
    """The CURRENT active session's paper ledger `account` block (inventory,
    cash, valuation_price, fees_quote) for `pair` -- the same field names
    paper.py's own save/load round-trip uses.

    Deliberately keyed off the live session_id (stack_session.json), not
    "whichever ledger file was modified most recently": once a worker is
    stopped, its last-written ledger stays the newest file on disk even
    after a new session starts, so mtime-sorting silently showed a dead
    session's numbers as if they were current. Scoping to the exact
    session file (or nothing, if that pair's worker hasn't written one for
    this session yet) keeps this page honest about what it's showing."""
    session_id = _session_id(app)
    if not session_id:
        return None
    safe_session = str(session_id).replace("/", "_")
    path = Path(app.config["STATE_DIR"]) / f"paper_{safe_session}_{PAIR_LOCK_SUFFIX[pair]}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("account")
    except (OSError, ValueError):
        return None


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and value != value:  # NaN
        return False
    return True


def _serialize_orders(records):
    """activity_records() rows carry raw pd.Timestamp/NaN values -- fine for
    jsonify (it special-cases datetime subclasses) but orders.html expects
    plain strings to slice/format. Converting once here keeps both the JSON
    and HTML branches consistent (and jsonify-safe either way)."""
    serialized = []
    for record in records:
        row = dict(record)
        timestamp = row.get("Timestamp")
        if hasattr(timestamp, "isoformat"):
            row["Timestamp"] = timestamp.isoformat()
        elif isinstance(timestamp, float) and timestamp != timestamp:  # NaN
            row["Timestamp"] = None
        serialized.append(row)
    return serialized


def _filter_orders(records, side=None, status=None):
    """Narrow activity_records() rows by side (buy/sell) and/or status
    (open/filled/cancelled/not_sent) -- both admin_orders' JSON and HTML
    responses share this so filtering behaves identically either way."""
    def keep(record):
        buy_status, sell_status = record.get("Buy Status"), record.get("Sell Status")
        if side == "buy" and not _has_value(buy_status):
            return False
        if side == "sell" and not _has_value(sell_status):
            return False
        if status:
            present = {s for s in (buy_status, sell_status) if _has_value(s)}
            if status not in present:
                return False
        return True
    return [record for record in records if keep(record)]


def _fee_scopes_summary(app: Flask):
    """CURRENT session's fee totals per pair from paper_fee_scopes.json
    (shared by both pairs' PaperLedger instances, see paper.py's
    FeeScopes) -- read-only, no PaperLedger instantiated here, matching
    _ledger_account's pattern.

    Only the active session's rows -- not every session ever recorded.
    Mixing old sessions into this table read as "combined/aggregate fees"
    with no way to tell which slice was current, which is exactly the
    confusion _ledger_account's mtime-sort bug caused on the balances
    above it; scoping both the same way keeps the whole page internally
    consistent."""
    path = Path(app.config["STATE_DIR"]) / "paper_fee_scopes.json"
    if not path.exists():
        return []
    session_id = _session_id(app)
    if not session_id:
        return []
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    per_pair = (document.get("sessions") or {}).get(str(session_id), {})
    return [
        {"session_id": session_id, "pair": pair, "fees_quote": float(fees_quote)}
        for pair, fees_quote in (per_pair or {}).items()
    ]


RESET_GLOBS = ("*_demo.csv", "*_demo.sqlite3", "paper_*.json", "paper_*_archive.jsonl", "paper_fee_scopes.json")


def _reset_preview(app: Flask):
    files = set()
    for directory in (Path(app.config["STATE_DIR"]), Path(app.config["RESULTS_DIR"])):
        if not directory.exists():
            continue
        for pattern in RESET_GLOBS:
            files.update(p.name for p in directory.glob(pattern) if p.is_file())
    return sorted(files)


def _list_backups(app: Flask):
    backups_dir = Path(app.config["BACKUPS_DIR"])
    if not backups_dir.exists():
        return []
    listing = []
    for entry in sorted(backups_dir.iterdir()):
        if entry.is_dir():
            listing.append({
                "name": entry.name,
                "files": sorted(p.name for p in entry.iterdir() if p.is_file()),
            })
    return listing


def _backup_preview(app: Flask, name: str):
    backup_dir = Path(app.config["BACKUPS_DIR"]) / name
    if not backup_dir.is_dir():
        return None
    return sorted(p.name for p in backup_dir.iterdir() if p.is_file())


def _recover_diagnostics(app: Flask):
    """Read-only mirror of control_agent.handle_recover_demo's diagnostic
    checks -- reports the same stale-lock/mismatch/corruption findings
    without clearing anything (that happens only via the mutating
    /admin/recover/apply route, which enqueues recover_demo)."""
    state_dir = Path(app.config["STATE_DIR"])
    stale_locks = [
        pair for pair in PAIRS
        if (state_dir / f".worker_{PAIR_LOCK_SUFFIX[pair]}.lock").exists() and not _worker_running(app, pair)
    ]
    session_id = _session_id(app)
    session_mismatches = []
    corrupt_files = []
    for ledger_path in sorted(state_dir.glob("paper_*.json")):
        try:
            document = json.loads(ledger_path.read_text())
        except (OSError, ValueError):
            corrupt_files.append(ledger_path.name)
            continue
        ledger_session = document.get("session_id")
        if session_id and ledger_session and ledger_session != session_id:
            session_mismatches.append(ledger_path.name)
    return {
        "stale_locks": stale_locks,
        "session_mismatches": session_mismatches,
        "corrupt_files": corrupt_files,
        "confirmation_required_for_reset": bool(session_mismatches or corrupt_files),
    }


def _effective_config_values(pair: str, path) -> dict:
    """Current effective value of every editable field for `pair`: the
    profile's own defaults (config.TRADING_PROFILES / MMConfig), with the
    Demo admin overlay layered on top if one exists -- exactly what a real
    demo worker would run with (see run.py:make_cfg). Unlike
    demo_config.load_overrides(), this never comes back sparse/blank for
    fields nobody has overridden yet."""
    cfg = MMConfig(session=0, mode="demo", **TRADING_PROFILES[pair])
    overrides = load_overrides(path)
    if overrides:
        apply_overrides(cfg, overrides)
    return {key: getattr(cfg, key) for key in FIELD_SPECS}


def _config_page_context(app: Flask, pair: str, path, *, error=None, success=None, attempted=None) -> dict:
    values = _effective_config_values(pair, path)
    if attempted:
        values = dict(values, **attempted)
    return {
        "pair": pair,
        "overrides": values,
        "has_previous": has_previous(path),
        "field_order": sorted(FIELD_SPECS),
        "FIELD_SPECS": FIELD_SPECS,
        "field_help": FIELD_HELP,
        "csrf_token": get_csrf_token(),
        "session_id": _session_id(app),
        "error": error,
        "success": success,
    }


def _workers_html_response(app: Flask, result):
    workers = {pair: _worker_running(app, pair) for pair in PAIRS}
    return render_template("admin/workers.html", workers=workers,
                            csrf_token=get_csrf_token(), session_id=_session_id(app), result=result)


def _backups_html_response(app: Flask):
    return render_template("admin/backups.html", backups=_list_backups(app),
                            csrf_token=get_csrf_token(), session_id=_session_id(app))


def _recovery_html_response(app: Flask):
    return render_template(
        "admin/recovery.html", diagnostics=_recover_diagnostics(app),
        session_id=_session_id(app),
        reset_files=_reset_preview(app), csrf_token=get_csrf_token(),
    )


def _register_routes(app: Flask) -> None:
    @app.route("/admin/health")
    def admin_health():
        # Unauthenticated on purpose -- container healthcheck endpoint,
        # returns nothing but a 200 and touches no Demo state.
        return ("ok", 200)

    @app.route("/admin/")
    @login_required
    def admin_index():
        status = {
            "session_id": _session_id(app),
            "control_lock_held": _control_lock_held(app),
            "workers": {pair: _worker_running(app, pair) for pair in PAIRS},
            "recent_audit": _recent_audit(app),
        }
        if _wants_html():
            return render_template("admin/overview.html", csrf_token=get_csrf_token(), **status)
        return jsonify(status)

    @app.route("/admin/config/<pair>", methods=["GET", "POST"])
    @login_required
    def admin_config(pair):
        if pair not in PAIRS:
            if _wants_html():
                return _error_page(404, "Unknown pair.")
            return ("unknown pair", 404)
        path = overlay_path(app.config["STATE_DIR"], pair)
        if request.method == "GET":
            if _wants_html():
                return render_template(
                    "admin/config.html", **_config_page_context(app, pair, path),
                )
            return jsonify({
                "pair": pair,
                "overrides": load_overrides(path),
                "has_previous": has_previous(path),
                "fields": sorted(FIELD_SPECS),
                "csrf_token": get_csrf_token(),
            })
        return _admin_config_post(app, pair, path)

    @app.route("/admin/workers")
    @login_required
    def admin_workers_page():
        workers = {pair: _worker_running(app, pair) for pair in PAIRS}
        if _wants_html():
            return render_template("admin/workers.html", workers=workers,
                                    csrf_token=get_csrf_token(), session_id=_session_id(app), result=None)
        return jsonify({"workers": workers})

    @app.route("/admin/workers/<action>", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_workers(action):
        if action not in ("start", "stop", "restart"):
            if _wants_html():
                return _error_page(400, "Unknown worker action.")
            return ("unknown worker action", 400)
        pairs = _resolve_pairs(request.form.get("pair"))
        if pairs is None:
            if _wants_html():
                return _error_page(400, "Invalid pair (expected btc, usdt, or both).")
            return ("invalid pair (expected btc, usdt, or both)", 400)
        try:
            results = enqueue_batch(app, f"{action}_worker", pairs)
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _workers_html_response(app, None), 409
            return (str(exc), 409)
        if _wants_html():
            flash(f"{action.capitalize()} requested for {', '.join(pairs)}.", "success")
            return _workers_html_response(app, results)
        return jsonify({"results": results})

    @app.route("/admin/session/new", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_new_session():
        try:
            result = enqueue_and_wait(app, "new_session")
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _workers_html_response(app, None), 409
            return (str(exc), 409)
        if _wants_html():
            flash("New demo session created.", "success")
            return _workers_html_response(app, result)
        return jsonify(result)

    @app.route("/admin/orders/cancel", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_cancel_orders():
        pairs = _resolve_pairs(request.form.get("pair"))
        if pairs is None:
            if _wants_html():
                return _error_page(400, "Invalid pair (expected btc, usdt, or both).")
            return ("invalid pair (expected btc, usdt, or both)", 400)
        try:
            results = enqueue_batch(app, "cancel_orders", pairs)
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _workers_html_response(app, None), 409
            return (str(exc), 409)
        if _wants_html():
            flash(f"Cancel requested for {', '.join(pairs)}.", "success")
            return _workers_html_response(app, results)
        return jsonify({"results": results})

    @app.route("/admin/backups", methods=["GET"])
    @login_required
    def admin_backups():
        if _wants_html():
            return render_template("admin/backups.html", backups=_list_backups(app),
                                    csrf_token=get_csrf_token(), session_id=_session_id(app))
        return jsonify({"backups": _list_backups(app)})

    @app.route("/admin/backups/create", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_backups_create():
        try:
            result = enqueue_and_wait(app, "create_backup", None, payload={"prefix": "manual"})
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _backups_html_response(app), 409
            return (str(exc), 409)
        if _wants_html():
            flash("Backup created.", "success")
            return _backups_html_response(app)
        return jsonify(result)

    @app.route("/admin/backups/<name>/restore", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_backups_restore(name):
        if request.form.get("confirm") != "RESTORE":
            if _wants_html():
                flash("Confirmation required: type RESTORE exactly to confirm.", "error")
                return _backups_html_response(app), 400
            return jsonify({
                "error": "confirmation required (send confirm=RESTORE)",
                "backup_files": _backup_preview(app, name),
            }), 400
        try:
            result = enqueue_and_wait(
                app, "restore_backup", None,
                payload={"backup_name": name, "confirm": "RESTORE"},
            )
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _backups_html_response(app), 409
            return (str(exc), 409)
        if _wants_html():
            flash(f"Backup '{name}' restored.", "success")
            return _backups_html_response(app)
        return jsonify(result)

    @app.route("/admin/backups/<name>/download", methods=["GET"])
    @login_required
    def admin_backups_download(name):
        """Download the exact backup listed in a row -- unlike /admin/export
        (which always creates a brand-new snapshot), this never enqueues
        anything, it only zips files already on disk for `name`."""
        backup_dir = Path(app.config["BACKUPS_DIR"]) / name
        if not backup_dir.is_dir():
            return ("backup not found", 404)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for file_path in sorted(backup_dir.iterdir()):
                if file_path.is_file():
                    archive.write(file_path, arcname=file_path.name)
        buffer.seek(0)
        return send_file(
            buffer, mimetype="application/zip", as_attachment=True,
            download_name=f"{name}.zip",
        )

    @app.route("/admin/reset", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_reset():
        if request.form.get("confirm") != "RESET":
            if _wants_html():
                flash("Confirmation required: type RESET exactly to confirm.", "error")
                return _recovery_html_response(app), 400
            return jsonify({
                "error": "confirmation required (send confirm=RESET)",
                "affected_files": _reset_preview(app),
            }), 400
        try:
            result = enqueue_and_wait(app, "reset_demo", None, payload={"confirm": "RESET"})
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _recovery_html_response(app), 409
            return (str(exc), 409)
        if _wants_html():
            flash("Demo reset complete. A new session was created.", "success")
            return _recovery_html_response(app)
        return jsonify(result)

    @app.route("/admin/recover", methods=["GET"])
    @login_required
    def admin_recover():
        diagnostics = _recover_diagnostics(app)
        if _wants_html():
            return render_template(
                "admin/recovery.html", diagnostics=diagnostics,
                reset_files=_reset_preview(app), csrf_token=get_csrf_token(),
                session_id=_session_id(app),
            )
        return jsonify(dict(diagnostics, reset_files=_reset_preview(app)))

    @app.route("/admin/recover/apply", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_recover_apply():
        # Only ever enqueues recover_demo (safe auto-fixes: stale locks
        # only). It never enqueues reset_demo -- the destructive path is
        # exclusively /admin/reset, gated by its own typed confirm field.
        try:
            result = enqueue_and_wait(app, "recover_demo", None)
        except OperationInProgress as exc:
            if _wants_html():
                flash(str(exc), "warning")
                return _recovery_html_response(app), 409
            return (str(exc), 409)
        if _wants_html():
            flash("Safe recovery applied.", "success")
            return _recovery_html_response(app)
        return jsonify(result)

    @app.route("/admin/export", methods=["GET"])
    @login_required
    def admin_export():
        try:
            result = enqueue_and_wait(app, "create_backup", None, payload={"prefix": "export"})
        except OperationInProgress as exc:
            if _wants_html():
                return _error_page(409, str(exc))
            return (str(exc), 409)
        backup_name = (result.get("detail") or {}).get("backup")
        if not result.get("ok") or not backup_name:
            if _wants_html():
                return _error_page(500, "Export snapshot failed.")
            return ("export snapshot failed", 500)
        backup_dir = Path(app.config["BACKUPS_DIR"]) / backup_name
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for file_path in sorted(backup_dir.iterdir()):
                if file_path.is_file():
                    archive.write(file_path, arcname=file_path.name)
        buffer.seek(0)
        return send_file(
            buffer, mimetype="application/zip", as_attachment=True,
            download_name=f"{backup_name}.zip",
        )

    @app.route("/admin/orders")
    @login_required
    def admin_orders():
        pair = request.args.get("pair", "btc")
        if pair not in PAIRS:
            if _wants_html():
                return _error_page(400, "Unknown pair.")
            return ("unknown pair", 400)
        side = request.args.get("side") or None
        status = request.args.get("status") or None
        # Join against this app's own RESULTS_DIR (not the global profile
        # path) so a ro-mounted runtime/results in production, or a tmp
        # dir in tests, is what actually gets read.
        csv_name = os.path.basename(demo_csv_path(TRADING_PROFILES[pair]["output_csv"]))
        csv_path = os.path.join(app.config["RESULTS_DIR"], csv_name)
        df = load_track(csv_path)  # dashboard.py's own pure loader -- not re-implemented here
        records = activity_records(df) if df is not None else []
        orders = _serialize_orders(_filter_orders(records, side=side, status=status))
        if _wants_html():
            return render_template(
                "admin/orders.html", orders=orders, selected_pair=pair,
                side_filter=side, status_filter=status, csrf_token=get_csrf_token(),
                session_id=_session_id(app),
            )
        return jsonify({"orders": orders})

    @app.route("/admin/balances")
    @login_required
    def admin_balances():
        balances = {pair: _ledger_account(app, pair) for pair in PAIRS}
        if _wants_html():
            return render_template(
                "admin/balances.html", balances=balances,
                fee_scopes=_fee_scopes_summary(app),
                starting_cash_btc=TRADING_PROFILES["btc"]["cash_initial"],
                starting_cash_usdt=TRADING_PROFILES["usdt"]["cash_initial"],
                csrf_token=get_csrf_token(), session_id=_session_id(app),
            )
        return jsonify(balances)

    @app.route("/admin/logs")
    @login_required
    def admin_logs():
        pair = request.args.get("pair", "btc")
        if pair not in PAIRS:
            if _wants_html():
                return _error_page(400, "Unknown pair.")
            return ("unknown pair", 400)
        state_dir = Path(app.config["STATE_DIR"])
        matches = sorted(
            state_dir.glob(f"paper_*_{PAIR_LOCK_SUFFIX[pair]}_archive.jsonl"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        entries = []
        if matches:
            for line in matches[0].read_text().splitlines()[-200:]:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        if _wants_html():
            return render_template("admin/logs.html", entries=entries, selected_pair=pair,
                                    csrf_token=get_csrf_token(), session_id=_session_id(app))
        return jsonify({"entries": entries})

    @app.route("/admin/audit")
    @login_required
    def admin_audit():
        limit = request.args.get("limit", "200")
        limit = int(limit) if limit.isdigit() else 200
        audit = _recent_audit(app, limit=limit)
        if _wants_html():
            return render_template("admin/audit.html", audit=audit, limit=limit,
                                    csrf_token=get_csrf_token(), session_id=_session_id(app))
        return jsonify({"audit": audit})


@csrf_protected
def _admin_config_post(app: Flask, pair: str, path):
    wants_html = _wants_html()

    if request.form.get("action") == "revert":
        try:
            result = enqueue_and_wait(app, "rollback_config", pair)
        except OperationInProgress as exc:
            if wants_html:
                return render_template(
                    "admin/config.html", **_config_page_context(app, pair, path, error=str(exc)),
                ), 409
            return (str(exc), 409)
        if wants_html:
            return render_template(
                "admin/config.html",
                **_config_page_context(app, pair, path, success="Reverted to previous configuration."),
            )
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
            message = f"invalid value for {key}"
            if wants_html:
                return render_template(
                    "admin/config.html", **_config_page_context(app, pair, path, error=message),
                ), 400
            return (message, 400)
    try:
        validated = validate_overrides(overrides)
    except OverlayValidationError as exc:
        # Rejected before enqueueing -- nothing submitted. Re-render with the
        # attempted (unsaved) values so the admin doesn't lose their edits.
        if wants_html:
            return render_template(
                "admin/config.html",
                **_config_page_context(app, pair, path, error=str(exc), attempted=overrides),
            ), 400
        return (str(exc), 400)

    try:
        result = enqueue_and_wait(app, "apply_config", pair, payload={"overrides": validated})
    except OperationInProgress as exc:
        if wants_html:
            return render_template(
                "admin/config.html", **_config_page_context(app, pair, path, error=str(exc)),
            ), 409
        return (str(exc), 409)
    if wants_html:
        return render_template(
            "admin/config.html",
            **_config_page_context(app, pair, path, success="Configuration applied."),
        )
    return jsonify(result)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def _not_found(_exc):
        if _wants_html():
            return _error_page(404, "That admin page doesn't exist.")
        return ("not found", 404)

    @app.errorhandler(500)
    def _server_error(_exc):
        if _wants_html():
            return _error_page(500, "Something went wrong on the server.")
        return ("internal error", 500)


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8051)
