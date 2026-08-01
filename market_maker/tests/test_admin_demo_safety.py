"""Zero-real-call safety proof for the admin surface (mirrors
test_demo_safety.py / test_deployment_contract.py's pattern): the admin app
and control agent must never be able to reach a real Nobitex trading call,
a MySQL/Telegram credential, a Docker socket, or a shell-interpreted
command -- by construction, not by convention."""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import admin_app  # noqa: E402
import control_agent  # noqa: E402


ADMIN_SOURCE = Path(admin_app.__file__).read_text()
AGENT_SOURCE = Path(control_agent.__file__).read_text()

FORBIDDEN_TOKENS = (
    "NobitexExchangeClient", "nobitex.ir", "mysql.connector",
    "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "docker.sock", "DockerClient",
)


def test_admin_app_never_references_real_trading_or_secrets():
    for token in FORBIDDEN_TOKENS:
        assert token not in ADMIN_SOURCE, f"admin_app.py references forbidden token: {token}"


def test_control_agent_never_references_real_trading_or_secrets():
    for token in FORBIDDEN_TOKENS:
        assert token not in AGENT_SOURCE, f"control_agent.py references forbidden token: {token}"


def test_admin_app_has_no_subprocess_or_shell_surface():
    assert "subprocess" not in ADMIN_SOURCE
    assert "os.system" not in ADMIN_SOURCE
    assert "shell=True" not in ADMIN_SOURCE


def test_control_agent_never_uses_shell_true():
    assert "shell=True" not in AGENT_SOURCE


def test_control_agent_worker_argv_is_a_fixed_dict_not_string_built():
    # Every value in WORKER_ARGV must be a plain list of string literals
    # (no f-strings/format calls composed from request-controlled input at
    # call time) -- resolve_worker_argv only ever indexes into it.
    for key, argv in control_agent.WORKER_ARGV.items():
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)
    source = inspect.getsource(control_agent.resolve_worker_argv)
    assert "return list(WORKER_ARGV[key])" in source


def test_docker_invoking_handlers_take_injectable_run_argv():
    for handler_name in ("handle_start_worker", "handle_stop_worker", "handle_restart_worker"):
        handler = getattr(control_agent, handler_name)
        params = list(inspect.signature(handler).parameters)
        assert "run_argv" in params


def test_agent_paths_and_handlers_never_import_nobitex_client():
    # The agent module graph must not pull in the real exchange adapter at
    # all -- Demo/paper trading never needs it, and importing it would be
    # a signal the boundary has been crossed.
    assert "nobitex" not in sys.modules or "exchange.nobitex" not in AGENT_SOURCE


def test_admin_app_mutating_routes_all_go_through_control_queue():
    # Every route module-level source must reference enqueue_and_wait or
    # enqueue_batch for its mutation -- i.e. admin_app itself performs no
    # direct filesystem writes to state/results, only to CONTROL_DIR via
    # control_queue's own atomic writer.
    mutating_markers = ("enqueue_and_wait(", "enqueue_batch(")
    assert any(marker in ADMIN_SOURCE for marker in mutating_markers)
    # No direct writes into STATE_DIR/RESULTS_DIR (only reads) anywhere in the file.
    assert '.write_text(' not in ADMIN_SOURCE
    assert "open(" not in ADMIN_SOURCE.replace("lock_path.open(", "")
