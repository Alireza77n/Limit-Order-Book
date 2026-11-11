"""Admin management page for the Demo (paper-trading) stack.

Plain Flask, server-rendered Jinja, no Dash. Lowest-privilege component of
the admin surface:
it can read Demo state (ro mounts in production) and enqueue a fixed set of
named actions via `control_queue.py` -- it never shells out, never touches
a Docker socket, and never writes directly to runtime/state or
runtime/results. All mutation is performed by `deploy/control_agent.py`.

No Real Mode surface anywhere in this file, by construction: every route
here only ever reads/writes Demo-prefixed files and enqueues Demo-only
control-queue actions (see control_queue.ACTIONS).
"""

from __future__ import annotations

import csv
import fcntl
import hmac
import io
import json
import os
import secrets
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from flask import Flask, after_this_request, flash, jsonify, redirect, render_template, request, send_file, session as flask_session, url_for
from werkzeug.security import check_password_hash

from control_queue import (
    ControlLock,
    OperationInProgress,
    ResultTimeout,
    await_result,
    submit_command,
)
import control_queue
from demo_config import (
    OverlayLoadError,
    OverlayValidationError,
    config_status,
    editable_schema,
    has_previous,
    load_desired_config,
    overlay_path,
    desired_digest,
    read_runtime_snapshot,
    runtime_snapshot_path,
    validate_overrides,
)
from forward_config import (
    FORWARD_FIELDS,
    config_path as forward_config_path,
    desired_config as forward_desired_config,
    validate_forward_config,
)
from config import TRADING_PROFILES, demo_csv_path
from dashboard import activity_records, filter_session, load_track
from result_store import DemoResultStore
from strategy.catalog import ALGORITHMS, DEFAULT_ALGORITHM, SELECTABLE_ALGORITHMS, algorithm_label
from validation.validation_engine import create_validation_engine
from validation.reports import generate_validation_report, format_metric_card

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
# Every market in config.TRADING_PROFILES, in its order (btc, usdt, eth, xaut, trx
# since 1 November 2026); the lock suffix is the symbol run.py leases on.
PAIRS = tuple(TRADING_PROFILES)
PAIR_LOCK_SUFFIX = {pair: str(TRADING_PROFILES[pair]["pair"]).lower() for pair in PAIRS}
PAIR_CURRENCY = {pair: str(TRADING_PROFILES[pair]["currency_1"]) for pair in PAIRS}
WALLET_CURRENCIES = ("rls",) + tuple(dict.fromkeys(PAIR_CURRENCY.values()))


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
        "FORWARD_CONTROL_DIR": os.environ.get("LOB_FORWARD_CONTROL_DIR", "runtime/forward/control"),
        "FORWARD_CONFIG_DIR": os.environ.get("LOB_FORWARD_CONFIG_DIR", "runtime/forward/config"),
        "FORWARD_LOCK_DIR": os.environ.get("LOB_FORWARD_LOCK_DIR", "runtime/forward/locks"),
        "FORWARD_RESULTS_DIR": os.environ.get("LOB_FORWARD_RESULTS_DIR", "runtime/forward/results"),
        "FORWARD_TRADING_ENABLED": os.environ.get("FORWARD_TRADING_ENABLED", "0") == "1",
        "FORWARD_ONLY_ADMIN": os.environ.get("LOB_FORWARD_ONLY_ADMIN", "0") == "1",
        "COMMAND_TIMEOUT": 40.0,
    }
    if config:
        cfg.update(config)
    app.config.update(cfg)
    app.secret_key = cfg["SECRET_KEY"]
    app.permanent_session_lifetime = SESSION_IDLE_TIMEOUT

    @app.context_processor
    def _pairs_for_templates():
        # The templates loop over the markets instead of naming btc and usdt.
        return {
            "pairs": list(PAIRS),
            "pair_symbols": {pair: str(TRADING_PROFILES[pair]["pair"]) for pair in PAIRS},
            "pair_currencies": PAIR_CURRENCY,
            "wallet_currencies": WALLET_CURRENCIES,
        }

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


def enqueue_forward(app: Flask, action: str, pair, payload=None):
    control_dir = app.config["FORWARD_CONTROL_DIR"]
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
        return dict(result, pair=pair)


def read_forward_wallet_snapshot(app: Flask, pair: str):
    """Ask the credentialed agent for live balances; the web process has no API key."""
    try:
        result = enqueue_forward(app, "read_wallet_balances", pair)
    except (OperationInProgress, ResultTimeout, OSError) as exc:
        return None, str(exc)
    detail = result.get("detail")
    if not result.get("ok"):
        message = detail.get("error") if isinstance(detail, dict) else None
        return None, message or "Nobitex balances are unavailable."
    if not isinstance(detail, dict) or not isinstance(detail.get("balances"), dict):
        return None, "The wallet agent returned an invalid balance snapshot."
    balances = detail["balances"]
    # The cash wallet and the two original markets must be present; a wallet
    # the forward agent does not read yet (a market added later) is shown as
    # unavailable rather than failing the whole snapshot.
    if any(
        currency not in balances
        or isinstance(balances[currency], bool)
        or not isinstance(balances[currency], (int, float))
        for currency in ("rls", "btc", "usdt")
    ):
        return None, "The wallet agent returned an incomplete balance snapshot."
    for currency in WALLET_CURRENCIES:
        value = balances.get(currency)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            balances[currency] = None
    return detail, None


def enqueue_session_batch(app: Flask, session_ids, *, confirm):
    """Like enqueue_batch, but the varying dimension is session_id inside the
    payload rather than pair (delete_session is a stack-wide action -- pair
    is always None). One delete_session command per session, one audit entry
    per completed sub-action, all under a single held control lock so a
    concurrent Admin action can't interleave mid-batch."""
    control_dir = app.config["CONTROL_DIR"]
    requested_by = flask_session.get("user", "unknown")
    results = []
    with ControlLock(control_dir):
        for session_id in session_ids:
            command_id = submit_command(
                control_dir, "delete_session", None, requested_by=requested_by,
                payload={"session_id": session_id, "confirm": confirm},
            )
            result = await_result(control_dir, command_id, timeout=app.config["COMMAND_TIMEOUT"])
            control_queue.append_audit(control_dir, {
                "command_id": command_id, "action": "delete_session",
                "pair": None, "requested_by": requested_by, "ok": result.get("ok"),
            })
            results.append(dict(result, session_id=session_id))
    return results


def _control_error(exc: Exception):
    """Map a control-queue failure to (status_code, message). ResultTimeout
    means no control agent picked up the command -- distinct from
    OperationInProgress (another operation is mid-flight)."""
    if isinstance(exc, ResultTimeout):
        return 504, (
            "No response from the control agent within the timeout. "
            "Is deploy/control_agent.py running for this environment?"
        )
    return 409, str(exc)


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


def _forward_worker_running(app: Flask, pair: str) -> bool:
    lock_path = Path(app.config["FORWARD_LOCK_DIR"]) / f".worker_live_{PAIR_LOCK_SUFFIX[pair]}.lock"
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
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    session_id = document.get("session_id")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


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
    return _lane_ledger_account(app, pair, None)


