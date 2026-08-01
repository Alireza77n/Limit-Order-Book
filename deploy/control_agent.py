"""Host-level control agent for the Demo admin surface.

Runs as the same unprivileged user that already runs `docker compose`
(see `rollback.sh`) -- NOT in a container, NOT given a Docker socket by
anything else. It is the *only* component with Docker/file mutation
authority for the Demo stack: it polls `runtime/control/commands/`, matches
each file's `action` against the fixed `HANDLERS` dict below, and writes a
result file. No handler ever builds a shell string or argv from
caller-supplied text -- worker actions are looked up in a hardcoded dict
(`WORKER_ARGV`), and `pair` values are restricted to the closed set
{"btc", "usdt"}.

Not installed/deployed by this session -- see docs/deployment/ADMIN_PAGE.md.
"""

from __future__ import annotations

import fcntl
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
    append_audit,
    submit_command,  # noqa: F401  (re-exported for convenience/tests)
    write_result,
)
from demo_config import (  # noqa: E402
    OverlayValidationError,
    overlay_path,
    rollback_overrides,
    validate_overrides,
    write_overrides,
)
from session import initialize_stack_session  # noqa: E402


AgentResult = namedtuple("AgentResult", ["ok", "detail"])

PAIRS = ("btc", "usdt")
COMPOSE_ARGV_PREFIX = [
    "docker", "compose", "-p", "limit-order-book",
    "-f", "deploy/docker-compose.production.yml",
    "--env-file", "deploy/demo.env",
]
SERVICE_NAME = {"btc": "btc-demo", "usdt": "usdt-demo"}
# Lock file suffix matches session.py/run.py: WorkerLease is created with
# identity f"{cfg.pair}-demo" against lease_path .worker_<pair.lower()>.lock,
# and cfg.pair is "BTCIRT"/"USDTIRT" (see config.TRADING_PROFILES).
LOCK_SUFFIX = {"btc": "btcirt", "usdt": "usdtirt"}

# Fixed argv per (action, pair) -- 6 arrays total, no string interpolation.
WORKER_ARGV: Dict[tuple, List[str]] = {
    (action, pair): COMPOSE_ARGV_PREFIX + [verb, SERVICE_NAME[pair]]
    for action, verb in (("start_worker", "start"), ("stop_worker", "stop"), ("restart_worker", "restart"))
    for pair in PAIRS
}

BACKUP_GLOBS = (
    "paper_*.json", "paper_*_archive.jsonl", "paper_fee_scopes.json",
    "stack_session.json", "*_demo.csv", "*_demo.sqlite3",
)


class AgentPaths:
    def __init__(self, app_root, state_dir, results_dir, backups_dir, control_dir):
        self.app_root = Path(app_root)
        self.state_dir = Path(state_dir)
        self.results_dir = Path(results_dir)
        self.backups_dir = Path(backups_dir)
        self.control_dir = Path(control_dir)


def resolve_worker_argv(action: str, pair: str) -> List[str]:
    """Fixed dict lookup only -- raises KeyError for anything outside
    {start,stop,restart}_worker x {btc,usdt}."""
    key = (action, pair)
    if key not in WORKER_ARGV:
        raise KeyError(f"no fixed argv for action={action!r} pair={pair!r}")
    return list(WORKER_ARGV[key])


def worker_lock_path(paths: AgentPaths, pair: str) -> Path:
    return paths.state_dir / f".worker_{LOCK_SUFFIX[pair]}.lock"


def is_worker_running(paths: AgentPaths, pair: str) -> bool:
    """A running worker holds an exclusive flock on its lease file (see
    session.py's WorkerLease). If we can acquire the lock, nothing is
    holding it -- the worker is not running."""
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


def run_worker_command(paths: AgentPaths, action: str, pair: str, run_argv: Callable = None) -> AgentResult:
    run_argv = run_argv or _default_run_argv
    argv = resolve_worker_argv(action, pair)
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


def create_backup(paths: AgentPaths, dest_name: str) -> List[str]:
    dest_dir = paths.backups_dir / dest_name
    return _snapshot_demo_files(paths, dest_dir)


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
        validate_overrides(overrides)
        write_overrides(overlay_path(paths.state_dir, pair), overrides)
    except OverlayValidationError as exc:
        return AgentResult(False, {"error": str(exc)})
    restart = run_worker_command(paths, "restart_worker", pair, run_argv)
    return AgentResult(restart.ok, {"applied": overrides, "restart": restart.detail})


def handle_rollback_config(paths, pair, payload, run_argv):
    try:
        restored = rollback_overrides(overlay_path(paths.state_dir, pair))
    except OverlayValidationError as exc:
        return AgentResult(False, {"error": str(exc)})
    restart = run_worker_command(paths, "restart_worker", pair, run_argv)
    return AgentResult(restart.ok, {"restored": restored, "restart": restart.detail})


