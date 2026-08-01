import os
from pathlib import Path
import re
import subprocess
import sys

from demo_entrypoint import SENSITIVE_ENV


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "deploy/docker-compose.production.yml"
ENV_PATH = ROOT / "deploy/demo.env"
SERVICES = ("session-init", "btc-demo", "usdt-demo", "dashboard")
CONTAINER_NAMES = {
    "session-init": "lob-session-init",
    "btc-demo": "lob-btc-demo",
    "usdt-demo": "lob-usdt-demo",
    "dashboard": "lob-dashboard",
}
COMMANDS = {
    "session-init": [
        "python", "demo_entrypoint.py", "python", "session.py", "stack-init",
        "--path", "/app/runtime/state/stack_session.json",
        "--worker-lock", "/app/runtime/state/.worker_btcirt.lock",
        "--worker-lock", "/app/runtime/state/.worker_usdtirt.lock",
    ],
    "btc-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "btc",
        "--mode", "demo", "--continuous",
    ],
    "usdt-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "usdt",
        "--mode", "demo", "--continuous",
    ],
    "dashboard": ["python", "demo_entrypoint.py", "python", "dashboard.py"],
}


def _service_blocks(text: str) -> dict[str, str]:
    services_text = text.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    starts = list(re.finditer(r"(?m)^  ([a-z0-9-]+):\n", services_text))
    return {
        match.group(1): services_text[match.start(): starts[index + 1].start()]
        if index + 1 < len(starts) else services_text[match.start():]
        for index, match in enumerate(starts)
    }


def _list(block: str, marker: str) -> list[str]:
    lines = block.splitlines()
    start = lines.index(marker) + 1
    marker_indent = len(marker) - len(marker.lstrip())
    values = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) <= marker_indent:
            break
        if line.startswith(" " * (marker_indent + 2) + "- "):
            values.append(line.strip()[2:].strip('"'))
    return values


def test_all_service_commands_use_the_guard_and_inner_python():
    text = COMPOSE_PATH.read_text()
    blocks = _service_blocks(text)

    assert "name: limit-order-book" in text.split("\n", 1)[0]
    assert set(blocks) == set(SERVICES)
    for name, expected in COMMANDS.items():
        assert f"container_name: {CONTAINER_NAMES[name]}" in blocks[name]
        assert _list(blocks[name], "    command:") == expected


def test_demo_entrypoint_forwards_a_representative_inner_python_command():
    env = os.environ.copy()
    for name in SENSITIVE_ENV:
        env.pop(name, None)

    completed = subprocess.run(
        [
            sys.executable,
            "demo_entrypoint.py",
            sys.executable,
            "-c",
            "print('FORWARDED_COMMAND_OK')",
        ],
        cwd=ROOT / "market_maker",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "DEMO_STARTUP_GUARD_OK" in completed.stdout
    assert completed.stdout.rstrip().endswith("FORWARDED_COMMAND_OK")


def test_dashboard_is_the_only_service_with_a_loopback_port():
    text = COMPOSE_PATH.read_text()
    blocks = _service_blocks(text)

    assert _list(blocks["dashboard"], "    ports:") == ["127.0.0.1:8652:8050"]
    for name in ("session-init", "btc-demo", "usdt-demo"):
        assert "\n    ports:" not in blocks[name]
    assert "privileged:" not in text
    assert "network_mode: host" not in text
    assert "/var/run/docker.sock" not in text
    assert "seccomp:unconfined" not in text


def test_stack_uses_one_named_bridge_with_outbound_egress():
    text = COMPOSE_PATH.read_text()
    network_text = text.rsplit("\nnetworks:\n", 1)[1]
    common_text = text.split("\nx-runtime-common:", 1)[1].split("\nservices:\n", 1)[0]

    assert _list(common_text, "  networks:") == ["limit-order-book-internal"]
    assert re.findall(r"(?m)^  ([a-z0-9-]+):$", network_text) == [
        "limit-order-book-internal"
    ]
    assert "name: limit-order-book-internal" in network_text
    assert "driver: bridge" in network_text
    assert not re.search(r"(?m)^\s+internal:", network_text)


def test_runtime_hardening_resources_and_restart_policies_are_bounded():
    text = COMPOSE_PATH.read_text()
    blocks = _service_blocks(text)
    common_text = text.split("\nx-runtime-common:", 1)[1].split("\nservices:\n", 1)[0]

    for setting in (
        "read_only: true",
        "init: true",
        "/tmp:rw,noexec,nosuid,size=64m",
        "driver: json-file",
        'max-size: "10m"',
        'max-file: "3"',
        "no-new-privileges:true",
    ):
        assert setting in common_text
    assert "read_only: false" not in text
    assert "init: false" not in text

    expected = {
        "session-init": ("no", None, 64, 64),
        "btc-demo": ("unless-stopped", "30s", 128, 160),
        "usdt-demo": ("unless-stopped", "30s", 128, 160),
        "dashboard": ("unless-stopped", "20s", 128, 128),
    }
    for name, (restart, grace, pids, memory) in expected.items():
        block = blocks[name]
        assert "<<: *runtime-common" in block
        assert f"restart: {restart}" in block or f'restart: "{restart}"' in block
        if grace:
            assert f"stop_grace_period: {grace}" in block
        assert f"pids_limit: {pids}" in block
        assert re.search(r"(?m)^    cpus: \"[0-9.]+\"$", block)
        assert f"mem_limit: {memory}m" in block
        assert f"memory: {memory}M" in block
        assert f"pids: {pids}" in block
        assert "healthcheck:" in block

    long_running_memory_mib = sum(expected[name][3] for name in SERVICES[1:])
    assert long_running_memory_mib <= 448


def test_mounts_are_limited_to_separate_results_and_state_roots():
    blocks = _service_blocks(COMPOSE_PATH.read_text())

    assert _list(blocks["session-init"], "    volumes:") == [
        "../runtime/state:/app/runtime/state"
    ]
    for name in ("btc-demo", "usdt-demo"):
        assert _list(blocks[name], "    volumes:") == [
            "../runtime/results:/app/runtime/results",
            "../runtime/state:/app/runtime/state",
        ]
        assert "/app/runtime/state/stack_session.json" in blocks[name]
    assert _list(blocks["dashboard"], "    volumes:") == [
        "../runtime/results:/app/runtime/results:ro"
    ]


def test_healthchecks_are_guarded_and_worker_health_requires_an_active_lock():
    blocks = _service_blocks(COMPOSE_PATH.read_text())

    for block in blocks.values():
        health_argv = _list(block, "      test:")
        assert health_argv[:4] == ["CMD", "python", "demo_entrypoint.py", "python"]
        assert "CMD-SHELL" not in block

    for name, lock_name in (
        ("btc-demo", ".worker_btcirt.lock"),
        ("usdt-demo", ".worker_usdtirt.lock"),
    ):
        block = blocks[name]
        assert lock_name in block
        assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in block
        assert "except BlockingIOError" in block
        assert 'load_stack_session("/app/runtime/state/stack_session.json")' in block
        assert "worker lease is not held" in block
        assert "test -s" not in block


def test_demo_environment_contains_no_sensitive_names_or_values():
    deployment_text = ENV_PATH.read_text() + COMPOSE_PATH.read_text()
    for name in SENSITIVE_ENV:
        assert name not in deployment_text


def test_sarkhatti_is_never_referenced_by_deployment_artifacts():
    for path in (ROOT / "deploy").rglob("*"):
        if path.is_file():
            assert "sarkhatti" not in path.read_text().lower()
