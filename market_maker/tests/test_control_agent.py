import json
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import config  # noqa: E402
import control_agent as ca  # noqa: E402
from demo_config import (  # noqa: E402
    desired_digest,
    load_desired_config,
    load_overrides,
    overlay_path,
    write_overrides,
)


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


def test_local_mode_uses_root_compose_and_market_maker_results(tmp_path):
    repo_root = tmp_path
    paths = ca.build_agent_paths("local", repo_root)
    local_results = repo_root / "market_maker" / "results"

    assert paths.state_dir == local_results
    assert paths.results_dir == local_results
    assert paths.control_dir == local_results / "control"
    assert paths.backups_dir == local_results / "backups"
    argv = ca.resolve_worker_argv("restart_worker", "btc", mode="local")
    assert argv[-2:] == ["restart", "btc-demo"]
    assert "docker-compose.yml" in argv
    assert "deploy/docker-compose.production.yml" not in argv


def test_local_new_session_uses_compose_liveness_and_restarts_workers(tmp_path):
    paths = ca.build_agent_paths("local", tmp_path)
    paths.state_dir.mkdir(parents=True)
    calls = []

    def local_runner(argv, **kwargs):
        calls.append(list(argv))
        completed = FakeCompleted(0)
        completed.stdout = f"{argv[-1]}\n" if "ps" in argv else ""
        return completed

    result = ca.handle_new_session(paths, None, None, local_runner)

    assert result.ok is True
    assert result.detail["restarted"] == ["btc", "usdt"]
    assert calls[:2] == [
        ca.LOCAL_COMPOSE_ARGV_PREFIX
        + ["ps", "--status", "running", "--services", "btc-demo"],
        ca.LOCAL_COMPOSE_ARGV_PREFIX
        + ["ps", "--status", "running", "--services", "usdt-demo"],
    ]
    assert calls[2:] == [
        ca.LOCAL_COMPOSE_ARGV_PREFIX + ["stop", "btc-demo"],
        ca.LOCAL_COMPOSE_ARGV_PREFIX + ["stop", "usdt-demo"],
        ca.LOCAL_COMPOSE_ARGV_PREFIX
        + ["up", "-d", "--no-deps", "--no-recreate", "btc-demo"],
        ca.LOCAL_COMPOSE_ARGV_PREFIX
        + ["up", "-d", "--no-deps", "--no-recreate", "usdt-demo"],
    ]


@pytest.mark.parametrize("failure_mode", ["nonzero", "exception"])
def test_local_worker_liveness_query_failure_is_explicit(tmp_path, failure_mode):
    paths = ca.build_agent_paths("local", tmp_path)

    def failing_runner(argv, **kwargs):
        if failure_mode == "exception":
            raise OSError("docker status unavailable")
        return FakeCompleted(17)

    with pytest.raises(ca.WorkerStatusError, match="btc.*status"):
        ca.is_worker_running(paths, "btc", failing_runner)


