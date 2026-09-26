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