def _lane_ledger_account(app: Flask, pair: str, lane: Optional[str]):
    """The active session's paper ledger account of one lane of `pair`
    (paper_<session>_<pair>_<lane>.json), or of the single-worker ledger
    when `lane` is None."""
    session_id = _session_id(app)
    if not session_id:
        return None
    safe_session = str(session_id).replace("/", "_")
    suffix = f"_{lane}" if lane else ""
    path = Path(app.config["STATE_DIR"]) / f"paper_{safe_session}_{PAIR_LOCK_SUFFIX[pair]}{suffix}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("account")
    except (OSError, ValueError):
        return None


def _starting_account(pair: str, path) -> dict:
    """Fallback account view for a pair with no ledger file yet this
    session (worker hasn't completed a round, or hasn't started) -- the
    configured starting point (profile default + Demo overlay, same
    effective values config.html shows) instead of an empty state. Zero
    valuation/fees is accurate here, not a placeholder: nothing has traded
    yet, so there's genuinely nothing to value or charge a fee on."""
    effective = _effective_config_values(pair, path)
    return {
        "inventory": effective["demo_inv_initial"],
        "cash": effective["cash_initial"],
        "valuation_price": 0.0,
        "fees_quote": 0.0,
    }


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


def _is_real_order(record) -> bool:
    """True only when a paper order was actually submitted this round.

    engine.py writes one CSV row per ROUND, not per order (see
    _demo_side_fields): rounds that produced no order still carry a status
    describing why -- "no_quote", "not_sent_insufficient_cash",
    "not_sent_inventory_cap", "not_sent_max_open_orders", "rejected". Those
    are decision records, not orders, and previously rendered in the Orders
    table with a blank ("nan") order id and zero amounts. The presence of a
    real order id is the authoritative signal, not a non-empty status
    string. See reports/demo-portfolio-jump-investigation.md.
    """
    return _has_value(record.get("Buy Order ID")) or _has_value(record.get("Sell Order ID"))


def _decision_reason(record):
    """Why a round produced no order, for display in the decisions view."""
    for key in ("Buy Not Sent Reason", "Sell Not Sent Reason", "No Quote Reason"):
        value = record.get(key)
        if _has_value(value):
            return value
    for key in ("Buy Status", "Sell Status"):
        value = record.get(key)
        if _has_value(value):
            return value
    return None


def _side_decision_reason(record, prefix):
    """Why one side of a round produced no order."""
    for key in (f"{prefix} Not Sent Reason", "No Quote Reason"):
        value = record.get(key)
        if _has_value(value):
            return value
    value = record.get(f"{prefix} Status")
    return value if _has_value(value) else None


def _display_rows(records, *, side=None, include_decisions=False):
    """Explode round records into ONE ROW PER ORDER.

    engine.py writes one CSV row per ROUND, carrying a Buy* order and a Sell*
    order side by side. The Orders table used to render one row per round and
    pick whichever side had an order id first, so every round that both bought
    and sold displayed only the buy and silently dropped the sell -- 11 of 13
    rounds in the observed session hid a sell that way, including three
    completed sell fills that the Dashboard showed and the Admin panel did not.
    """
    rows = []
    for record in records:
        emitted = False
        for prefix in ("Buy", "Sell"):
            if side and side != prefix.lower():
                continue
            order_id = record.get(f"{prefix} Order ID")
            status = record.get(f"{prefix} Status")
            if not _has_value(order_id) and not _has_value(status):
                continue
            is_order = _has_value(order_id)
            if not is_order and not include_decisions:
                continue
            rows.append({
                "timestamp": record.get("Timestamp"),
                "round": record.get("Round"),
                "algorithm": algorithm_label(record.get("Strategy") or DEFAULT_ALGORITHM),
                "side": prefix.lower(),
                "is_order": is_order,
                "status": status,
                "order_id": order_id if is_order else None,
                "original": record.get(f"{prefix} Original Amount"),
                "matched": record.get(f"{prefix} Matched Amount"),
                "remaining": record.get(f"{prefix} Remaining Amount"),
                "limit_price": record.get(f"{prefix} Limit Price"),
                "level": record.get(f"{prefix} Selected Level"),
                "fill_reason": record.get(f"{prefix} Fill Reason"),
                "decision_reason": None if is_order else _side_decision_reason(record, prefix),
            })
            emitted = True
        if not emitted and include_decisions and not side:
            # Neither side quoted this round (no_quote) -- one sideless row.
            rows.append({
                "timestamp": record.get("Timestamp"),
                "round": record.get("Round"),
                "algorithm": algorithm_label(record.get("Strategy") or DEFAULT_ALGORITHM),
                "side": None,
                "is_order": False,
                "status": None,
                "order_id": None,
                "original": None, "matched": None, "remaining": None,
                "limit_price": None, "level": None, "fill_reason": None,
                "decision_reason": _decision_reason(record),
            })
    return rows


def _filter_orders(records, side=None, status=None, include_decisions=False):
    """Narrow activity_records() rows by side (buy/sell) and/or status
    (open/filled/cancelled/...) -- both admin_orders' JSON and HTML
    responses share this so filtering behaves identically either way.

    By default only rows carrying a real submitted order are returned;
    include_decisions=True adds the no-order rounds back with their reason.
    """
    def keep(record):
        if not include_decisions and not _is_real_order(record):
            return False
        buy_id, sell_id = record.get("Buy Order ID"), record.get("Sell Order ID")
        buy_status, sell_status = record.get("Buy Status"), record.get("Sell Status")
        if side == "buy" and not (_has_value(buy_id) or _has_value(buy_status)):
            return False
        if side == "sell" and not (_has_value(sell_id) or _has_value(sell_status)):
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


def _zip_backup_response(backup_dir: Path, download_name: str):
    """Zip the files of one backup directory into a temporary file on disk
    and stream that file. The archive used to be built in an ``io.BytesIO``:
    with a 150 MB SQLite store in the snapshot the admin process reached its
    384 MiB container limit and was OOM-killed on every export (three times
    on 23 October 2026). On disk the memory stays flat whatever the size."""
    handle = tempfile.NamedTemporaryFile(prefix="lob-export-", suffix=".zip", delete=False)
    handle.close()
    archive_path = Path(handle.name)
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(backup_dir.iterdir()):
                if file_path.is_file():
                    archive.write(file_path, arcname=file_path.name)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    @after_this_request
    def _remove_archive(response):
        # The response holds the open file; on Linux the unlinked file stays
        # readable until it is closed, so the download completes.
        try:
            archive_path.unlink()
        except OSError:
            pass
        return response

    return send_file(
        archive_path, mimetype="application/zip", as_attachment=True,
        download_name=download_name,
    )


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
    """Resolve the selected pair through the worker's desired-config seam."""
    return load_desired_config(pair, Path(path).parent, TRADING_PROFILES)