def _tree_snapshot(root):
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("action", "pair", "payload"),
    [
        ("new_session", None, None),
        ("cancel_orders", "btc", None),
        ("reset_demo", None, {"confirm": "RESET"}),
        (
            "restore_backup",
            None,
            {"confirm": "RESTORE", "backup_name": "manual"},
        ),
        ("recover_demo", None, None),
    ],
)
@pytest.mark.parametrize("failure_mode", ["nonzero", "exception"])
def test_local_destructive_actions_fail_closed_when_liveness_is_unknown(
    tmp_path, action, pair, payload, failure_mode
):
    paths = ca.build_agent_paths("local", tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.backups_dir.mkdir(parents=True)
    (paths.state_dir / "stack_session.json").write_text(
        '{"session_id":"original"}'
    )
    (paths.state_dir / "paper_original_btcirt.json").write_text(
        '{"session_id":"original","value":"live"}'
    )
    ca.worker_lock_path(paths, "btc").write_text("btc lock")
    ca.worker_lock_path(paths, "usdt").write_text("usdt lock")
    backup_dir = paths.backups_dir / "manual"
    backup_dir.mkdir()
    (backup_dir / "paper_original_btcirt.json").write_text(
        '{"session_id":"original","value":"backup"}'
    )
    before = _tree_snapshot(tmp_path)

    def failing_runner(argv, **kwargs):
        if failure_mode == "exception":
            raise OSError("docker status unavailable")
        return FakeCompleted(17)

    result = ca.process_command(
        paths,
        {"action": action, "pair": pair, "payload": payload},
        failing_runner,
    )

    assert result.ok is False
    assert result.detail["phase"] == "liveness"
    assert "worker status" in result.detail["error"]
    assert _tree_snapshot(tmp_path) == before


def test_local_recover_preflights_every_status_before_unlinking(tmp_path):
    paths = ca.build_agent_paths("local", tmp_path)
    paths.state_dir.mkdir(parents=True)
    ca.worker_lock_path(paths, "btc").write_text("btc lock")
    ca.worker_lock_path(paths, "usdt").write_text("usdt lock")
    before = _tree_snapshot(tmp_path)
    queries = []

    def second_query_fails(argv, **kwargs):
        queries.append(argv[-1])
        if len(queries) == 2:
            raise OSError("docker status unavailable")
        completed = FakeCompleted(0)
        completed.stdout = ""
        return completed

    result = ca.handle_recover_demo(paths, None, None, second_query_fails)

    assert result.ok is False
    assert result.detail["phase"] == "liveness"
    assert queries == ["btc-demo", "usdt-demo"]
    assert _tree_snapshot(tmp_path) == before


def test_resolve_worker_argv_fixed_dict_lookup():
    argv = ca.resolve_worker_argv("start_worker", "btc")
    assert argv == ca.COMPOSE_ARGV_PREFIX + [
        "up", "-d", "--no-deps", "--no-recreate", "btc-demo",
    ]


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
    assert [call[0] for call in runner.calls] == [
        ca.COMPOSE_ARGV_PREFIX + ["stop", "btc-demo"],
        ca.COMPOSE_ARGV_PREFIX
        + ["up", "-d", "--no-deps", "--no-recreate", "btc-demo"],
    ]


def test_cancel_orders_aborts_restart_when_stop_fails(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    monkeypatch.setattr(ca, "worker_statuses", lambda *_args: {"btc": True})
    runner = RecordingRunner(returncode=1)

    result = ca.handle_cancel_orders(paths, "btc", None, runner)

    assert result.ok is False
    assert result.detail["phase"] == "stop"
    assert result.detail["now_running"] is True
    assert len(runner.calls) == 1
    assert runner.calls[0][0][-2:] == ["stop", "btc-demo"]


def test_cancel_orders_reports_start_failure_and_final_state(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    statuses = iter(({"btc": True}, {"btc": False}))
    monkeypatch.setattr(ca, "worker_statuses", lambda *_args: next(statuses))
    calls = 0

    def fail_start(argv, **kwargs):
        nonlocal calls
        calls += 1
        return FakeCompleted(0 if calls == 1 else 1)

    result = ca.handle_cancel_orders(paths, "btc", None, fail_start)

    assert result.ok is False
    assert result.detail["phase"] == "start"
    assert result.detail["now_running"] is False
    assert result.detail["start"]["returncode"] == 1


def test_new_session_aborts_if_a_running_worker_cannot_stop(tmp_path):
    import fcntl

    paths = make_paths(tmp_path)
    lock_path = ca.worker_lock_path(paths, "btc")
    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls = []

    def failing_stop_runner(argv, **kwargs):
        calls.append(list(argv))
        return FakeCompleted(1)

    try:
        result = ca.handle_new_session(paths, None, None, failing_stop_runner)
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    assert result.ok is False
    assert result.detail["phase"] == "stop"
    assert result.detail["pair"] == "btc"
    assert result.detail["action"] == "stop_worker"
    assert calls[0][-2:] == ["stop", "btc-demo"]
    assert not (paths.state_dir / "stack_session.json").exists()


def test_new_session_reports_stop_runner_exception_with_lifecycle_context(tmp_path):
    import fcntl

    paths = make_paths(tmp_path)
    lock_path = ca.worker_lock_path(paths, "btc")
    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def raising_runner(argv, **kwargs):
        raise OSError("docker unavailable")

    try:
        result = ca.handle_new_session(paths, None, None, raising_runner)
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    assert result.ok is False
    assert result.detail["phase"] == "stop"
    assert result.detail["pair"] == "btc"
    assert result.detail["action"] == "stop_worker"
    assert result.detail["failure"] == {"error": "OSError: docker unavailable"}


def test_new_session_reports_partial_failure_if_worker_cannot_restart(tmp_path):
    import fcntl

    paths = make_paths(tmp_path)
    lock_path = ca.worker_lock_path(paths, "btc")
    lease = {"file": lock_path.open("a+")}
    fcntl.flock(lease["file"].fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def failing_start_runner(argv, **kwargs):
        if argv[-2] == "stop":
            fcntl.flock(lease["file"].fileno(), fcntl.LOCK_UN)
            lease["file"].close()
            lease["file"] = None
            return FakeCompleted(0)
        return FakeCompleted(1)

    try:
        result = ca.handle_new_session(paths, None, None, failing_start_runner)
    finally:
        if lease["file"] is not None:
            fcntl.flock(lease["file"].fileno(), fcntl.LOCK_UN)
            lease["file"].close()

    assert result.ok is False
    assert result.detail["phase"] == "start"
    assert result.detail["pair"] == "btc"
    assert result.detail["action"] == "start_worker"
    assert result.detail["failures"] == [{
        "pair": "btc",
        "action": "start_worker",
        "detail": {
            "argv": ca.COMPOSE_ARGV_PREFIX
            + ["up", "-d", "--no-deps", "--no-recreate", "btc-demo"],
            "returncode": 1,
        },
    }]
    assert (paths.state_dir / "stack_session.json").exists()


def test_new_session_reports_start_runner_exception_with_lifecycle_context(tmp_path):
    import fcntl

    paths = make_paths(tmp_path)
    lock_path = ca.worker_lock_path(paths, "btc")
    lease = {"file": lock_path.open("a+")}
    fcntl.flock(lease["file"].fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def raising_start_runner(argv, **kwargs):
        if argv[-2] == "stop":
            fcntl.flock(lease["file"].fileno(), fcntl.LOCK_UN)
            lease["file"].close()
            lease["file"] = None
            return FakeCompleted(0)
        raise OSError("docker unavailable")

    try:
        result = ca.handle_new_session(paths, None, None, raising_start_runner)
    finally:
        if lease["file"] is not None:
            fcntl.flock(lease["file"].fileno(), fcntl.LOCK_UN)
            lease["file"].close()

    assert result.ok is False
    assert result.detail["phase"] == "start"
    assert result.detail["pair"] == "btc"
    assert result.detail["action"] == "start_worker"
    assert result.detail["failures"] == [{
        "pair": "btc",
        "action": "start_worker",
        "detail": {"error": "OSError: docker unavailable"},
    }]


def test_apply_config_persists_without_restarting_worker(tmp_path):
    paths = make_paths(tmp_path)
    runner = RecordingRunner()
    result = ca.handle_apply_config(paths, "btc", {"overrides": {"order_size": 0.2}}, runner)
    assert result.ok is True
    assert load_overrides(
        overlay_path(paths.state_dir, "btc"), pair="btc", profiles=config.TRADING_PROFILES
    ) == {"order_size": 0.2}
    assert runner.calls == []
    assert result.detail["persisted"] == {"order_size": 0.2}
    assert result.detail["modes"] == {"order_size": "hot"}
    assert result.detail["desired_digest"] == desired_digest(
        load_desired_config("btc", paths.state_dir, config.TRADING_PROFILES)
    )


def test_apply_config_rejects_invalid_overlay(tmp_path):
    paths = make_paths(tmp_path)
    runner = RecordingRunner()
    result = ca.handle_apply_config(paths, "btc", {"overrides": {"token": "leak"}}, runner)
    assert result.ok is False
    assert runner.calls == []


def test_rollback_config_restores_previous(tmp_path):
    paths = make_paths(tmp_path)
    path = overlay_path(paths.state_dir, "btc")
    write_overrides(
        path, {"order_size": 1}, pair="btc", profiles=config.TRADING_PROFILES
    )
    write_overrides(
        path, {"order_size": 2}, pair="btc", profiles=config.TRADING_PROFILES
    )
    runner = RecordingRunner()
    result = ca.handle_rollback_config(paths, "btc", None, runner)
    assert result.ok is True
    assert load_overrides(
        path, pair="btc", profiles=config.TRADING_PROFILES
    ) == {"order_size": 1.0}
    assert runner.calls == []
    assert result.detail["restored"] == {"order_size": 1.0}
    assert result.detail["modes"] == {"order_size": "hot"}
    assert result.detail["status"] == "stopped"
    assert result.detail["worker_running"] is False
    assert result.detail["desired_digest"] == desired_digest(
        load_desired_config("btc", paths.state_dir, config.TRADING_PROFILES)
    )


def test_rollback_running_worker_reports_hot_reload_without_restart(
    tmp_path, monkeypatch,
):
    paths = make_paths(tmp_path)
    path = overlay_path(paths.state_dir, "btc")
    write_overrides(
        path, {"order_size": 1}, pair="btc", profiles=config.TRADING_PROFILES,
    )
    write_overrides(
        path, {"order_size": 2}, pair="btc", profiles=config.TRADING_PROFILES,
    )
    monkeypatch.setattr(ca, "worker_statuses", lambda *_args: {"btc": True})
    runner = RecordingRunner()

    result = ca.handle_rollback_config(paths, "btc", None, runner)

    assert result.ok is True
    assert result.detail["status"] == "waiting"
    assert result.detail["worker_running"] is True
    assert runner.calls == []


def test_rollback_reports_new_session_values_as_pending(tmp_path):
    paths = make_paths(tmp_path)
    path = overlay_path(paths.state_dir, "btc")
    write_overrides(
        path, {"commission_perc": 0.01},
        pair="btc", profiles=config.TRADING_PROFILES,
    )
    write_overrides(
        path, {"commission_perc": 0.02},
        pair="btc", profiles=config.TRADING_PROFILES,
    )

    result = ca.handle_rollback_config(paths, "btc", None, RecordingRunner())

    assert result.ok is True
    assert result.detail["status"] == "stopped"
    assert result.detail["field_status"] == {
        "commission_perc": "pending_new_session",
    }


def test_rollback_rejects_invalid_previous_without_changing_overlay(tmp_path):
    paths = make_paths(tmp_path)
    path = overlay_path(paths.state_dir, "btc")
    previous_path = path.with_name(path.name + ".previous")
    path.write_text('{"order_size": 2}')
    previous_path.write_text('{"not_a_real_field": 1}')
    before = path.read_bytes()
    runner = RecordingRunner()

    result = ca.handle_rollback_config(paths, "btc", None, runner)

    assert result.ok is False
    assert "unknown field" in result.detail["error"]
    assert path.read_bytes() == before
    assert runner.calls == []


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


def _local_destructive_fixture(tmp_path):
    paths = ca.build_agent_paths("local", tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.backups_dir.mkdir(parents=True)
    (paths.state_dir / "stack_session.json").write_text(
        '{"session_id":"original"}'
    )
    (paths.state_dir / "paper_original_btcirt.json").write_text(
        '{"session_id":"original","value":"live"}'
    )
    backup_dir = paths.backups_dir / "manual"
    backup_dir.mkdir()
    (backup_dir / "paper_original_btcirt.json").write_text(
        '{"session_id":"original","value":"backup"}'
    )
    return paths


@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        (ca.handle_reset_demo, {"confirm": "RESET"}),
        (
            ca.handle_restore_backup,
            {"confirm": "RESTORE", "backup_name": "manual"},
        ),
    ],
)
def test_destructive_action_stop_failure_aborts_before_state_mutation(
    tmp_path, handler, payload
):
    paths = _local_destructive_fixture(tmp_path)
    before = _tree_snapshot(tmp_path)

    def failing_stop(argv, **kwargs):
        completed = FakeCompleted(1 if "stop" in argv else 0)
        completed.stdout = f"{argv[-1]}\n" if "ps" in argv else ""
        return completed

    result = handler(paths, None, payload, failing_stop)

    assert result.ok is False
    assert result.detail == {
        "phase": "stop",
        "pair": "btc",
        "action": "stop_worker",
        "failure": {
            "argv": ca.LOCAL_COMPOSE_ARGV_PREFIX + ["stop", "btc-demo"],
            "returncode": 1,
        },
        "stopped": [],
    }
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        (ca.handle_reset_demo, {"confirm": "RESET"}),
        (
            ca.handle_restore_backup,
            {"confirm": "RESTORE", "backup_name": "manual"},
        ),
    ],
)
def test_destructive_action_restart_failure_is_reported_with_partial_state(
    tmp_path, handler, payload
):
    paths = _local_destructive_fixture(tmp_path)

    def failing_btc_start(argv, **kwargs):
        completed = FakeCompleted(
            1 if "up" in argv and argv[-1] == "btc-demo" else 0
        )
        completed.stdout = f"{argv[-1]}\n" if "ps" in argv else ""
        return completed

    result = handler(paths, None, payload, failing_btc_start)

    assert result.ok is False
    assert result.detail["phase"] == "start"
    assert result.detail["pair"] == "btc"
    assert result.detail["action"] == "start_worker"
    assert result.detail["stopped"] == ["btc", "usdt"]
    assert result.detail["started"] == ["usdt"]
    assert result.detail["failures"] == [{
        "pair": "btc",
        "action": "start_worker",
        "detail": {
            "argv": ca.LOCAL_COMPOSE_ARGV_PREFIX
            + ["up", "-d", "--no-deps", "--no-recreate", "btc-demo"],
            "returncode": 1,
        },
    }]


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


def test_agent_leaves_user_audit_logging_to_admin_app():
    """One user action must produce exactly one audit record."""
    assert "append_audit" not in inspect.getsource(ca)
