import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import control_agent as ca  # noqa: E402
from demo_config import load_overrides, overlay_path, write_overrides  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


class RecordingRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return FakeCompleted(self.returncode)


def make_paths(tmp_path):
    state_dir = tmp_path / "state"
    results_dir = tmp_path / "results"
    backups_dir = tmp_path / "backups"
    control_dir = tmp_path / "control"
    for d in (state_dir, results_dir, backups_dir, control_dir):
        d.mkdir(parents=True, exist_ok=True)
    return ca.AgentPaths(tmp_path, state_dir, results_dir, backups_dir, control_dir)


def test_resolve_worker_argv_fixed_dict_lookup():
    argv = ca.resolve_worker_argv("start_worker", "btc")
    assert argv == ca.COMPOSE_ARGV_PREFIX + ["start", "btc-demo"]


@pytest.mark.parametrize("action,pair", [
    ("start_worker", "eth"),
    ("delete_everything", "btc"),
    ("apply_config", "btc"),  # not a worker action
])
def test_resolve_worker_argv_rejects_outside_whitelist(action, pair):
    with pytest.raises(KeyError):
        ca.resolve_worker_argv(action, pair)


def test_worker_command_uses_injectable_runner(tmp_path):
    paths = make_paths(tmp_path)
    runner = RecordingRunner()
    result = ca.run_worker_command(paths, "stop_worker", "usdt", runner)
    assert result.ok is True
    assert runner.calls[0][0] == ca.COMPOSE_ARGV_PREFIX + ["stop", "usdt-demo"]


def test_is_worker_running_false_when_lock_absent(tmp_path):
    paths = make_paths(tmp_path)
    assert ca.is_worker_running(paths, "btc") is False


def test_is_worker_running_true_when_lock_held(tmp_path):
    import fcntl
    paths = make_paths(tmp_path)
    lock_path = ca.worker_lock_path(paths, "btc")
    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert ca.is_worker_running(paths, "btc") is True
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


def test_cancel_orders_noop_when_already_stopped(tmp_path):
    paths = make_paths(tmp_path)
    runner = RecordingRunner()
    result = ca.handle_cancel_orders(paths, "btc", None, runner)
    assert result.ok is True
    assert result.detail["was_running"] is False
    assert result.detail["now_running"] is False
    assert runner.calls == []


def test_cancel_orders_stops_and_restarts_when_running(tmp_path):
    import fcntl
    paths = make_paths(tmp_path)
    lock_path = ca.worker_lock_path(paths, "btc")
    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        runner = RecordingRunner()
        result = ca.handle_cancel_orders(paths, "btc", None, runner)
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    assert result.ok is True
    assert result.detail["was_running"] is True
    assert result.detail["now_running"] is True
    verbs = [call[0][-2] for call in runner.calls]  # second-to-last argv element is the compose verb
    assert verbs == ["stop", "start"]


def test_apply_config_writes_overlay_and_restarts(tmp_path):
    paths = make_paths(tmp_path)
    runner = RecordingRunner()
    result = ca.handle_apply_config(paths, "btc", {"overrides": {"order_size": 5}}, runner)
    assert result.ok is True
    assert load_overrides(overlay_path(paths.state_dir, "btc")) == {"order_size": 5.0}
    assert runner.calls[0][0][-2:] == ["restart", "btc-demo"]


def test_apply_config_rejects_invalid_overlay(tmp_path):
    paths = make_paths(tmp_path)
    runner = RecordingRunner()
    result = ca.handle_apply_config(paths, "btc", {"overrides": {"token": "leak"}}, runner)
    assert result.ok is False
    assert runner.calls == []


def test_rollback_config_restores_previous(tmp_path):
    paths = make_paths(tmp_path)
    path = overlay_path(paths.state_dir, "btc")
    write_overrides(path, {"order_size": 1})
    write_overrides(path, {"order_size": 2})
    runner = RecordingRunner()
    result = ca.handle_rollback_config(paths, "btc", None, runner)
    assert result.ok is True
    assert load_overrides(path) == {"order_size": 1.0}


def test_create_backup_snapshots_expected_files_only(tmp_path):
    paths = make_paths(tmp_path)
    (paths.state_dir / "paper_s1_btcirt.json").write_text('{"a":1}')
    (paths.state_dir / "stack_session.json").write_text('{"session_id":"x"}')
    (paths.state_dir / "other_file.txt").write_text("ignore me")
    (paths.results_dir / "btc_track_demo.csv").write_text("t,v\n")
    (paths.results_dir / "btc_track.csv").write_text("t,v\n")  # real csv, must not be copied

    result = ca.handle_create_backup(paths, None, {"prefix": "manual"}, None)
    assert result.ok is True
    backup_dir = paths.backups_dir / result.detail["backup"]
    names = {p.name for p in backup_dir.iterdir()}
    assert names == {"paper_s1_btcirt.json", "stack_session.json", "btc_track_demo.csv"}