def _serialized_schema(pair: str) -> dict:
    return {
        field: {
            "type": definition.value_type.__name__,
            "minimum": definition.minimum,
            "maximum": definition.maximum,
            "mode": definition.mode.value,
            "help_text": definition.help_text,
            "choices": list(definition.choices) if definition.choices else None,
        }
        for field, definition in editable_schema(pair, TRADING_PROFILES).items()
    }


def _choice_labels() -> Dict[str, Dict[str, str]]:
    """Display text for the choices of the str fields (the algorithm drop-down)."""
    return {"algorithm": {key: f"{info.label} — {info.title}" for key, info in ALGORITHMS.items()}}


def _algorithm_labels() -> Dict[str, str]:
    return {key: info.label for key, info in ALGORITHMS.items()}


def _demo_lanes_enabled(app: Flask, pair: str) -> Optional[Dict[str, bool]]:
    """Which algorithms the pair's Demo overlay switches on (its lanes), or
    None when the overlay cannot be read."""
    try:
        desired = load_desired_config(pair, app.config["STATE_DIR"], TRADING_PROFILES)
    except (OverlayLoadError, OverlayValidationError, KeyError, OSError):
        return None
    return {key: bool(desired.get(f"{key}_enabled", False)) for key in SELECTABLE_ALGORITHMS}


def _lane_has_run(app: Flask, pair: str, lane: str, session_id: Optional[str]) -> bool:
    """A lane that has published its runtime snapshot for the current session
    has started (the snapshot is written at its first round boundary)."""
    if not session_id:
        return False
    try:
        snapshot = read_runtime_snapshot(app.config["STATE_DIR"], pair, lane)
    except OverlayLoadError:
        return False
    return bool(snapshot) and snapshot.get("session_id") == str(session_id)


def _algorithms_in_force(app: Flask) -> Dict[str, Dict[str, Optional[str]]]:
    """Per pair: the Demo lanes switched on (a comma-separated list of
    algorithm keys, "" when none, None when the overlay cannot be read) and
    the live configuration's algorithm (what a live worker gets when it is
    started; a running one keeps the algorithm it started with)."""
    demo: Dict[str, Optional[str]] = {}
    live: Dict[str, Optional[str]] = {}
    for pair in PAIRS:
        lanes = _demo_lanes_enabled(app, pair)
        demo[pair] = None if lanes is None else ",".join(key for key, on in lanes.items() if on)
        try:
            desired = forward_desired_config(pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"])
            live[pair] = str(desired.get("algorithm") or DEFAULT_ALGORITHM)
        except (OverlayLoadError, OverlayValidationError, KeyError, OSError):
            live[pair] = None
    return {"demo": demo, "live": live}


def _live_newest_row(app: Flask, pair: str) -> Optional[Dict[str, str]]:
    """The newest row of a pair's live round history (<pair>_track.csv), as
    column -> text: the worker process runs under another user and is not
    visible from here, so its rows are how this page knows what it runs.
    None when the history cannot be read or holds no row."""
    path = Path(app.config["FORWARD_RESULTS_DIR"]) / f"{pair}_track.csv"
    try:
        data = path.read_bytes()        # read-only: admin_app never opens files itself
    except OSError:
        return None
    header = data.split(b"\n", 1)[0].decode("utf-8", "replace")
    tail = data[-65536:].decode("utf-8", "replace").splitlines()
    last = next((line for line in reversed(tail) if line.strip()), "")
    if not header.strip() or not last or last.strip() == header.strip():
        return None
    try:
        columns = next(csv.reader([header]))
        values = next(csv.reader([last]))
    except (StopIteration, csv.Error):
        return None
    return dict(zip(columns, values))


def _row_algorithm(row: Dict[str, str]) -> str:
    """Rows written before the Strategy column existed mean Algorithm 1."""
    return (row.get("Strategy") or "").strip() or DEFAULT_ALGORITHM


def _live_running_algorithm(app: Flask, pair: str) -> Optional[str]:
    """The algorithm a running live worker is using: the Strategy cell of the
    newest row of its round history. None when the history cannot be read."""
    row = _live_newest_row(app, pair)
    return _row_algorithm(row) if row is not None else None


def _algorithm_choices() -> list:
    """(key, display text) of every algorithm that can be chosen, in catalog order."""
    return [(key, f"{ALGORITHMS[key].label} — {ALGORITHMS[key].title}") for key in SELECTABLE_ALGORITHMS]


def _algorithm_state(app: Flask) -> dict:
    """Where each algorithm runs, for the Algorithm page: per pair, the Demo
    worker's lanes (one per algorithm, switched on or off in the pair's
    configuration; every lane and pair shares the one Demo session, each
    lane with its own paper account) and the live worker (the algorithm of
    its newest round while it runs, the algorithm saved for its next start,
    and the session of its newest round)."""
    in_force = _algorithms_in_force(app)
    demo_session = _session_id(app)
    demo, live = [], []
    for pair in PAIRS:
        symbol = TRADING_PROFILES[pair]["pair"]
        worker_running = _worker_running(app, pair)
        enabled = _demo_lanes_enabled(app, pair)
        lanes = {}
        for key in SELECTABLE_ALGORITHMS:
            on = None if enabled is None else enabled[key]
            if on is None:
                state = "unknown"
            elif not on:
                state = "off"
            elif not worker_running:
                state = "worker_stopped"
            elif _lane_has_run(app, pair, key, demo_session):
                state = "running"
            else:
                state = "starting"
            lanes[key] = {
                "enabled": on, "state": state,
                "session_label": f"{algorithm_label(key)} · {demo_session}" if demo_session else None,
            }
        demo.append({
            "pair": pair, "symbol": symbol, "running": worker_running,
            "algorithm": in_force["demo"][pair], "session_id": demo_session, "lanes": lanes,
            "enabled_labels": [algorithm_label(k) for k, lane in lanes.items() if lane["enabled"]],
        })
        running = _forward_worker_running(app, pair)
        newest = _live_newest_row(app, pair) if running else None
        session_id = ((newest.get("Session_id") or "").strip() or None) if newest else None
        live.append({
            "pair": pair, "symbol": symbol, "running": running,
            "running_algorithm": _row_algorithm(newest) if newest else None,
            "next_algorithm": in_force["live"][pair],
            "session_id": session_id,
            "session_started_at": _live_session_started_at(session_id) if session_id else None,
        })
    return {"demo_session_id": demo_session, "demo": demo, "live": live}


def _form_value(definition, values):
    """Coerce the submitted form value(s) of one field to the type its
    definition declares (None = unparseable)."""
    value = values[-1]
    if definition.value_type is bool:
        return any(item in ("true", "on", "1", "True") for item in values)
    if definition.value_type is str:
        return value
    try:
        return int(value) if definition.value_type is int else float(value)
    except ValueError:
        return None


def _pair_runtime_snapshot(app: Flask, pair: str, desired) -> Optional[Dict[str, Any]]:
    """The pair's runtime truth. Since the demo lanes (20 October 2026) every
    lane publishes runtime_config_<pair>_<lane>.json and the pair-level file
    is the single-worker era's, so it goes stale: the Config page showed
    "Runtime status unknown/stale" with no applied values while every lane
    had applied the overlay. Among the switched-on lanes, a snapshot that
    still differs from the desired digest wins (the pair is applied only when
    every lane is), else the newest one; the pair file is read only when no
    lane has ever published."""
    state_dir = app.config["STATE_DIR"]
    enabled = [lane for lane in SELECTABLE_ALGORITHMS if desired.get(f"{lane}_enabled")]
    snapshots = []
    for lane in enabled or SELECTABLE_ALGORITHMS:
        snapshot = read_runtime_snapshot(state_dir, pair, lane)
        if snapshot:
            snapshots.append(snapshot)
    if not snapshots:
        if any(runtime_snapshot_path(state_dir, pair, lane).exists() for lane in SELECTABLE_ALGORITHMS):
            return None
        return read_runtime_snapshot(state_dir, pair)
    wanted = desired_digest(dict(desired))
    behind = [item for item in snapshots if item.get("desired_digest") != wanted]
    if behind:
        return max(behind, key=lambda item: str(item.get("updated_at") or ""))
    return max(snapshots, key=lambda item: str(item.get("updated_at") or ""))


def _config_view(app: Flask, pair: str, path) -> dict:
    desired = _effective_config_values(pair, path)
    runtime_error = None
    try:
        snapshot = _pair_runtime_snapshot(app, pair, desired)
    except OverlayLoadError as exc:
        snapshot = None
        runtime_error = str(exc)
    current_session_id = _session_id(app)
    status = config_status(
        pair,
        desired,
        snapshot if current_session_id is not None else None,
        _worker_running(app, pair),
        current_session_id=current_session_id,
    )
    return {
        "pair": pair,
        "desired": desired,
        "applied": status["applied"],
        "pending_new_session": status["pending_new_session"],
        "status": status,
        "runtime_error": runtime_error,
        "snapshot": snapshot,
    }


def _config_page_context(
    app: Flask, pair: str, path, *, error=None, success=None, attempted=None,
    command_error=None,
) -> dict:
    view = _config_view(app, pair, path)
    if command_error:
        view["status"] = {
            **view["status"],
            "state": "failed",
            "label": "Failed to apply",
            "error": command_error,
            "fields": {
                field: {
                    **row,
                    "state": "failed",
                    "label": "Failed to apply",
                }
                for field, row in view["status"]["fields"].items()
            },
        }
    values = view["desired"]
    if attempted:
        values = dict(values, **attempted)
    return {
        **view,
        "values": values,
        "has_previous": has_previous(path),
        "schema": editable_schema(pair, TRADING_PROFILES),
        "choice_labels": _choice_labels(),
        "csrf_token": get_csrf_token(),
        "session_id": _session_id(app),
        "error": error,
        "success": success,
    }


def _command_failure_message(result: dict) -> str:
    return json.dumps(result, sort_keys=True, default=str)


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


def _session_rows(csv_path: Path):
    """Per-session counts of one track: from the SQLite store when it exists
    (every session, however old), else from the CSV."""
    store_path = csv_path.with_suffix(".sqlite3")
    if store_path.exists():
        try:
            return DemoResultStore.open_existing(store_path).session_summary()
        except Exception:
            pass
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path, usecols=["Session_id", "Timestamp"], dtype={"Session_id": str})
    except Exception:
        return []
    rows = []
    for session_id, group in df.groupby("Session_id"):
        stamps = group["Timestamp"].dropna()
        rows.append({
            "session_id": str(session_id), "rounds": int(len(group)),
            "first_timestamp": str(stamps.min()) if not stamps.empty else None,
            "last_timestamp": str(stamps.max()) if not stamps.empty else None,
        })
    return rows


