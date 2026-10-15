"""Command-queue primitives shared by `admin_app.py` (producer) and
`deploy/control_agent.py` (consumer). Files only -- no sockets, no shell.

Layout under `<control_dir>`:
  commands/<command_id>.json  -- {"command_id","action","pair"?,"requested_by","requested_at","payload"?}
  results/<command_id>.json   -- {"command_id","ok","detail","finished_at"}
  audit.log                   -- append-only JSONL, one line per completed action
  control.lock                -- flock held for one full management round-trip

Both producer and consumer independently validate `action`/`pair` against
the same closed enums -- neither side trusts the other's file shape blindly.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

PathLike = Union[str, os.PathLike]

FORWARD_ACTIONS = frozenset({
    "start_forward_worker", "stop_forward_worker", "apply_forward_config",
    "read_wallet_balances", "delete_forward_session",
})
# Forward actions that address the whole live history, not one pair.
FORWARD_STACK_ACTIONS = frozenset({"delete_forward_session"})

ACTIONS = frozenset({
    "start_worker", "stop_worker", "restart_worker",
    "apply_config", "rollback_config",
    "new_session", "cancel_orders",
    "create_backup", "reset_demo", "restore_backup", "recover_demo",
    "delete_session",
})

# Actions that operate on one pair; the rest are stack-wide (pair must be None).
PAIR_ACTIONS = (frozenset({
    "start_worker", "stop_worker", "restart_worker",
    "apply_config", "rollback_config", "cancel_orders",
}) | FORWARD_ACTIONS) - FORWARD_STACK_ACTIONS
PAIRS = frozenset({"btc", "usdt"})


class QueueValidationError(ValueError):
    pass


class OperationInProgress(RuntimeError):
    pass


class ResultTimeout(RuntimeError):
    pass


def _dirs(control_dir: PathLike):
    root = Path(control_dir)
    commands_dir = root / "commands"
    results_dir = root / "results"
    commands_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    return root, commands_dir, results_dir


def _validate_action_pair(action: str, pair: Optional[str]) -> None:
    if action not in ACTIONS and action not in FORWARD_ACTIONS:
        raise QueueValidationError(f"unknown action: {action}")
    if action in PAIR_ACTIONS:
        if pair not in PAIRS:
            raise QueueValidationError(f"action {action} requires pair in {sorted(PAIRS)}")
    elif pair is not None:
        raise QueueValidationError(f"action {action} does not take a pair")


def _atomic_write_json(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w") as tmp_file:
        json.dump(document, tmp_file, sort_keys=True, indent=2)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, path)


def submit_command(
    control_dir: PathLike,
    action: str,
    pair: Optional[str] = None,
    *,
    requested_by: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Validate and enqueue a command file. Raises QueueValidationError
    (fail-closed) for anything outside the fixed action/pair enums."""
    _validate_action_pair(action, pair)
    _root, commands_dir, _results_dir = _dirs(control_dir)
    command_id = uuid.uuid4().hex
    document: Dict[str, Any] = {
        "command_id": command_id,
        "action": action,
        "pair": pair,
        "requested_by": requested_by,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload is not None:
        document["payload"] = payload
    _atomic_write_json(commands_dir / f"{command_id}.json", document)
    return command_id


def write_result(control_dir: PathLike, command_id: str, ok: bool, detail: Dict[str, Any]) -> None:
    _root, _commands_dir, results_dir = _dirs(control_dir)
    document = {
        "command_id": command_id,
        "ok": ok,
        "detail": detail,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(results_dir / f"{command_id}.json", document)


def await_result(control_dir: PathLike, command_id: str, timeout: float = 30.0, poll_interval: float = 0.1) -> Dict[str, Any]:
    _root, _commands_dir, results_dir = _dirs(control_dir)
    result_path = results_dir / f"{command_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text())
            except (OSError, ValueError):
                pass
        time.sleep(poll_interval)
    raise ResultTimeout(f"no result for {command_id} within {timeout}s")


def append_audit(control_dir: PathLike, entry: Dict[str, Any]) -> None:
    root, _commands_dir, _results_dir = _dirs(control_dir)
    audit_path = root / "audit.log"
    record = dict(entry)
    record.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
    with audit_path.open("a") as audit_file:
        audit_file.write(json.dumps(record, sort_keys=True) + "\n")


class ControlLock:
    """Non-blocking flock over `<control_dir>/control.lock`, held for one
    full management round-trip (submit -> await_result). Mirrors
    session.py's WorkerLease pattern. A second acquire while held raises
    OperationInProgress instead of blocking."""

    def __init__(self, control_dir: PathLike) -> None:
        self.path = Path(control_dir) / "control.lock"
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise OperationInProgress("a management operation is already in progress") from exc
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