def handle_new_session(paths, pair, payload, run_argv):
    running_before = {p: is_worker_running(paths, p) for p in PAIRS}
    for p, was_running in running_before.items():
        if was_running:
            run_worker_command(paths, "stop_worker", p, run_argv)
    session_id = initialize_stack_session(paths.state_dir / "stack_session.json", force_new=True)
    restarted = [p for p, was_running in running_before.items() if was_running]
    for p in restarted:
        run_worker_command(paths, "start_worker", p, run_argv)
    return AgentResult(True, {"session_id": session_id, "restarted": restarted})


def handle_cancel_orders(paths, pair, payload, run_argv):
    was_running = is_worker_running(paths, pair)
    if not was_running:
        return AgentResult(True, {
            "pair": pair, "was_running": False, "now_running": False,
            "note": "worker already stopped; nothing to cancel",
        })
    stop = run_worker_command(paths, "stop_worker", pair, run_argv)
    start = run_worker_command(paths, "start_worker", pair, run_argv)
    return AgentResult(stop.ok and start.ok, {
        "pair": pair, "was_running": True, "now_running": True,
        "stop": stop.detail, "start": start.detail,
    })


def handle_create_backup(paths, pair, payload, run_argv):
    prefix = (payload or {}).get("prefix", "manual")
    dest_name = f"{prefix}-{_stamp()}"
    files = create_backup(paths, dest_name)
    return AgentResult(True, {"backup": dest_name, "files": files})


def handle_reset_demo(paths, pair, payload, run_argv):
    if (payload or {}).get("confirm") != "RESET":
        return AgentResult(False, {"error": "confirmation required"})
    running_before = {p: is_worker_running(paths, p) for p in PAIRS}
    for p, was_running in running_before.items():
        if was_running:
            run_worker_command(paths, "stop_worker", p, run_argv)

    backup_name = f"demo-reset-{_stamp()}"
    backup_files = create_backup(paths, backup_name)

    removed = []
    for source_dir in (paths.state_dir, paths.results_dir):
        if not source_dir.exists():
            continue
        for pattern in ("*_demo.csv", "*_demo.sqlite3", "paper_*.json", "paper_*_archive.jsonl", "paper_fee_scopes.json"):
            for target in source_dir.glob(pattern):
                if target.is_file():
                    target.unlink()
                    removed.append(target.name)

    session_id = initialize_stack_session(paths.state_dir / "stack_session.json", force_new=True)
    restarted = [p for p, was_running in running_before.items() if was_running]
    for p in restarted:
        run_worker_command(paths, "start_worker", p, run_argv)

    return AgentResult(True, {
        "backup": backup_name, "backup_files": backup_files,
        "removed": removed, "session_id": session_id, "restarted": restarted,
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

    running_before = {p: is_worker_running(paths, p) for p in PAIRS}
    for p, was_running in running_before.items():
        if was_running:
            run_worker_command(paths, "stop_worker", p, run_argv)

    pre_restore_name = f"pre-restore-{_stamp()}"
    create_backup(paths, pre_restore_name)

    restored = []
    for source_file in sorted(backup_dir.iterdir()):
        dest_dir = paths.results_dir if source_file.suffix in (".csv", ".sqlite3") else paths.state_dir
        _atomic_copy(source_file, dest_dir / source_file.name)
        restored.append(source_file.name)

    restarted = [p for p, was_running in running_before.items() if was_running]
    for p in restarted:
        run_worker_command(paths, "start_worker", p, run_argv)

    return AgentResult(True, {
        "restored_from": backup_dir.name, "pre_restore_backup": pre_restore_name,
        "files": restored, "restarted": restarted,
    })


def handle_recover_demo(paths, pair, payload, run_argv):
    stale_locks = []
    for p in PAIRS:
        lock_path = worker_lock_path(paths, p)
        if lock_path.exists() and not is_worker_running(paths, p):
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
}
assert set(HANDLERS) == set(ACTIONS)  # every queue action has exactly one handler


def process_command(paths: AgentPaths, command: dict, run_argv: Callable = None) -> AgentResult:
    action = command.get("action")
    if action not in HANDLERS:
        return AgentResult(False, {"error": f"no handler for action: {action}"})
    handler = HANDLERS[action]
    try:
        return handler(paths, command.get("pair"), command.get("payload"), run_argv)
    except Exception as exc:  # noqa: BLE001 -- always report, never crash the poll loop
        return AgentResult(False, {"error": f"{type(exc).__name__}: {exc}"})


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
        append_audit(paths.control_dir, {
            "command_id": command["command_id"], "action": command.get("action"),
            "pair": command.get("pair"), "requested_by": command.get("requested_by"),
            "ok": result.ok,
        })
        command_file.unlink(missing_ok=True)
        processed += 1
    return processed


def main() -> None:
    app_root = Path(__file__).resolve().parent.parent
    paths = AgentPaths(
        app_root=app_root,
        state_dir=app_root / "runtime/state",
        results_dir=app_root / "runtime/results",
        backups_dir=app_root / "runtime/backups",
        control_dir=app_root / "runtime/control",
    )
    while True:
        poll_once(paths)
        time.sleep(2)


if __name__ == "__main__":
    main()
