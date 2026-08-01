from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from session import (
    SessionError,
    WorkerLease,
    initialize_stack_for_workers,
    initialize_stack_session,
    load_stack_session,
)


FIXED_NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def test_initialize_creates_then_reuses_one_session(tmp_path):
    path = tmp_path / "stack-session.json"

    first = initialize_stack_session(path, now=FIXED_NOW, nonce="first")
    second = initialize_stack_session(path, now=FIXED_NOW, nonce="second")

    assert first == "demo-20260801T080000Z-first"
    assert second == first
    assert load_stack_session(path) == first


def test_concurrent_initialization_returns_one_id(tmp_path):
    path = tmp_path / "stack-session.json"

    def initialize(index):
        return initialize_stack_session(path, now=FIXED_NOW, nonce=str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(initialize, range(32)))

    assert len(set(ids)) == 1
    assert load_stack_session(path) == ids[0]


def test_force_new_models_a_genuine_full_stack_restart(tmp_path):
    path = tmp_path / "stack-session.json"
    old = initialize_stack_session(path, now=FIXED_NOW, nonce="old")

    new = initialize_stack_session(path, now=FIXED_NOW, nonce="new", force_new=True)

    assert new != old
    assert load_stack_session(path) == new


def test_load_rejects_missing_or_corrupt_session_document(tmp_path):
    path = tmp_path / "stack-session.json"
    with pytest.raises(SessionError, match="does not exist"):
        load_stack_session(path)

    path.write_text('{"session_id": ""}')
    with pytest.raises(SessionError, match="invalid"):
        load_stack_session(path)


def test_stack_initializer_reuses_session_while_worker_is_active(tmp_path):
    session_path = tmp_path / "stack-session.json"
    worker_path = tmp_path / ".worker_btc.lock"
    original = initialize_stack_session(session_path, now=FIXED_NOW, nonce="original")

    with WorkerLease(worker_path, identity="btc"):
        retry = initialize_stack_for_workers(
            session_path,
            [worker_path],
            now=FIXED_NOW,
            nonce="retry",
        )

    assert retry == original


def test_stack_initializer_creates_new_session_after_all_workers_stop(tmp_path):
    session_path = tmp_path / "stack-session.json"
    worker_path = tmp_path / ".worker_btc.lock"
    original = initialize_stack_session(session_path, now=FIXED_NOW, nonce="original")

    replacement = initialize_stack_for_workers(
        session_path,
        [worker_path],
        now=FIXED_NOW,
        nonce="replacement",
    )

    assert replacement != original
    assert load_stack_session(session_path) == replacement
