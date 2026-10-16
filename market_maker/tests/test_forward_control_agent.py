import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deploy import forward_control_agent as agent
from admin_app import create_app


def test_stop_command_fails_when_worker_remains_alive(monkeypatch, tmp_path):
    paths = agent.AgentPaths(*(tmp_path / name for name in ("control", "config", "locks", "results", "logs")))
    commands = paths.control / "commands"
    commands.mkdir(parents=True)
    (commands / "stop1.json").write_text(json.dumps({
        "command_id": "stop1", "action": "stop_forward_worker", "pair": "btc",
    }))
    signals = []
    monkeypatch.setattr(agent, "_worker_pid", lambda paths, pair: 123)
    monkeypatch.setattr(agent, "worker_running", lambda paths, pair: True)
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(agent, "STOP_TIMEOUT", 0)

    agent.run_once(paths)

    result = json.loads((paths.control / "results" / "stop1.json").read_text())
    assert signals == [(123, agent.signal.SIGTERM)]
    assert result["ok"] is False
    assert "did not exit" in result["detail"]["error"]


def test_admin_wait_exceeds_agent_stop_timeout():
    app = create_app({"TESTING": True})
    assert app.config["COMMAND_TIMEOUT"] > agent.STOP_TIMEOUT


@pytest.mark.parametrize("detail", [{"stopped": False, "running": True}, None])
def test_admin_stop_does_not_report_success_for_stopped_false(monkeypatch, detail):
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
    monkeypatch.setattr("admin_app.enqueue_forward", lambda *a, **k: {
        "ok": True, "detail": detail,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "admin"
        session["last_seen"] = datetime.now(timezone.utc).isoformat()
        session["csrf_token"] = "test-csrf"

    response = client.post("/admin/forward/stop", data={"pair": "btc", "csrf_token": "test-csrf"})

    assert response.status_code == 502
    assert b"Forward-test worker stop requested" not in response.data


def test_forward_env_examples_default_to_disabled():
    root = Path(__file__).resolve().parents[2]
    for name in ("admin-forward.env.example", "forward.env.example"):
        lines = (root / "deploy" / name).read_text().splitlines()
        assert "FORWARD_TRADING_ENABLED=0" in lines


LIVE_HEADER = "Session_id,Algorithm,Crypto,Timestamp,Buy Status\n"


def _live_paths(tmp_path):
    paths = agent.AgentPaths(*(tmp_path / name for name in ("control", "config", "locks", "results", "logs")))
    (paths.control / "commands").mkdir(parents=True)
    paths.results.mkdir()
    (paths.results / "btc_track.csv").write_text(
        LIVE_HEADER + "1790443247.368056,1btc,BTCIRT,2026-09-26 20:50:47,not_sent\n"
        "1792069118.716741,1btc,BTCIRT,2026-10-15 16:28:38,not_sent\n"
    )
    (paths.results / "usdt_track.csv").write_text(
        LIVE_HEADER + "1790443247.368056,1usdt,USDTIRT,2026-09-26 20:50:48,not_sent\n"
        "1790443247.368056,1usdt,USDTIRT,2026-09-26 20:51:18,not_sent\n"
        "1792069104.17595,1usdt,USDTIRT,2026-10-15 16:28:24,not_sent\n"
    )
    return paths


def _submit(paths, command_id, payload):
    (paths.control / "commands" / f"{command_id}.json").write_text(json.dumps({
        "command_id": command_id, "action": "delete_forward_session", "pair": None, "payload": payload,
    }))
    agent.run_once(paths)
    return json.loads((paths.control / "results" / f"{command_id}.json").read_text())


def test_delete_forward_session_rewrites_the_live_history_with_a_backup(monkeypatch, tmp_path):
    paths = _live_paths(tmp_path)
    monkeypatch.setattr(agent, "worker_running", lambda paths, pair: False)

    result = _submit(paths, "del1", {"session_id": "1790443247.368056", "confirm": "DELETE"})

    assert result["ok"] is True
    assert result["detail"]["removed"] == {"btc": 1, "usdt": 2}
    assert (paths.results / "btc_track.csv").read_text() == LIVE_HEADER + "1792069118.716741,1btc,BTCIRT,2026-10-15 16:28:38,not_sent\n"
    assert (paths.results / "usdt_track.csv").read_text() == LIVE_HEADER + "1792069104.17595,1usdt,USDTIRT,2026-10-15 16:28:24,not_sent\n"
    backup = Path(result["detail"]["backup"])
    assert backup.parent == paths.results / "backups"
    assert "1790443247.368056,1usdt" in (backup / "usdt_track.csv").read_text()
    assert not list(paths.results.glob(".*.tmp"))


def test_delete_forward_session_refuses_while_a_live_worker_runs(monkeypatch, tmp_path):
    paths = _live_paths(tmp_path)
    monkeypatch.setattr(agent, "worker_running", lambda paths, pair: pair == "usdt")

    result = _submit(paths, "del2", {"session_id": "1790443247.368056", "confirm": "DELETE"})

    assert result["ok"] is False
    assert "stop the live workers" in result["detail"]["error"]
    assert "1790443247.368056" in (paths.results / "usdt_track.csv").read_text()


@pytest.mark.parametrize("payload, message", [
    ({"session_id": "1790443247.368056"}, "confirmation required"),
    ({"session_id": "../etc", "confirm": "DELETE"}, "invalid or missing session_id"),
    ({"session_id": "9999999999.0", "confirm": "DELETE"}, "session not found"),
])
def test_delete_forward_session_rejects_bad_requests(monkeypatch, tmp_path, payload, message):
    paths = _live_paths(tmp_path)
    monkeypatch.setattr(agent, "worker_running", lambda paths, pair: False)

    result = _submit(paths, "del3", payload)

    assert result["ok"] is False
    assert message in result["detail"]["error"]


# ---------- the algorithm a live worker is started with ----------

def _forward_start_paths(tmp_path):
    paths = agent.AgentPaths(*(tmp_path / name for name in ("control", "config", "locks", "results", "logs")))
    for directory in (paths.control, paths.config, paths.locks, paths.results, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.mark.parametrize("saved, expected", [(None, "algo1"), ("algo1", "algo1"), ("algo3", "algo3")])
def test_live_start_uses_the_algorithm_of_the_saved_live_configuration(monkeypatch, tmp_path, saved, expected):
    from config import TRADING_PROFILES
    from forward_config import save_config

    paths = _forward_start_paths(tmp_path)
    if saved is not None:
        save_config({"algorithm": saved}, "btc", TRADING_PROFILES, paths.config)
    started = []
    monkeypatch.setattr(agent, "_start_worker", lambda paths, pair, algorithm="algo1": started.append((pair, algorithm)) or {"running": True})

    detail = agent.process_command(paths, {"action": "start_forward_worker", "pair": "btc", "payload": {"confirmed": True}})

    assert started == [("btc", expected)]
    assert detail["algorithm"] == expected


def test_live_worker_argv_carries_the_algorithm(monkeypatch, tmp_path):
    paths = _forward_start_paths(tmp_path)
    monkeypatch.setenv("FORWARD_TRADING_ENABLED", "1")
    monkeypatch.setenv("NOBITEX_API_KEY", "test-only")
    monkeypatch.setenv("NOBITEX_SECRET_KEY", "test-only")
    calls = {"running": 0}

    def fake_running(paths, pair):
        calls["running"] += 1
        return calls["running"] > 1          # not running before the start, running after

    launched = []

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        launched.append(argv)
        return FakeProcess()

    monkeypatch.setattr(agent, "worker_running", fake_running)
    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)

    result = agent._start_worker(paths, "usdt", "algo3")

    assert result["running"] is True
    [argv] = launched
    assert argv[argv.index("--algorithm") + 1] == "algo3"
    assert argv[argv.index("--mode") + 1] == "real" and argv[argv.index("--profile") + 1] == "usdt"