def _list_sessions(app: Flask):
    """Every Demo session recorded in either pair's round history, most
    recent first. A pair's store accumulates rows from every session ever
    run against it -- this aggregates both so a session that only traded
    one pair, or traded both, is reported consistently."""
    active = _session_id(app)
    by_session: Dict[str, Dict[str, Any]] = {}
    for pair in PAIRS:
        for csv_path in _demo_store_paths(app, pair):
            for row in _session_rows(csv_path):
                session_id = row["session_id"]
                if not session_id:
                    continue
                entry = by_session.setdefault(session_id, {
                    "session_id": session_id, "is_active": session_id == active, "pairs": {}, "algorithms": [],
                })
                counts = entry["pairs"].setdefault(pair, {"rounds": 0, "first_timestamp": None, "last_timestamp": None})
                counts["rounds"] += int(row["rounds"])
                for key in ("first_timestamp", "last_timestamp"):
                    value = row.get(key)
                    if value and (counts[key] is None or (value < counts[key] if key == "first_timestamp" else value > counts[key])):
                        counts[key] = value
                for key in row.get("algorithms") or []:
                    if key not in entry["algorithms"]:
                        entry["algorithms"].append(key)
    for entry in by_session.values():
        entry["label"] = " → ".join(algorithm_label(k) for k in entry["algorithms"]) + f" · {entry['session_id']}" \
            if entry["algorithms"] else entry["session_id"]
    return sorted(by_session.values(), key=lambda entry: entry["session_id"], reverse=True)


def _demo_store_paths(app: Flask, pair: str) -> list:
    """The pair's demo result files in this app's RESULTS_DIR: the single-
    worker history (<pair>_track_demo.csv) and every lane (<pair>_track_demo_<lane>.csv)."""
    results_dir = Path(app.config["RESULTS_DIR"])
    stem = os.path.splitext(os.path.basename(demo_csv_path(TRADING_PROFILES[pair]["output_csv"])))[0]
    return [results_dir / f"{stem}.csv"] + [results_dir / f"{stem}_{lane}.csv" for lane in SELECTABLE_ALGORITHMS]


