import json

import pytest

from control_queue import (
    ControlLock,
    OperationInProgress,
    QueueValidationError,
    ResultTimeout,
    append_audit,
    await_result,
    submit_command,
    write_result,
)


def test_submit_command_rejects_unknown_action(tmp_path):
    with pytest.raises(QueueValidationError):
        submit_command(tmp_path, "delete_everything", requested_by="admin")


def test_submit_command_rejects_bad_pair(tmp_path):
    with pytest.raises(QueueValidationError):
        submit_command(tmp_path, "start_worker", pair="eth", requested_by="admin")


def test_submit_command_rejects_missing_pair_for_pair_action(tmp_path):
    with pytest.raises(QueueValidationError):
        submit_command(tmp_path, "start_worker", pair=None, requested_by="admin")


def test_submit_command_rejects_pair_for_stackwide_action(tmp_path):
    with pytest.raises(QueueValidationError):
        submit_command(tmp_path, "reset_demo", pair="btc", requested_by="admin")


def test_submit_and_await_result_round_trip(tmp_path):
    command_id = submit_command(tmp_path, "start_worker", pair="btc", requested_by="admin")
    written = json.loads((tmp_path / "commands" / f"{command_id}.json").read_text())
    assert written["action"] == "start_worker"
    write_result(tmp_path, command_id, ok=True, detail={"argv": ["docker"]})
    result = await_result(tmp_path, command_id, timeout=1.0)
    assert result["ok"] is True
    assert result["detail"]["argv"] == ["docker"]


def test_await_result_times_out(tmp_path):
    with pytest.raises(ResultTimeout):
        await_result(tmp_path, "nonexistent", timeout=0.2, poll_interval=0.05)


def test_lock_contention_raises_operation_in_progress(tmp_path):
    lock1 = ControlLock(tmp_path)
    lock1.acquire()
    lock2 = ControlLock(tmp_path)
    try:
        with pytest.raises(OperationInProgress):
            lock2.acquire()
    finally:
        lock1.release()


def test_lock_releases_and_can_be_reacquired(tmp_path):
    lock1 = ControlLock(tmp_path)
    with lock1:
        pass
    lock2 = ControlLock(tmp_path)
    with lock2:
        pass  # does not raise


def test_audit_entries_are_well_formed_jsonl(tmp_path):
    append_audit(tmp_path, {"action": "start_worker", "pair": "btc", "ok": True})
    append_audit(tmp_path, {"action": "stop_worker", "pair": "usdt", "ok": True})
    lines = (tmp_path / "audit.log").read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert "logged_at" in record
        assert "action" in record
