import argparse
import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union


PathLike = Union[str, os.PathLike]


class SessionError(RuntimeError):
    pass


class DuplicateWorkerError(RuntimeError):
    pass


class WorkerLease:
    """Process-lifetime nonblocking lease stored on the shared results volume."""

    def __init__(self, path: PathLike, *, identity: str) -> None:
        self.path = Path(path)
        self.identity = identity
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lease_file = self.path.open("a+")
        try:
            fcntl.flock(lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lease_file.close()
            raise DuplicateWorkerError(
                f"Duplicate active worker rejected: {self.identity}"
            ) from exc
        lease_file.seek(0)
        lease_file.truncate()
        lease_file.write(f"{self.identity} pid={os.getpid()}\n")
        lease_file.flush()
        self._file = lease_file

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


def _session_id(now: datetime, nonce: str) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"demo-{stamp}-{nonce}"


def load_stack_session(path: PathLike) -> str:
    session_path = Path(path)
    if not session_path.exists():
        raise SessionError(f"Shared session file does not exist: {session_path}")
    try:
        document = json.loads(session_path.read_text())
        value = document.get("session_id")
    except (OSError, ValueError, TypeError) as exc:
        raise SessionError(f"Shared session document is invalid: {session_path}") from exc
    if not isinstance(value, str) or not value.strip():
        raise SessionError(f"Shared session document is invalid: {session_path}")
    return value


def initialize_stack_session(
    path: PathLike,
    *,
    now: Optional[datetime] = None,
    nonce: Optional[str] = None,
    force_new: bool = False,
) -> str:
    session_path = Path(path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = session_path.with_name(f".{session_path.name}.lock")

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if session_path.exists() and not force_new:
            return load_stack_session(session_path)

        created_at = now or datetime.now(timezone.utc)
        unique = nonce or uuid.uuid4().hex[:12]
        value = _session_id(created_at, unique)
        document = {
            "session_id": value,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
        }
        temp_path = session_path.with_name(f".{session_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("x") as temp_file:
                json.dump(document, temp_file, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, session_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return value


def initialize_stack_for_workers(
    path: PathLike,
    worker_lock_paths,
    *,
    now: Optional[datetime] = None,
    nonce: Optional[str] = None,
) -> str:
    """Reuse while any worker is live; create new only for a stopped stack."""
    active_worker = False
    acquired_files = []
    try:
        for worker_path in worker_lock_paths:
            lock_path = Path(worker_path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired_files.append(lock_file)
            except BlockingIOError:
                active_worker = True
                lock_file.close()
        if active_worker:
            return load_stack_session(path)
        return initialize_stack_session(
            path, now=now, nonce=nonce, force_new=True
        )
    finally:
        for lock_file in acquired_files:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or inspect the shared Demo stack session.")
    parser.add_argument("command", choices=["init", "stack-init", "load"])
    parser.add_argument("--path", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--worker-lock", action="append", default=[])
    args = parser.parse_args()

    if args.command == "init":
        value = initialize_stack_session(args.path, force_new=args.force)
        print(f"Shared session created: {value}" if args.force else f"Shared session loaded: {value}")
    elif args.command == "stack-init":
        value = initialize_stack_for_workers(args.path, args.worker_lock)
        print(f"Shared stack session ready: {value}")
    else:
        print(load_stack_session(args.path))


if __name__ == "__main__":
    main()
