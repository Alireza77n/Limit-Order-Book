import pytest

from demo_entrypoint import DemoStartupError, validate_demo_startup


SAFE_WORKER = ["python", "run.py", "--profile", "btc", "--mode", "demo", "--continuous"]


def test_safe_demo_worker_is_accepted():
    validate_demo_startup(SAFE_WORKER, {})


@pytest.mark.parametrize("command", [
    ["python", "run.py", "--profile", "btc", "--mode", "real"],
    ["python", "run.py", "--profile", "btc"],
    ["python", "/app/market_maker/run.py", "--profile", "btc", "--mode", "real"],
    ["python", "/app/market_maker/run.py", "--profile", "btc"],
])
def test_worker_must_explicitly_select_demo(command):
    with pytest.raises(DemoStartupError, match="--mode demo"):
        validate_demo_startup(command, {})


@pytest.mark.parametrize("command", [
    ["python", "dashboard.py"],
    ["python", "/app/market_maker/session.py"],
])
def test_non_worker_commands_are_accepted(command):
    validate_demo_startup(command, {})


@pytest.mark.parametrize("name", [
    "NOBITEX_API_TOKEN", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
    "MYSQL_DATABASE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
])
def test_sensitive_environment_must_be_empty(name):
    with pytest.raises(DemoStartupError, match=name):
        validate_demo_startup(SAFE_WORKER, {name: "forbidden"})


@pytest.mark.parametrize("name,value", [
    ("LOB_RESULTS_DIR", "/home/ubuntu/sarkhatti/data"),
    ("LOB_STATE_DIR", "/app/market_maker/results"),
    ("DEMO_SESSION_FILE", "/tmp/stack_session.json"),
    ("LOB_CONTROL_DIR", "/app/runtime/results"),
    ("LOB_BACKUPS_DIR", "/home/ubuntu/sarkhatti/data"),
])
def test_runtime_paths_must_stay_in_dedicated_roots(name, value):
    with pytest.raises(DemoStartupError, match=name):
        validate_demo_startup(SAFE_WORKER, {name: value})


@pytest.mark.parametrize("name,value", [
    ("LOB_RESULTS_DIR", "/app/runtime/results/../state"),
    ("LOB_STATE_DIR", "/app/runtime/state/../results"),
    ("DEMO_SESSION_FILE", "/app/runtime/state/../results/stack_session.json"),
    ("LOB_CONTROL_DIR", "/app/runtime/control/../state"),
    ("LOB_BACKUPS_DIR", "/app/runtime/backups/../results"),
])
def test_runtime_paths_reject_parent_traversal(name, value):
    with pytest.raises(DemoStartupError, match=name):
        validate_demo_startup(SAFE_WORKER, {name: value})