def _live_session_started_at(session_id: str) -> Optional[str]:
    """run.py --mode real names a session after its start time (a Unix
    timestamp); show it as a date."""
    try:
        return datetime.fromtimestamp(float(session_id), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _list_live_sessions(app: Flask):
    """Every live forward-test session recorded in the real-money round
    history (<pair>_track.csv under FORWARD_RESULTS_DIR), most recent first.
    A running live worker's session (the pair's newest) is marked active."""
    by_session: Dict[str, Dict[str, Any]] = {}
    for pair in PAIRS:
        csv_path = Path(app.config["FORWARD_RESULTS_DIR"]) / os.path.basename(TRADING_PROFILES[pair]["output_csv"])
        rows = [row for row in _session_rows(csv_path) if row["session_id"]]
        running = _forward_worker_running(app, pair)
        newest = max(rows, key=lambda row: row.get("last_timestamp") or "", default=None)
        for row in rows:
            session_id = row["session_id"]
            entry = by_session.setdefault(session_id, {
                "session_id": session_id, "started_at": _live_session_started_at(session_id),
                "is_active": False, "pairs": {},
            })
            entry["pairs"][pair] = {
                "rounds": row["rounds"],
                "first_timestamp": row.get("first_timestamp"),
                "last_timestamp": row.get("last_timestamp"),
            }
            if running and row is newest:
                entry["is_active"] = True
    return sorted(by_session.values(), key=lambda entry: entry["session_id"], reverse=True)


def _sessions_html_response(app: Flask):
    return render_template(
        "admin/sessions.html", sessions=_list_sessions(app),
        live_sessions=_list_live_sessions(app),
        live_workers_running=any(_forward_worker_running(app, pair) for pair in PAIRS),
        csrf_token=get_csrf_token(), session_id=_session_id(app),
    )


def _demo_managed(app: Flask) -> bool:
    """Whether this panel drives the Demo stack: it has the Demo control
    queue. FORWARD_ONLY_ADMIN only makes the index land on the Live Forward
    page; the Docker admin sets it and still manages the Demo."""
    return Path(app.config["CONTROL_DIR"]).is_dir()


def _algorithm_html_response(app: Flask, selected: Optional[str], status: int = 200):
    return render_template(
        "admin/algorithm.html", selected=selected, algorithm_choices=_algorithm_choices(),
        algorithm_labels=_algorithm_labels(), state=_algorithm_state(app),
        demo_managed=_demo_managed(app),
        trading_enabled=app.config["FORWARD_TRADING_ENABLED"],
        csrf_token=get_csrf_token(), session_id=_session_id(app),
    ), status


def _activate_algorithm(app: Flask, algorithm: str, target: str, pair: str, enabled: bool = True) -> dict:
    """Save one change through the same path as the config pages. Demo: switch
    the algorithm's lane on or off in the pair's overlay (`<algorithm>_enabled`;
    the worker starts or stops the lane at its next check, within seconds).
    Live: save `algorithm` in the pair's live configuration (used when the
    live worker is next started; a running one keeps its algorithm). Every
    other setting is resubmitted unchanged. Raises what the config pages'
    saves raise."""
    if target == "demo":
        desired = load_desired_config(pair, app.config["STATE_DIR"], TRADING_PROFILES)
        desired[f"{algorithm}_enabled"] = bool(enabled)
        validated = validate_overrides(desired, pair=pair, profiles=TRADING_PROFILES)
        return enqueue_and_wait(app, "apply_config", pair, payload={"overrides": validated})
    desired = forward_desired_config(pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"])
    desired["algorithm"] = algorithm
    validated = validate_forward_config(desired, pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"])
    return enqueue_forward(app, "apply_forward_config", pair, payload={"overrides": validated})


def _activation_message(app: Flask, algorithm: str, target: str, pair: str, enabled: bool = True) -> str:
    label, symbol = ALGORITHMS[algorithm].label, TRADING_PROFILES[pair]["pair"]
    if target == "demo":
        session_id = _session_id(app)
        where = f" in session {label} · {session_id}" if session_id else ""
        if not enabled:
            return (f"{label} lane switched off for the {symbol} demo: the worker stops it within seconds "
                    "and cancels its open paper orders; its results stay on the dashboard.")
        when = "starts it within seconds" if _worker_running(app, pair) else "starts it when the worker is started"
        return (f"{label} lane switched on for the {symbol} demo: the worker {when}{where}, "
                "with its own paper account and results.")
    message = f"{label} saved for the {symbol} live worker: it runs it from its next start."
    running = _live_running_algorithm(app, pair) if _forward_worker_running(app, pair) else None
    if running and running != algorithm:
        message += (f" The running worker keeps {_algorithm_labels().get(running, running)} "
                    "until it is stopped and started again on Live Forward Test.")
    return message


def _register_routes(app: Flask) -> None:
    @app.route("/admin/health")
    def admin_health():
        # Unauthenticated on purpose -- container healthcheck endpoint,
        # returns nothing but a 200 and touches no Demo state.
        return ("ok", 200)

    @app.route("/admin/")
    @login_required
    def admin_index():
        if app.config["FORWARD_ONLY_ADMIN"]:
            return redirect(url_for("admin_forward_page"))
        status = {
            "session_id": _session_id(app),
            "control_lock_held": _control_lock_held(app),
            "workers": {pair: _worker_running(app, pair) for pair in PAIRS},
            "forward_workers": {pair: _forward_worker_running(app, pair) for pair in PAIRS},
            "forward_trading_enabled": app.config["FORWARD_TRADING_ENABLED"],
            "algorithms": _algorithms_in_force(app),
            "recent_audit": _recent_audit(app),
        }
        if _wants_html():
            return render_template("admin/overview.html", csrf_token=get_csrf_token(),
                                   algorithm_labels=_algorithm_labels(),
                                   algorithm_choices=_algorithm_choices(), **status)
        return jsonify(status)

    @app.route("/admin/algorithm")
    @login_required
    def admin_algorithm_page():
        """Choose an algorithm, then activate it per pair on the Demo stack
        and for the live forward test, each table with its active session."""
        selected = request.args.get("algorithm") or None
        if selected is not None and selected not in SELECTABLE_ALGORITHMS:
            if _wants_html():
                return _error_page(400, f"Unknown algorithm: {selected}")
            return (f"unknown algorithm: {selected}", 400)
        if _wants_html():
            return _algorithm_html_response(app, selected)
        return jsonify(dict(_algorithm_state(app), algorithm=selected))

    @app.route("/admin/algorithm/activate", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_algorithm_activate():
        algorithm = request.form.get("algorithm")
        target = request.form.get("target")
        pair = request.form.get("pair")
        action = request.form.get("action") or "activate"
        enabled = action == "activate"
        problem = None
        if algorithm not in SELECTABLE_ALGORITHMS:
            problem = "Unknown algorithm."
        elif target not in ("demo", "live"):
            problem = "Invalid target (expected demo or live)."
        elif action not in ("activate", "deactivate") or (target == "live" and not enabled):
            problem = "Invalid action (expected activate, or deactivate for a demo lane)."
        elif pair not in PAIRS:
            problem = "Invalid pair (expected one of " + ", ".join(PAIRS) + ")."
        elif target == "demo" and not _demo_managed(app):
            problem = "This panel does not manage the Demo stack."
        if problem:
            if _wants_html():
                return _error_page(400, problem)
            return (problem, 400)
        try:
            result = _activate_algorithm(app, algorithm, target, pair, enabled)
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            result = None
        except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as exc:
            config_page = "Config" if target == "demo" else "Live Config"
            status, message = 400, f"{config_page} {pair.upper()} cannot be saved: {exc}"
            result = None
        if result is not None and not result.get("ok"):
            status, message = 502, _command_failure_message(result)
        elif result is not None:
            message = _activation_message(app, algorithm, target, pair, enabled)
            if _wants_html():
                flash(message, "success")
                return redirect(url_for("admin_algorithm_page", algorithm=algorithm))
            return jsonify(dict(result, message=message))
        if _wants_html():
            flash(message, "error")
            return _algorithm_html_response(app, algorithm, status)
        return (message, status)

    @app.route("/admin/config/<pair>", methods=["GET", "POST"])
    @login_required
    def admin_config(pair):
        if pair not in PAIRS:
            if _wants_html():
                return _error_page(404, "Unknown pair.")
            return ("unknown pair", 404)
        path = overlay_path(app.config["STATE_DIR"], pair)
        if request.method == "GET":
            try:
                if _wants_html():
                    return render_template(
                        "admin/config.html", **_config_page_context(app, pair, path),
                    )
                view = _config_view(app, pair, path)
            except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as exc:
                if _wants_html():
                    return _error_page(500, str(exc))
                return jsonify({"error": str(exc)}), 500
            return jsonify({
                "pair": pair,
                "desired": view["desired"],
                "applied": view["applied"],
                "pending_new_session": view["pending_new_session"],
                "status": view["status"],
                "runtime_error": view["runtime_error"],
                "has_previous": has_previous(path),
                "schema": _serialized_schema(pair),
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
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _workers_html_response(app, None), status
            return (message, status)
        if _wants_html():
            failed = [result for result in results if not result.get("ok")]
            if failed:
                flash(_command_failure_message({"results": failed}), "error")
                return _workers_html_response(app, results), 502
            flash(f"{action.capitalize()} requested for {', '.join(pairs)}.", "success")
            return _workers_html_response(app, results)
        return jsonify({"results": results})

    @app.route("/admin/forward")
    @login_required
    def admin_forward_page():
        workers = {pair: _forward_worker_running(app, pair) for pair in PAIRS}
        return render_template(
            "admin/forward.html", workers=workers,
            trading_enabled=app.config["FORWARD_TRADING_ENABLED"],
            csrf_token=get_csrf_token(), session_id=None,
            algorithms=_algorithms_in_force(app), algorithm_labels=_algorithm_labels(),
            algorithm_choices=_algorithm_choices(),
            live_running={pair: _live_running_algorithm(app, pair) if workers[pair] else None for pair in PAIRS},
        )

    @app.route("/admin/forward/<action>", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_forward_action(action):
        if action not in {"start", "stop"}:
            return _error_page(400, "Unknown forward-test action.")
        pair = request.form.get("pair")
        if pair not in PAIRS:
            return _error_page(400, "Invalid pair (expected one of " + ", ".join(PAIRS) + ").")
        if action == "start":
            if not app.config["FORWARD_TRADING_ENABLED"]:
                return _error_page(503, "Live forward trading is disabled by FORWARD_TRADING_ENABLED.")
            expected = f"START {TRADING_PROFILES[pair]['pair']} LIVE"
            if request.form.get("confirm_phrase", "").strip() != expected:
                return _error_page(400, f"Type {expected} exactly to start live orders.")
            try:
                validate_forward_config(
                    forward_desired_config(pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"]),
                    pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"],
                )
            except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as exc:
                return _error_page(400, str(exc))
            agent_action = "start_forward_worker"
            payload = {"confirmed": True}
        else:
            agent_action = "stop_forward_worker"
            payload = None
        try:
            result = enqueue_forward(app, agent_action, pair, payload=payload)
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            return _error_page(status, message)
        if not result.get("ok"):
            return _error_page(502, _command_failure_message(result))
        detail = result.get("detail")
        if action == "stop" and (not isinstance(detail, dict) or detail.get("stopped") is not True):
            return _error_page(502, "Forward-test worker did not confirm it stopped.")
        flash(f"Forward-test worker {action} requested for {pair.upper()}.", "success")
        return redirect(url_for("admin_forward_page"))

    @app.route("/admin/forward/config/<pair>", methods=["GET", "POST"])
    @login_required
    def admin_forward_config(pair):
        if pair not in PAIRS:
            return _error_page(404, "Unknown pair.")
        if request.method == "POST" and not _valid_csrf(request.form.get("csrf_token")):
            return _error_page(400, "CSRF token missing or invalid. Go back and try again.")
        path = forward_config_path(pair, app.config["FORWARD_CONFIG_DIR"])
        schema = {
            key: definition for key, definition in editable_schema(pair, TRADING_PROFILES).items()
            if key in FORWARD_FIELDS
        }
        try:
            desired = forward_desired_config(pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"])
        except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as exc:
            return _error_page(500, str(exc))
        if request.method == "GET":
            wallet_snapshot, wallet_error = read_forward_wallet_snapshot(app, pair)
            return render_template(
                "admin/forward_config.html", pair=pair, schema=schema,
                values=desired, csrf_token=get_csrf_token(), session_id=None,
                success=None, error=None, choice_labels=_choice_labels(),
                wallet_snapshot=wallet_snapshot, wallet_error=wallet_error,
            )
        allowed = set(schema) | {"csrf_token"}
        unknown = sorted(set(request.form) - allowed)
        if unknown:
            return _error_page(400, f"Unknown field: {unknown[0]}")
        submitted = {}
        for key, definition in schema.items():
            if key not in request.form:
                continue
            value = _form_value(definition, request.form.getlist(key))
            if value is None:
                return _error_page(400, f"Invalid value for {key}.")
            submitted[key] = value
        desired.update(submitted)
        try:
            validated = validate_forward_config(desired, pair, TRADING_PROFILES, app.config["FORWARD_CONFIG_DIR"])
            result = enqueue_forward(
                app, "apply_forward_config", pair, payload={"overrides": validated},
            )
        except (OverlayValidationError, OperationInProgress, ResultTimeout, KeyError, OSError) as exc:
            status, message = _control_error(exc) if isinstance(exc, (OperationInProgress, ResultTimeout)) else (400, str(exc))
            wallet_snapshot, wallet_error = read_forward_wallet_snapshot(app, pair)
            return render_template(
                "admin/forward_config.html", pair=pair, schema=schema,
                values=desired, csrf_token=get_csrf_token(), session_id=None,
                success=None, error=message, choice_labels=_choice_labels(),
                wallet_snapshot=wallet_snapshot, wallet_error=wallet_error,
            ), status
        if not result.get("ok"):
            return _error_page(502, _command_failure_message(result))
        flash("Forward-test configuration saved. Stop and start the worker to apply it.", "success")
        return redirect(url_for("admin_forward_config", pair=pair))

    @app.route("/admin/session/new", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_new_session():
        # The Algorithm page offers a new session too and wants the operator back on it.
        back_to_algorithm = request.form.get("return_to") == "algorithm"
        chosen = request.form.get("algorithm")
        chosen = chosen if chosen in SELECTABLE_ALGORITHMS else None
        try:
            result = enqueue_and_wait(app, "new_session")
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                if back_to_algorithm:
                    return _algorithm_html_response(app, chosen, status)
                return _workers_html_response(app, None), status
            return (message, status)
        if _wants_html():
            if not result.get("ok"):
                flash(_command_failure_message(result), "error")
                if back_to_algorithm:
                    return _algorithm_html_response(app, chosen, 502)
                return _workers_html_response(app, result), 502
            flash("New demo session created.", "success")
            if back_to_algorithm:
                return redirect(url_for("admin_algorithm_page", **({"algorithm": chosen} if chosen else {})))
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
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _workers_html_response(app, None), status
            return (message, status)
        if _wants_html():
            failed = [result for result in results if not result.get("ok")]
            if failed:
                flash(_command_failure_message({"results": failed}), "error")
                return _workers_html_response(app, results), 502
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
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _backups_html_response(app), status
            return (message, status)
        if _wants_html():
            if not result.get("ok"):
                flash(_command_failure_message(result), "error")
                return _backups_html_response(app), 502
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
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _backups_html_response(app), status
            return (message, status)
        if _wants_html():
            if not result.get("ok"):
                flash(_command_failure_message(result), "error")
                return _backups_html_response(app), 502
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
        return _zip_backup_response(backup_dir, f"{name}.zip")

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
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _recovery_html_response(app), status
            return (message, status)
        if _wants_html():
            if not result.get("ok"):
                flash(_command_failure_message(result), "error")
                return _recovery_html_response(app), 502
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
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _recovery_html_response(app), status
            return (message, status)
        if _wants_html():
            if not result.get("ok"):
                flash(_command_failure_message(result), "error")
                return _recovery_html_response(app), 502
            flash("Safe recovery applied.", "success")
            return _recovery_html_response(app)
        return jsonify(result)

    @app.route("/admin/sessions", methods=["GET"])
    @login_required
    def admin_sessions():
        if _wants_html():
            return _sessions_html_response(app)
        return jsonify({"sessions": _list_sessions(app), "live_sessions": _list_live_sessions(app)})

    @app.route("/admin/forward/sessions/<session_id>/delete", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_forward_sessions_delete(session_id):
        """Delete one live forward-test session's rows from the real-money
        round history, through the forward agent (it backs the files up and
        refuses while a live worker runs)."""
        if request.form.get("confirm") != "DELETE":
            message = "Confirmation required."
            if _wants_html():
                flash(message, "error")
                return _sessions_html_response(app), 400
            return (message, 400)
        try:
            result = enqueue_forward(
                app, "delete_forward_session", None,
                payload={"session_id": session_id, "confirm": "DELETE"},
            )
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _sessions_html_response(app), status
            return (message, status)
        if not result.get("ok"):
            message = _command_failure_message(result)
            if _wants_html():
                flash(message, "error")
                return _sessions_html_response(app), 502
            return jsonify(result), 502
        if _wants_html():
            flash(f"Live session {session_id} deleted.", "success")
            return redirect(url_for("admin_sessions"))
        return jsonify(result)

    @app.route("/admin/sessions/<session_id>/delete", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_sessions_delete(session_id):
        if request.form.get("confirm") != "DELETE":
            message = "Confirmation required."
            if _wants_html():
                flash(message, "error")
                return _sessions_html_response(app), 400
            return (message, 400)
        try:
            result = enqueue_and_wait(
                app, "delete_session", None,
                payload={"session_id": session_id, "confirm": "DELETE"},
            )
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _sessions_html_response(app), status
            return (message, status)
        if not result.get("ok"):
            message = _command_failure_message(result)
            if _wants_html():
                flash(message, "error")
                return _sessions_html_response(app), 502
            return jsonify(result), 502
        if _wants_html():
            flash(f"Session {session_id} deleted.", "success")
            return _sessions_html_response(app)
        return jsonify(result)

    @app.route("/admin/sessions/delete", methods=["POST"])
    @login_required
    @csrf_protected
    def admin_sessions_delete_bulk():
        session_ids = [value for value in request.form.getlist("session_ids") if value]
        if request.form.get("confirm") != "DELETE":
            message = "Confirmation required."
            if _wants_html():
                flash(message, "error")
                return _sessions_html_response(app), 400
            return (message, 400)
        if not session_ids:
            message = "No sessions selected."
            if _wants_html():
                flash(message, "error")
                return _sessions_html_response(app), 400
            return (message, 400)
        try:
            results = enqueue_session_batch(app, session_ids, confirm="DELETE")
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                flash(message, "warning")
                return _sessions_html_response(app), status
            return (message, status)
        failed = [result for result in results if not result.get("ok")]
        if _wants_html():
            if failed:
                flash(_command_failure_message({"results": failed}), "error")
                return _sessions_html_response(app), 502
            flash(f"Deleted {len(session_ids)} session(s).", "success")
            return _sessions_html_response(app)
        if failed:
            return jsonify({"results": results}), 502
        return jsonify({"results": results})

    @app.route("/admin/export", methods=["GET"])
    @login_required
    def admin_export():
        try:
            result = enqueue_and_wait(app, "create_backup", None, payload={"prefix": "export"})
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if _wants_html():
                return _error_page(status, message)
            return (message, status)
        backup_name = (result.get("detail") or {}).get("backup")
        if not result.get("ok"):
            if _wants_html():
                return _error_page(502, _command_failure_message(result))
            return ("export snapshot failed", 500)
        if not backup_name:
            if _wants_html():
                return _error_page(500, "Export snapshot failed.")
            return ("export snapshot failed", 500)
        backup_dir = Path(app.config["BACKUPS_DIR"]) / backup_name
        return _zip_backup_response(backup_dir, f"{backup_name}.zip")

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
        # Default view is real submitted orders only; ?view=decisions adds
        # the rounds that deliberately produced no order (no_quote,
        # insufficient cash, inventory/open-order caps) with their reason.
        view = request.args.get("view") or "orders"
        include_decisions = view == "decisions"
        # Join against this app's own RESULTS_DIR (not the global profile
        # path) so a ro-mounted runtime/results in production, or a tmp
        # dir in tests, is what actually gets read.
        store_paths = [str(path) for path in _demo_store_paths(app, pair)]
        df = load_track(tuple(store_paths))  # dashboard.py's own pure loader -- every store of the pair
        # Scope to the running session. The CSV accumulates every session ever
        # run and round numbers restart at 1 in each, so without this the table
        # listed three different "Round 8" rows from three sessions -- and
        # disagreed with the Dashboard, which does filter (dashboard.filter_session).
        # The Admin panel manages the *running* stack, so that is the session
        # it must report on.
        session_id = _session_id(app)
        if df is not None and session_id:
            df = filter_session(df, session_id)
        records = activity_records(df) if df is not None else []
        filtered = _filter_orders(
            records, side=side, status=status, include_decisions=include_decisions
        )
        orders = _serialize_orders(filtered)
        for record in orders:
            record["is_order"] = _is_real_order(record)
            record["decision_reason"] = None if record["is_order"] else _decision_reason(record)
        if _wants_html():
            return render_template(
                "admin/orders.html",
                # One row per ORDER, so a round that both bought and sold shows
                # both. The JSON keeps the round-shaped records it always
                # returned -- those were never lossy, only the table was.
                rows=_display_rows(
                    orders, side=side, include_decisions=include_decisions
                ),
                selected_pair=pair,
                side_filter=side, status_filter=status, view=view,
                decision_count=sum(1 for r in records if not _is_real_order(r)),
                csrf_token=get_csrf_token(), session_id=session_id,
            )
        return jsonify({"orders": orders})

    @app.route("/admin/balances")
    @login_required
    def admin_balances():
        balances = {pair: _ledger_account(app, pair) for pair in PAIRS}
        lane_balances = {
            pair: {lane: _lane_ledger_account(app, pair, lane) for lane in SELECTABLE_ALGORITHMS}
            for pair in PAIRS
        }
        if _wants_html():
            # Browsers get the configured starting point instead of an empty
            # state when a pair has no ledger yet this session (fresh session,
            # worker hasn't completed a round) -- the JSON API keeps returning
            # null there unchanged, for any existing consumer relying on it.
            is_starting = {pair: balances[pair] is None for pair in PAIRS}
            display_balances = {
                pair: balances[pair] if balances[pair] is not None
                else _starting_account(pair, overlay_path(app.config["STATE_DIR"], pair))
                for pair in PAIRS
            }
            return render_template(
                "admin/balances.html", balances=display_balances, is_starting=is_starting,
                lane_balances=lane_balances, algorithm_labels=_algorithm_labels(),
                fee_scopes=_fee_scopes_summary(app),
                starting_cash={pair: TRADING_PROFILES[pair]["cash_initial"] for pair in PAIRS},
                starting_cash_btc=TRADING_PROFILES["btc"]["cash_initial"],
                starting_cash_usdt=TRADING_PROFILES["usdt"]["cash_initial"],
                csrf_token=get_csrf_token(), session_id=_session_id(app),
            )
        return jsonify(dict(balances, lanes=lane_balances))

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

    @app.route("/admin/validation/metrics")
    @login_required
    def admin_validation_metrics():
        """API endpoint for validation metrics.

        Query params:
            - pair: one of the markets (btc, usdt, eth, xaut, trx; default: btc)
            - session: session_id or "all" (default: current active session)
            - track: real or demo (default: demo)
            - risk_free_rate: annual risk-free rate, decimal (default: 0.0)
            - target_return: annual target/minimum acceptable return for
              Sortino, decimal (default: 0.0)
        """
        pair = request.args.get("pair", "btc")
        if pair not in PAIRS:
            return jsonify({"error": "Unknown pair"}), 400

        session_id = request.args.get("session")
        track_type = request.args.get("track", "demo")

        try:
            risk_free_rate = float(request.args.get("risk_free_rate", 0.0))
            target_return = float(request.args.get("target_return", 0.0))
        except (TypeError, ValueError):
            return jsonify({"error": "risk_free_rate and target_return must be numbers"}), 400

        # Load the appropriate CSV
        if track_type == "demo":
            csv_name = os.path.basename(demo_csv_path(TRADING_PROFILES[pair]["output_csv"]))
            csv_path = os.path.join(app.config["RESULTS_DIR"], csv_name)
        else:
            csv_path = TRADING_PROFILES[pair]["output_csv"]
        
        df = load_track(csv_path)
        if df is None or df.empty:
            return jsonify({"error": "No data available"}), 404
        
        # Filter by session if specified
        if session_id and session_id != "all":
            df = filter_session(df, session_id)
            if df.empty:
                return jsonify({"error": "No data for selected session"}), 404
        
        # Run validation
        engine = create_validation_engine(
            risk_free_rate=risk_free_rate, target_return=target_return
        )
        result = engine.validate_from_csv(df)
        
        # Generate report
        report = generate_validation_report(result)
        
        if _wants_html():
            # Return formatted for HTML rendering
            return jsonify({
                "metrics": report["summary"],
                "details": report["details"],
                "charts": report["charts"],
            })
        
        return jsonify(report)


@csrf_protected
def _admin_config_post(app: Flask, pair: str, path):
    wants_html = _wants_html()
    schema = editable_schema(pair, TRADING_PROFILES)
    control_fields = {"csrf_token", "action"}
    unknown_fields = sorted(set(request.form) - set(schema) - control_fields)
    if unknown_fields:
        message = f"unknown field: {unknown_fields[0]}"
        if wants_html:
            return render_template(
                "admin/config.html", **_config_page_context(app, pair, path, error=message),
            ), 400
        return (message, 400)

    if request.form.get("action") == "revert":
        try:
            result = enqueue_and_wait(app, "rollback_config", pair)
        except (OperationInProgress, ResultTimeout) as exc:
            status, message = _control_error(exc)
            if wants_html:
                return render_template(
                    "admin/config.html", **_config_page_context(app, pair, path, error=message),
                ), status
            return (message, status)
        if not result.get("ok"):
            message = _command_failure_message(result)
            if wants_html:
                return render_template(
                    "admin/config.html",
                    **_config_page_context(
                        app, pair, path, error=message, command_error=message,
                    ),
                ), 502
            return jsonify(result), 502
        if wants_html:
            return render_template(
                "admin/config.html",
                **_config_page_context(app, pair, path, success="Previous configuration saved."),
            )
        return jsonify(result)

    if request.form.get("action"):
        message = "unknown configuration action"
        if wants_html:
            return render_template(
                "admin/config.html", **_config_page_context(app, pair, path, error=message),
            ), 400
        return (message, 400)

    try:
        desired = load_desired_config(pair, app.config["STATE_DIR"], TRADING_PROFILES)
    except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as exc:
        if wants_html:
            return _error_page(400, str(exc))
        return (str(exc), 400)

    submitted = {}
    for key, definition in schema.items():
        if key not in request.form:
            continue
        value = _form_value(definition, request.form.getlist(key))
        if value is None:
            message = f"invalid value for {key}"
            if wants_html:
                return render_template(
                    "admin/config.html", **_config_page_context(app, pair, path, error=message),
                ), 400
            return (message, 400)
        submitted[key] = value
    desired.update(submitted)
    try:
        validated = validate_overrides(desired, pair=pair, profiles=TRADING_PROFILES)
    except OverlayValidationError as exc:
        # Rejected before enqueueing -- nothing submitted. Re-render with the
        # attempted (unsaved) values so the admin doesn't lose their edits.
        if wants_html:
            return render_template(
                "admin/config.html",
                **_config_page_context(app, pair, path, error=str(exc), attempted=submitted),
            ), 400
        return (str(exc), 400)

    try:
        result = enqueue_and_wait(app, "apply_config", pair, payload={"overrides": validated})
    except (OperationInProgress, ResultTimeout) as exc:
        status, message = _control_error(exc)
        if wants_html:
            return render_template(
                "admin/config.html", **_config_page_context(app, pair, path, error=message),
            ), status
        return (message, status)
    if not result.get("ok"):
        message = _command_failure_message(result)
        if wants_html:
            return render_template(
                "admin/config.html",
                **_config_page_context(
                    app, pair, path, error=message, attempted=validated,
                    command_error=message,
                ),
            ), 502
        return jsonify(result), 502
    if wants_html:
        return render_template(
            "admin/config.html",
            **_config_page_context(
                app, pair, path, success="Configuration saved.", attempted=validated,
            ),
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
