"""Host-level control agent for the Demo admin surface.

Runs as the same unprivileged user that already runs `docker compose`
(see `rollback.sh`) -- NOT in a container, NOT given a Docker socket by
anything else. It is the *only* component with Docker/file mutation
authority for the Demo stack: it polls `runtime/control/commands/`, matches
each file's `action` against the fixed `HANDLERS` dict below, and writes a
result file. No handler ever builds a shell string or argv from
caller-supplied text -- worker actions are looked up in a hardcoded dict
(`WORKER_ARGV`), and `pair` values are restricted to the closed set of
markets in config.TRADING_PROFILES (btc, usdt, eth, xaut, trx).

On the server it runs as deploy/systemd/lob-control-agent.service; in the
local stack it is docker-compose.yml's `control-agent` service.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

# market_maker/ holds demo_config.py, session.py, control_queue.py -- add it
# to sys.path so this host script (which lives in deploy/, outside the
# package) can import them without duplicating logic.
_MARKET_MAKER_DIR = str(Path(__file__).resolve().parent.parent / "market_maker")
if _MARKET_MAKER_DIR not in sys.path:
    sys.path.insert(0, _MARKET_MAKER_DIR)

from control_queue import (  # noqa: E402
    ACTIONS,
    submit_command,  # noqa: F401  (re-exported for convenience/tests)
    write_result,
)
from config import TRADING_PROFILES, demo_csv_path  # noqa: E402
from result_store import DemoResultStore  # noqa: E402
from no_quote import purge_session  # noqa: E402
from demo_config import (  # noqa: E402
    OverlayLoadError,
    OverlayValidationError,
    desired_digest,
    editable_schema,
    load_desired_config,
    overlay_path,
    rollback_overrides,
    validate_overrides,
    write_overrides,
)
from session import initialize_stack_session  # noqa: E402


AgentResult = namedtuple("AgentResult", ["ok", "detail"])


class WorkerStatusError(RuntimeError):
    """Worker liveness could not be established authoritatively."""


PAIRS = tuple(TRADING_PROFILES)          # every market in config.TRADING_PROFILES
COMPOSE_ARGV_PREFIX = [
    "docker", "compose", "-p", "limit-order-book",
    "-f", "deploy/docker-compose.production.yml",
    "--env-file", "deploy/demo.env",
]
LOCAL_COMPOSE_ARGV_PREFIX = [
    "docker", "compose", "-p", "limit-order-book",
    "-f", "docker-compose.yml",
]
SERVICE_NAME = {pair: f"{pair}-demo" for pair in PAIRS}
# Lock file suffix matches session.py/run.py: WorkerLease is created with
# identity f"{cfg.pair}-demo" against lease_path .worker_<pair.lower()>.lock,
# and cfg.pair is "BTCIRT"/"USDTIRT" (see config.TRADING_PROFILES).
LOCK_SUFFIX = {pair: str(TRADING_PROFILES[pair]["pair"]).lower() for pair in PAIRS}

# Fixed argv per (action, pair) -- 6 arrays total, no string interpolation.
WORKER_COMMAND_SUFFIX: Dict[str, List[str]] = {
    "start_worker": ["up", "-d", "--no-deps", "--no-recreate"],
    "stop_worker": ["stop"],
    "restart_worker": ["restart"],
}
WORKER_ARGV: Dict[tuple, List[str]] = {
    (action, pair): COMPOSE_ARGV_PREFIX + suffix + [SERVICE_NAME[pair]]
    for action, suffix in WORKER_COMMAND_SUFFIX.items()
    for pair in PAIRS
}
LOCAL_WORKER_ARGV: Dict[tuple, List[str]] = {
    (action, pair): LOCAL_COMPOSE_ARGV_PREFIX + suffix + [SERVICE_NAME[pair]]
    for action, suffix in WORKER_COMMAND_SUFFIX.items()
    for pair in PAIRS
}
LOCAL_WORKER_STATUS_ARGV: Dict[str, List[str]] = {
    pair: LOCAL_COMPOSE_ARGV_PREFIX
    + ["ps", "--status", "running", "--services", SERVICE_NAME[pair]]
    for pair in PAIRS
}

BACKUP_GLOBS = (
    "paper_*.json", "paper_*_archive.jsonl", "paper_fee_scopes.json",
    "stack_session.json", "*_demo.csv", "*_demo.sqlite3",
    # demo lanes (one algorithm each): <pair>_track_demo_<algo>.*
    "*_demo_algo*.csv", "*_demo_algo*.sqlite3",
)

WORKER_STATUS_QUERY_TIMEOUT_SECONDS = 2.0
WORKER_VERIFY_TIMEOUT_SECONDS = 5.0
WORKER_VERIFY_INTERVAL_SECONDS = 0.25
_monotonic = time.monotonic
_sleep = time.sleep


class AgentPaths:
    def __init__(self, app_root, state_dir, results_dir, backups_dir, control_dir, mode="production"):
        self.app_root = Path(app_root)
        self.state_dir = Path(state_dir)
        self.results_dir = Path(results_dir)
        self.backups_dir = Path(backups_dir)
        self.control_dir = Path(control_dir)
        self.mode = mode


def build_agent_paths(mode: str, app_root) -> AgentPaths:
    app_root = Path(app_root)
    if mode == "production":
        return AgentPaths(
            app_root=app_root,
            state_dir=app_root / "runtime/state",
            results_dir=app_root / "runtime/results",
            backups_dir=app_root / "runtime/backups",
            control_dir=app_root / "runtime/control",
            mode=mode,
        )
    if mode == "local":
        local_results = app_root / "market_maker/results"
        return AgentPaths(
            app_root=app_root,
            state_dir=local_results,
            results_dir=local_results,
            backups_dir=local_results / "backups",
            control_dir=local_results / "control",
            mode=mode,
        )
    raise ValueError(f"unknown control-agent mode: {mode!r}")


def resolve_worker_argv(action: str, pair: str, mode: str = "production") -> List[str]:
    """Fixed dict lookup only -- raises KeyError for anything outside
    {start,stop,restart}_worker x {btc,usdt}."""
    key = (action, pair)
    if mode == "production":
        if key not in WORKER_ARGV:
            raise KeyError(f"no fixed argv for action={action!r} pair={pair!r}")
        return list(WORKER_ARGV[key])
    if mode == "local":
        if key not in LOCAL_WORKER_ARGV:
            raise KeyError(f"no fixed argv for action={action!r} pair={pair!r}")
        return list(LOCAL_WORKER_ARGV[key])
    raise KeyError(f"no fixed argv for mode={mode!r}")


def worker_lock_path(paths: AgentPaths, pair: str) -> Path:
    return paths.state_dir / f".worker_{LOCK_SUFFIX[pair]}.lock"


def is_worker_running(
    paths: AgentPaths, pair: str, run_argv: Callable = None
) -> bool:
    """A running worker holds an exclusive flock on its lease file (see
    session.py's WorkerLease). If we can acquire the lock, nothing is
    holding it -- the worker is not running. Docker Desktop does not
    propagate container-held flocks to the macOS host, so local mode uses
    Compose's fixed service-status query instead."""
    if paths.mode == "local":
        run_argv = run_argv or _default_run_argv
        argv = list(LOCAL_WORKER_STATUS_ARGV[pair])
        try:
            completed = run_argv(
                argv, cwd=str(paths.app_root), capture_output=True, text=True,
                timeout=WORKER_STATUS_QUERY_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed at authority boundary
            raise WorkerStatusError(
                f"{pair} worker status query failed: {type(exc).__name__}: {exc}"
            ) from exc
        returncode = getattr(completed, "returncode", None)
        if returncode != 0:
            raise WorkerStatusError(
                f"{pair} worker status query failed with exit code {returncode}"
            )
        running_services = set((getattr(completed, "stdout", "") or "").splitlines())
        return SERVICE_NAME[pair] in running_services
    lock_path = worker_lock_path(paths, pair)
    if not lock_path.exists():
        return False
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return False


def worker_statuses(paths: AgentPaths, pairs, run_argv: Callable = None) -> dict:
    """Resolve every requested status before a caller mutates shared state."""
    return {pair: is_worker_running(paths, pair, run_argv) for pair in pairs}


def liveness_failure(exc: WorkerStatusError) -> AgentResult:
    return AgentResult(False, {"phase": "liveness", "error": str(exc)})


def _wait_for_worker_running(paths, pair, run_argv):
    """Poll fixed worker liveness until startup is visible or time expires."""
    deadline = _monotonic() + WORKER_VERIFY_TIMEOUT_SECONDS
    observed = None
    last_error = None
    while True:
        try:
            observed = is_worker_running(paths, pair, run_argv)
            if observed:
                return True, None
        except WorkerStatusError as exc:
            observed = None
            last_error = str(exc)
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return observed, last_error
        _sleep(min(WORKER_VERIFY_INTERVAL_SECONDS, remaining))


def _lifecycle_command(paths, action, pair, run_argv) -> AgentResult:
    try:
        return run_worker_command(paths, action, pair, run_argv)
    except Exception as exc:  # noqa: BLE001 -- preserve lifecycle context
        return AgentResult(False, {"error": f"{type(exc).__name__}: {exc}"})


def _stop_running_workers(paths, running_before, run_argv):
    stopped = []
    for pair, was_running in running_before.items():
        if not was_running:
            continue
        result = _lifecycle_command(paths, "stop_worker", pair, run_argv)
        if not result.ok:
            return stopped, AgentResult(False, {
                "phase": "stop",
                "pair": pair,
                "action": "stop_worker",
                "failure": result.detail,
                "stopped": stopped,
            })
        stopped.append(pair)
    return stopped, None


def _start_stopped_workers(paths, stopped, run_argv):
    started = []
    failures = []
    for pair in stopped:
        result = _lifecycle_command(paths, "start_worker", pair, run_argv)
        if result.ok:
            started.append(pair)
        else:
            failures.append({
                "pair": pair,
                "action": "start_worker",
                "detail": result.detail,
            })
    return started, failures


def _start_failure(stopped, started, failures, operation_detail):
    first = failures[0]
    return AgentResult(False, {
        **operation_detail,
        "phase": "start",
        "pair": first["pair"],
        "action": first["action"],
        "stopped": stopped,
        "started": started,
        "failures": failures,
    })


def run_worker_command(paths: AgentPaths, action: str, pair: str, run_argv: Callable = None) -> AgentResult:
    run_argv = run_argv or _default_run_argv
    argv = resolve_worker_argv(action, pair, mode=paths.mode)
    completed = run_argv(argv, cwd=str(paths.app_root), capture_output=True, text=True)
    ok = getattr(completed, "returncode", 1) == 0
    return AgentResult(ok, {"argv": argv, "returncode": getattr(completed, "returncode", None)})


def _default_run_argv(argv, **kwargs):
    import subprocess
    return subprocess.run(argv, **kwargs)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Consistent read-only snapshot via sqlite3's own backup() API -- safe
    against a concurrently writing worker in WAL mode, no file-copy race."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    dest_conn = sqlite3.connect(str(tmp))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()
    os.replace(tmp, dst)


def _snapshot_demo_files(paths: AgentPaths, dest_dir: Path) -> List[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = set()
    for source_dir in (paths.state_dir, paths.results_dir):
        if not source_dir.exists():
            continue
        for pattern in BACKUP_GLOBS:
            for source_file in sorted(source_dir.glob(pattern)):
                if not source_file.is_file():
                    continue
                target = dest_dir / source_file.name
                if source_file.suffix == ".sqlite3":
                    _sqlite_snapshot(source_file, target)
                else:
                    _atomic_copy(source_file, target)
                copied.add(source_file.name)
    return sorted(copied)


def _readable_by_the_stack(dest_dir: Path, reference: Path) -> None:
    """Backups are read by lob-admin (the stack's uid, read-only mount) and
    this agent runs as root: give the snapshot the runtime directory's
    owner and world-readable modes. The worker writes its CSV as 0600, and
    a root-owned copy of that was unreadable for /admin/export."""
    try:
        owner = reference.stat()
    except OSError:
        owner = None
    for entry in [dest_dir, *dest_dir.iterdir()]:
        try:
            entry.chmod(0o755 if entry.is_dir() else 0o644)
            if owner is not None and hasattr(os, "chown") and os.geteuid() == 0:
                os.chown(entry, owner.st_uid, owner.st_gid)
        except OSError:
            continue


def create_backup(paths: AgentPaths, dest_name: str) -> List[str]:
    dest_dir = paths.backups_dir / dest_name
    files = _snapshot_demo_files(paths, dest_dir)
    _readable_by_the_stack(dest_dir, paths.state_dir)
    return files


# --- handlers -------------------------------------------------------------
# Uniform signature: handle_<action>(paths, pair, payload, run_argv) -> AgentResult
# `pair`/`payload` are None where the action doesn't use them.

def handle_start_worker(paths, pair, payload, run_argv):
    return run_worker_command(paths, "start_worker", pair, run_argv)


def handle_stop_worker(paths, pair, payload, run_argv):
    return run_worker_command(paths, "stop_worker", pair, run_argv)


def handle_restart_worker(paths, pair, payload, run_argv):
    return run_worker_command(paths, "restart_worker", pair, run_argv)


def handle_apply_config(paths, pair, payload, run_argv):
    overrides = (payload or {}).get("overrides", {})
    try:
        # Re-validate independently -- never trust lob-admin's validation alone.
        validated = validate_overrides(
            overrides, pair=pair, profiles=TRADING_PROFILES
        )
        persisted = write_overrides(
            overlay_path(paths.state_dir, pair),
            validated,
            pair=pair,
            profiles=TRADING_PROFILES,
        )
        desired = load_desired_config(pair, paths.state_dir, TRADING_PROFILES)
    except (OverlayLoadError, OverlayValidationError, OSError) as exc:
        return AgentResult(False, {"error": str(exc)})
    schema = editable_schema(pair, TRADING_PROFILES)
    return AgentResult(True, {
        "persisted": persisted,
        "desired_digest": desired_digest(desired),
        "modes": {field: schema[field].mode.value for field in persisted},
    })


def handle_rollback_config(paths, pair, payload, run_argv):
    try:
        restored = rollback_overrides(
            overlay_path(paths.state_dir, pair),
            pair=pair,
            profiles=TRADING_PROFILES,
        )
    except (OverlayLoadError, OverlayValidationError, OSError) as exc:
        return AgentResult(False, {"error": str(exc)})
    schema = editable_schema(pair, TRADING_PROFILES)
    desired = {field: TRADING_PROFILES[pair][field] for field in schema}
    desired.update(restored)
    desired = validate_overrides(
        desired, pair=pair, profiles=TRADING_PROFILES,
    )
    modes = {field: schema[field].mode.value for field in restored}
    return AgentResult(True, {
        "restored": restored,
        "desired_digest": desired_digest(desired),
        "modes": modes,
        "status": "saved",
    })


def handle_new_session(paths, pair, payload, run_argv):
    try:
        running_before = worker_statuses(paths, PAIRS, run_argv)
    except WorkerStatusError as exc:
        return liveness_failure(exc)
    stopped = []
    for p, was_running in running_before.items():
        if was_running:
            try:
                stop = run_worker_command(paths, "stop_worker", p, run_argv)
            except Exception as exc:  # noqa: BLE001 -- preserve lifecycle context
                stop = AgentResult(False, {"error": f"{type(exc).__name__}: {exc}"})
            if not stop.ok:
                return AgentResult(False, {
                    "phase": "stop",
                    "pair": p,
                    "action": "stop_worker",
                    "failure": stop.detail,
                    "stopped": stopped,
                })
            stopped.append(p)
    session_id = initialize_stack_session(paths.state_dir / "stack_session.json", force_new=True)
    restarted = [p for p, was_running in running_before.items() if was_running]
    started = []
    failures = []
    for p in restarted:
        try:
            start = run_worker_command(paths, "start_worker", p, run_argv)
        except Exception as exc:  # noqa: BLE001 -- preserve lifecycle context
            start = AgentResult(False, {"error": f"{type(exc).__name__}: {exc}"})
        if start.ok:
            started.append(p)
        else:
            failures.append({
                "pair": p,
                "action": "start_worker",
                "detail": start.detail,
            })
    if failures:
        first_failure = failures[0]
        return AgentResult(False, {
            "phase": "start",
            "pair": first_failure["pair"],
            "action": first_failure["action"],
            "session_id": session_id,
            "started": started,
            "failures": failures,
        })
    return AgentResult(True, {"session_id": session_id, "restarted": restarted})


def handle_cancel_orders(paths, pair, payload, run_argv):
    try:
        was_running = worker_statuses(paths, (pair,), run_argv)[pair]
    except WorkerStatusError as exc:
        return liveness_failure(exc)
    if not was_running:
        return AgentResult(True, {
            "pair": pair, "was_running": False, "now_running": False,
            "note": "worker already stopped; nothing to cancel",
        })
    stop = _lifecycle_command(paths, "stop_worker", pair, run_argv)
    if not stop.ok:
        try:
            now_running = worker_statuses(paths, (pair,), run_argv)[pair]
            status_error = None
        except WorkerStatusError as exc:
            now_running = None
            status_error = str(exc)
        detail = {
            "phase": "stop", "pair": pair, "was_running": True,
            "now_running": now_running, "stop": stop.detail,
        }
        if status_error is not None:
            detail["status_error"] = status_error
        return AgentResult(False, detail)

    start = _lifecycle_command(paths, "start_worker", pair, run_argv)
    if start.ok:
        now_running, status_error = _wait_for_worker_running(
            paths, pair, run_argv,
        )
    else:
        try:
            now_running = is_worker_running(paths, pair, run_argv)
            status_error = None
        except WorkerStatusError as exc:
            now_running = None
            status_error = str(exc)
    ok = start.ok and now_running is True
    detail = {
        "pair": pair, "was_running": True, "now_running": now_running,
        "stop": stop.detail, "start": start.detail,
    }
    if not ok:
        detail["phase"] = "start" if not start.ok else "verify"
    if status_error is not None:
        detail["status_error"] = status_error
    return AgentResult(ok, detail)


def handle_create_backup(paths, pair, payload, run_argv):
    prefix = (payload or {}).get("prefix", "manual")
    dest_name = f"{prefix}-{_stamp()}"
    files = create_backup(paths, dest_name)
    return AgentResult(True, {"backup": dest_name, "files": files})


def handle_reset_demo(paths, pair, payload, run_argv):
    if (payload or {}).get("confirm") != "RESET":
        return AgentResult(False, {"error": "confirmation required"})
    try:
        running_before = worker_statuses(paths, PAIRS, run_argv)
    except WorkerStatusError as exc:
        return liveness_failure(exc)
    stopped, stop_failure = _stop_running_workers(
        paths, running_before, run_argv
    )
    if stop_failure is not None:
        return stop_failure

    backup_name = f"demo-reset-{_stamp()}"
    backup_files = create_backup(paths, backup_name)

    removed = []
    for source_dir in (paths.state_dir, paths.results_dir):
        if not source_dir.exists():
            continue
        for pattern in ("*_demo.csv", "*_demo.sqlite3", "*_demo_algo*.csv", "*_demo_algo*.sqlite3",
                        "paper_*.json", "paper_*_archive.jsonl", "paper_fee_scopes.json"):
            for target in source_dir.glob(pattern):
                if target.is_file():
                    target.unlink()
                    removed.append(target.name)

    session_id = initialize_stack_session(paths.state_dir / "stack_session.json", force_new=True)
    operation_detail = {
        "backup": backup_name, "backup_files": backup_files,
        "removed": removed, "session_id": session_id,
    }
    started, failures = _start_stopped_workers(paths, stopped, run_argv)
    if failures:
        return _start_failure(stopped, started, failures, operation_detail)
    return AgentResult(True, {**operation_detail, "restarted": started})


def _valid_session_id(session_id: Optional[str]) -> bool:
    """session_id gets embedded directly into filenames (paper_<id>_<pair>.json)
    -- reject anything that could escape the results/state directory. Mirrors
    _safe_backup_dir's approach for the same class of risk."""
    if not session_id or not isinstance(session_id, str):
        return False
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        return False
    return session_id == session_id.strip() and bool(session_id)


def _active_session_id(paths: AgentPaths) -> Optional[str]:
    session_path = paths.state_dir / "stack_session.json"
    if not session_path.exists():
        return None
    try:
        document = json.loads(session_path.read_text())
    except (OSError, ValueError):
        return None
    value = document.get("session_id") if isinstance(document, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _delete_session_ledger_files(paths: AgentPaths, session_id: str) -> List[str]:
    removed = []
    for source_dir in (paths.state_dir, paths.results_dir):
        if not source_dir.exists():
            continue
        for pattern in (f"paper_{session_id}_*.json", f"paper_{session_id}_*_archive.jsonl"):
            for target in source_dir.glob(pattern):
                if target.is_file():
                    target.unlink()
                    removed.append(target.name)
    return removed


def _delete_session_rounds(paths: AgentPaths, session_id: str) -> Dict[str, int]:
    """Remove one session's rows from each pair's round history + no-quote
    state, then re-export the CSV from what remains. The CSV is a bounded
    export of the SQLite `rounds` table (DemoResultStore.export_bounded_csv),
    never edited directly -- see result_store.py."""
    removed_rounds: Dict[str, int] = {}
    for pair in PAIRS:
        csv_name = os.path.basename(demo_csv_path(TRADING_PROFILES[pair]["output_csv"]))
        stem = os.path.splitext(csv_name)[0]
        # the pair's single-worker store (history) and its per-algorithm lane stores
        stores = [paths.results_dir / f"{stem}.sqlite3"] + sorted(paths.results_dir.glob(f"{stem}_algo*.sqlite3"))
        for db_path in stores:
            if not db_path.exists():
                continue
            try:
                store = DemoResultStore.open_existing(
                    db_path, csv_export_path=db_path.with_suffix(".csv"),
                )
            except (FileNotFoundError, ValueError):
                continue
            round_count = store.delete_session(session_id)
            purge_session(db_path, session_id)
            key = pair if db_path.name == f"{stem}.sqlite3" else f"{pair}:{db_path.stem.rsplit('_', 1)[1]}"
            removed_rounds[key] = round_count
            if round_count:
                store.export_bounded_csv()
    return removed_rounds


def _delete_session_fee_contribution(paths: AgentPaths, session_id: str) -> Dict[str, float]:
    """all_time is a running sum of every session's per-symbol contribution
    (paper.py's _record_shared_fee adds the same fee_quote to both `sessions`
    and `all_time`), so subtracting this session's recorded share is an exact
    reversal, not an approximation."""
    fee_path = paths.state_dir / "paper_fee_scopes.json"
    if not fee_path.exists():
        return {}
    try:
        document = json.loads(fee_path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    sessions = document.get("sessions") or {}
    session_fees = sessions.pop(session_id, None)
    if not session_fees:
        return {}
    all_time = document.setdefault("all_time", {})
    for symbol, amount in session_fees.items():
        all_time[symbol] = max(0.0, float(all_time.get(symbol, 0.0)) - float(amount))
    watermarks = document.get("order_high_watermarks") or {}
    prefix = f"{session_id}:"
    for key in [k for k in watermarks if k.startswith(prefix)]:
        del watermarks[key]
    _atomic_write_json(fee_path, document)
    return {symbol: float(amount) for symbol, amount in session_fees.items()}


def _atomic_write_json(path: Path, document: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(document, sort_keys=True, indent=2))
    os.replace(tmp, path)


def handle_delete_session(paths, pair, payload, run_argv):
    payload = payload or {}
    if payload.get("confirm") != "DELETE":
        return AgentResult(False, {"error": "confirmation required"})
    session_id = payload.get("session_id")
    if not _valid_session_id(session_id):
        return AgentResult(False, {"error": "invalid or missing session_id"})
    active = _active_session_id(paths)
    if session_id == active:
        return AgentResult(False, {
            "error": "cannot delete the active session; start a new session first",
            "active_session_id": active,
        })

    backup_name = f"delete-session-{_stamp()}"
    backup_files = create_backup(paths, backup_name)

    removed_ledger_files = _delete_session_ledger_files(paths, session_id)
    removed_rounds = _delete_session_rounds(paths, session_id)
    removed_fees = _delete_session_fee_contribution(paths, session_id)

    found_anything = bool(removed_ledger_files) or any(removed_rounds.values()) or bool(removed_fees)
    if not found_anything:
        return AgentResult(False, {
            "error": "session not found",
            "session_id": session_id,
            "backup": backup_name,
        })

    return AgentResult(True, {
        "session_id": session_id,
        "backup": backup_name,
        "backup_files": backup_files,
        "removed_ledger_files": removed_ledger_files,
        "removed_rounds": removed_rounds,
        "removed_fee_contribution": removed_fees,
    })


def _safe_backup_dir(paths: AgentPaths, name: Optional[str]) -> Optional[Path]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    candidate = paths.backups_dir / name
    try:
        resolved = candidate.resolve()
        root = paths.backups_dir.resolve()
    except OSError:
        return None
    if root != resolved.parent or not resolved.is_dir():
        return None
    for entry in resolved.iterdir():
        if not entry.is_file():
            return None
    return resolved


def handle_restore_backup(paths, pair, payload, run_argv):
    payload = payload or {}
    if payload.get("confirm") != "RESTORE":
        return AgentResult(False, {"error": "confirmation required"})
    backup_dir = _safe_backup_dir(paths, payload.get("backup_name"))
    if backup_dir is None:
        return AgentResult(False, {"error": "invalid or unknown backup name"})

    try:
        running_before = worker_statuses(paths, PAIRS, run_argv)
    except WorkerStatusError as exc:
        return liveness_failure(exc)
    stopped, stop_failure = _stop_running_workers(
        paths, running_before, run_argv
    )
    if stop_failure is not None:
        return stop_failure

    pre_restore_name = f"pre-restore-{_stamp()}"
    create_backup(paths, pre_restore_name)

    restored = []
    for source_file in sorted(backup_dir.iterdir()):
        dest_dir = paths.results_dir if source_file.suffix in (".csv", ".sqlite3") else paths.state_dir
        _atomic_copy(source_file, dest_dir / source_file.name)
        restored.append(source_file.name)

    operation_detail = {
        "restored_from": backup_dir.name, "pre_restore_backup": pre_restore_name,
        "files": restored,
    }
    started, failures = _start_stopped_workers(paths, stopped, run_argv)
    if failures:
        return _start_failure(stopped, started, failures, operation_detail)
    return AgentResult(True, {**operation_detail, "restarted": started})


def handle_recover_demo(paths, pair, payload, run_argv):
    try:
        running = worker_statuses(paths, PAIRS, run_argv)
    except WorkerStatusError as exc:
        return liveness_failure(exc)
    stale_locks = []
    for p in PAIRS:
        lock_path = worker_lock_path(paths, p)
        if lock_path.exists() and not running[p]:
            lock_path.unlink()
            stale_locks.append(p)

    session_mismatches = []
    session_path = paths.state_dir / "stack_session.json"
    session_id = None
    if session_path.exists():
        try:
            import json as _json
            session_id = _json.loads(session_path.read_text()).get("session_id")
        except (OSError, ValueError):
            session_id = None
    if session_id:
        for ledger_path in sorted(paths.state_dir.glob("paper_*.json")):
            try:
                import json as _json
                document = _json.loads(ledger_path.read_text())
            except (OSError, ValueError):
                continue  # reported below as corrupt, not here
            ledger_session = document.get("session_id")
            if ledger_session and ledger_session != session_id:
                session_mismatches.append(ledger_path.name)

    corrupt_files = []
    for ledger_path in sorted(paths.state_dir.glob("paper_*.json")):
        try:
            import json as _json
            _json.loads(ledger_path.read_text())
        except (OSError, ValueError):
            corrupt_files.append(ledger_path.name)

    needs_reset = bool(session_mismatches or corrupt_files)
    return AgentResult(True, {
        "cleared_stale_locks": stale_locks,
        "session_mismatches": session_mismatches,
        "corrupt_files": corrupt_files,
        "confirmation_required_for_reset": needs_reset,
    })


HANDLERS: Dict[str, Callable] = {
    "start_worker": handle_start_worker,
    "stop_worker": handle_stop_worker,
    "restart_worker": handle_restart_worker,
    "apply_config": handle_apply_config,
    "rollback_config": handle_rollback_config,
    "new_session": handle_new_session,
    "cancel_orders": handle_cancel_orders,
    "create_backup": handle_create_backup,
    "reset_demo": handle_reset_demo,
    "restore_backup": handle_restore_backup,
    "recover_demo": handle_recover_demo,
    "delete_session": handle_delete_session,
}
assert set(HANDLERS) == set(ACTIONS)  # every queue action has exactly one handler


# Handlers that write stack files. This agent runs as root while the workers,
# the dashboard and the admin panel run as the stack user, so anything it
# creates or rewrites must be handed back to that user afterwards (a
# root-owned 0600 CSV left the workers crash-looping on 2026-10-15).
WRITING_ACTIONS = frozenset({
    "apply_config", "rollback_config", "new_session", "delete_session",
    "create_backup", "reset_demo", "restore_backup", "recover_demo",
})


def _restore_stack_ownership(paths: AgentPaths) -> List[str]:
    """Give every file under the state, results and backups directories the
    directory's owner and a readable mode. No-op unless running as root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return []
    changed: List[str] = []
    for directory in (paths.state_dir, paths.results_dir, paths.backups_dir):
        try:
            owner = _stat_entry(directory)
        except OSError:
            continue
        for entry in _walk_files(directory):
            try:
                info = _stat_entry(entry)
                if info.st_uid != owner.st_uid or info.st_gid != owner.st_gid:
                    os.chown(entry, owner.st_uid, owner.st_gid)
                    os.chmod(entry, 0o755 if entry.is_dir() else 0o644)
                    changed.append(str(entry))
            except OSError:
                continue
    return changed


def _stat_entry(path: Path):
    return path.stat()


def _walk_files(directory: Path):
    for entry in directory.iterdir():
        yield entry
        if entry.is_dir():
            yield from _walk_files(entry)


def process_command(paths: AgentPaths, command: dict, run_argv: Callable = None) -> AgentResult:
    action = command.get("action")
    if action not in HANDLERS:
        return AgentResult(False, {"error": f"no handler for action: {action}"})
    handler = HANDLERS[action]
    try:
        return handler(paths, command.get("pair"), command.get("payload"), run_argv)
    except Exception as exc:  # noqa: BLE001 -- always report, never crash the poll loop
        return AgentResult(False, {"error": f"{type(exc).__name__}: {exc}"})
    finally:
        if action in WRITING_ACTIONS:
            _restore_stack_ownership(paths)


def poll_once(paths: AgentPaths, run_argv: Callable = None) -> int:
    commands_dir = paths.control_dir / "commands"
    if not commands_dir.exists():
        return 0
    processed = 0
    for command_file in sorted(commands_dir.glob("*.json")):
        import json as _json
        try:
            command = _json.loads(command_file.read_text())
        except (OSError, ValueError):
            command_file.unlink(missing_ok=True)
            continue
        result = process_command(paths, command, run_argv)
        write_result(paths.control_dir, command["command_id"], result.ok, result.detail)
        command_file.unlink(missing_ok=True)
        processed += 1
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Demo host control agent")
    parser.add_argument("--mode", choices=("production", "local"), default="production")
    args = parser.parse_args()
    app_root = Path(__file__).resolve().parent.parent
    paths = build_agent_paths(args.mode, app_root)
    while True:
        poll_once(paths)
        time.sleep(2)


if __name__ == "__main__":
    main()