def test_reset_demo_requires_confirmation(tmp_path):
    paths = make_paths(tmp_path)
    result = ca.handle_reset_demo(paths, None, {}, RecordingRunner())
    assert result.ok is False


def test_reset_demo_backs_up_then_clears_only_demo_suffixed_files(tmp_path):
    paths = make_paths(tmp_path)
    (paths.state_dir / "paper_s1_btcirt.json").write_text('{"a":1}')
    (paths.results_dir / "btc_track_demo.csv").write_text("t,v\n")
    (paths.results_dir / "btc_track.csv").write_text("keep me\n")

    runner = RecordingRunner()
    result = ca.handle_reset_demo(paths, None, {"confirm": "RESET"}, runner)
    assert result.ok is True
    assert not (paths.state_dir / "paper_s1_btcirt.json").exists()
    assert not (paths.results_dir / "btc_track_demo.csv").exists()
    assert (paths.results_dir / "btc_track.csv").read_text() == "keep me\n"
    backup_dir = paths.backups_dir / result.detail["backup"]
    assert (backup_dir / "paper_s1_btcirt.json").exists()
    assert (paths.state_dir / "stack_session.json").exists()  # new_session created it


def test_restore_backup_rejects_path_traversal(tmp_path):
    paths = make_paths(tmp_path)
    result = ca.handle_restore_backup(paths, None, {"confirm": "RESTORE", "backup_name": "../etc"}, RecordingRunner())
    assert result.ok is False


def test_restore_backup_rejects_unknown_name(tmp_path):
    paths = make_paths(tmp_path)
    result = ca.handle_restore_backup(paths, None, {"confirm": "RESTORE", "backup_name": "does-not-exist"}, RecordingRunner())
    assert result.ok is False


def test_restore_backup_snapshots_current_state_first(tmp_path):
    paths = make_paths(tmp_path)
    (paths.state_dir / "paper_s1_btcirt.json").write_text('{"a":1}')
    manual = ca.handle_create_backup(paths, None, {"prefix": "manual"}, None).detail["backup"]
    (paths.state_dir / "paper_s1_btcirt.json").write_text('{"a":2}')  # live state changed since backup

    result = ca.handle_restore_backup(paths, None, {"confirm": "RESTORE", "backup_name": manual}, RecordingRunner())
    assert result.ok is True
    assert json.loads((paths.state_dir / "paper_s1_btcirt.json").read_text()) == {"a": 1}
    pre_restore_dir = paths.backups_dir / result.detail["pre_restore_backup"]
    assert json.loads((pre_restore_dir / "paper_s1_btcirt.json").read_text()) == {"a": 2}


def test_recover_demo_clears_only_stale_lock(tmp_path):
    paths = make_paths(tmp_path)
    ca.worker_lock_path(paths, "btc").write_text("stale")
    result = ca.handle_recover_demo(paths, None, None, RecordingRunner())
    assert result.ok is True
    assert result.detail["cleared_stale_locks"] == ["btc"]
    assert not ca.worker_lock_path(paths, "btc").exists()


def test_recover_demo_never_calls_reset_when_destructive_fix_needed(tmp_path):
    paths = make_paths(tmp_path)
    (paths.state_dir / "stack_session.json").write_text('{"session_id":"current"}')
    (paths.state_dir / "paper_s1_btcirt.json").write_text('{"session_id":"stale","a":1}')
    result = ca.handle_recover_demo(paths, None, None, RecordingRunner())
    assert result.ok is True
    assert result.detail["session_mismatches"] == ["paper_s1_btcirt.json"]
    assert result.detail["confirmation_required_for_reset"] is True
    # still present -- recover_demo must never auto-delete/reset
    assert (paths.state_dir / "paper_s1_btcirt.json").exists()


def test_recover_demo_reports_corrupt_json_without_repairing(tmp_path):
    paths = make_paths(tmp_path)
    (paths.state_dir / "paper_s1_btcirt.json").write_text("{not json")
    result = ca.handle_recover_demo(paths, None, None, RecordingRunner())
    assert result.detail["corrupt_files"] == ["paper_s1_btcirt.json"]
    assert result.detail["confirmation_required_for_reset"] is True
    assert (paths.state_dir / "paper_s1_btcirt.json").read_text() == "{not json"


def test_handlers_dict_covers_every_queue_action():
    from control_queue import ACTIONS
    assert set(ca.HANDLERS) == set(ACTIONS)


def test_process_command_unknown_action_reports_error(tmp_path):
    paths = make_paths(tmp_path)
    result = ca.process_command(paths, {"action": "not_real", "command_id": "x"}, RecordingRunner())
    assert result.ok is False
