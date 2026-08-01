import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

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


def _rendered_compose() -> dict:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is unavailable")
    compose_version = subprocess.run(
        [docker, "compose", "version"], text=True, capture_output=True, check=False
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose CLI is unavailable")
    completed = subprocess.run(
        [
            docker,
            "compose",
            "-p",
            "limit-order-book",
            "-f",
            "deploy/docker-compose.production.yml",
            "--env-file",
            "deploy/demo.env",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


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
        "btc-demo": ("unless-stopped", "30s", 128, 128),
        "usdt-demo": ("unless-stopped", "30s", 128, 128),
        "dashboard": ("unless-stopped", "20s", 128, 192),
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


def test_demo_environment_is_the_exact_credential_empty_runtime_map():
    lines = [line for line in ENV_PATH.read_text().splitlines() if line]
    values = dict(line.split("=", 1) for line in lines)

    assert len(lines) == 4
    assert values == {
        "LOB_RESULTS_DIR": "/app/runtime/results",
        "LOB_STATE_DIR": "/app/runtime/state",
        "DEMO_SESSION_FILE": "/app/runtime/state/stack_session.json",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_rendered_compose_model_enforces_the_complete_deployment_contract():
    model = _rendered_compose()
    services = model["services"]

    assert model["name"] == "limit-order-book"
    assert set(services) == set(SERVICES)
    dashboard_ports = services["dashboard"]["ports"]
    assert len(dashboard_ports) == 1
    assert dashboard_ports[0]["mode"] == "ingress"
    assert dashboard_ports[0]["host_ip"] == "127.0.0.1"
    assert str(dashboard_ports[0]["published"]) == "8652"
    assert dashboard_ports[0]["target"] == 8050
    assert dashboard_ports[0]["protocol"] == "tcp"
    for name in ("session-init", "btc-demo", "usdt-demo"):
        assert "ports" not in services[name]

    assert set(model["networks"]) == {"limit-order-book-internal"}
    network = model["networks"]["limit-order-book-internal"]
    assert network["name"] == "limit-order-book-internal"
    assert network["driver"] == "bridge"
    assert network.get("internal", False) is False
    for service in services.values():
        assert set(service["networks"]) == {"limit-order-book-internal"}

    expected_volumes = {
        "session-init": [
            (ROOT / "runtime/state", "/app/runtime/state", False),
        ],
        "btc-demo": [
            (ROOT / "runtime/results", "/app/runtime/results", False),
            (ROOT / "runtime/state", "/app/runtime/state", False),
        ],
        "usdt-demo": [
            (ROOT / "runtime/results", "/app/runtime/results", False),
            (ROOT / "runtime/state", "/app/runtime/state", False),
        ],
        "dashboard": [
            (ROOT / "runtime/results", "/app/runtime/results", True),
        ],
    }
    for name, expected in expected_volumes.items():
        actual = [
            (Path(volume["source"]), volume["target"], volume.get("read_only", False))
            for volume in services[name]["volumes"]
        ]
        assert actual == expected

    limits = {
        "session-init": (0.25, 64, 64, "no", None, "10s", "5s", "2s"),
        "btc-demo": (0.5, 128, 128, "unless-stopped", "30s", "30s", "10s", "20s"),
        "usdt-demo": (0.5, 128, 128, "unless-stopped", "30s", "30s", "10s", "20s"),
        "dashboard": (0.5, 192, 128, "unless-stopped", "20s", "30s", "10s", "20s"),
    }
    for name, values in limits.items():
        cpus, memory_mib, pids, restart, grace, interval, timeout, start_period = values
        service = services[name]
        memory_bytes = memory_mib * 1024 * 1024
        assert service["read_only"] is True
        assert service["init"] is True
        assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }
        assert service["cpus"] == cpus
        assert int(service["mem_limit"]) == memory_bytes
        assert service["pids_limit"] == pids
        deploy_limits = service["deploy"]["resources"]["limits"]
        assert deploy_limits["cpus"] == cpus
        assert int(deploy_limits["memory"]) == memory_bytes
        assert deploy_limits["pids"] == pids
        assert service["restart"] == restart
        if grace is None:
            assert "stop_grace_period" not in service
        else:
            assert service["stop_grace_period"] == grace
        health = service["healthcheck"]
        assert health["test"][:4] == ["CMD", "python", "demo_entrypoint.py", "python"]
        assert health["retries"] == 3
        assert health["interval"] == interval
        assert health["timeout"] == timeout
        assert health["start_period"] == start_period

    assert services["session-init"]["healthcheck"]["test"] == [
        "CMD", "python", "demo_entrypoint.py", "python", "session.py", "load",
        "--path", "/app/runtime/state/stack_session.json",
    ]
    assert services["dashboard"]["healthcheck"]["test"] == [
        "CMD", "python", "demo_entrypoint.py", "python", "-c",
        "from urllib.request import urlopen; response = "
        "urlopen('http://127.0.0.1:8050/', timeout=3); assert response.status == 200",
    ]
    for name, lock_name in (
        ("btc-demo", ".worker_btcirt.lock"),
        ("usdt-demo", ".worker_usdtirt.lock"),
    ):
        health_test = services[name]["healthcheck"]["test"]
        assert health_test[:5] == ["CMD", "python", "demo_entrypoint.py", "python", "-c"]
        assert lock_name in health_test[5]
        assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in health_test[5]
        assert 'load_stack_session("/app/runtime/state/stack_session.json")' in health_test[5]
        assert 'SystemExit("worker lease is not held")' in health_test[5]

    assert sum(limits[name][1] for name in SERVICES[1:]) == 448


def test_sarkhatti_is_never_referenced_by_deployment_artifacts():
    for path in (ROOT / "deploy").rglob("*"):
        if path.is_file():
            assert "sarkhatti" not in path.read_text().lower()


def test_nginx_bootstrap_only_serves_the_acme_challenge():
    bootstrap = (ROOT / "deploy/nginx/lob-dashboard-http.conf").read_text()

    assert "listen 80;" in bootstrap
    assert "server_name dashboard.185-206-95-141.nip.io;" in bootstrap
    assert "location /.well-known/acme-challenge/" in bootstrap
    assert "root /var/www/lob-dashboard-acme;" in bootstrap
    assert "try_files $uri =404;" in bootstrap
    assert "location /" in bootstrap
    assert "return 404;" in bootstrap
    assert "proxy_pass" not in bootstrap


def test_nginx_final_site_is_an_authenticated_loopback_proxy():
    https = (ROOT / "deploy/nginx/lob-dashboard-https.conf").read_text()

    assert https.count("server_name dashboard.185-206-95-141.nip.io;") == 2
    assert https.count("listen 80;") == 1
    assert https.count("listen 443 ssl http2;") == 1
    assert "return 301 https://$host$request_uri;" in https
    assert "location /.well-known/acme-challenge/" in https
    assert "root /var/www/lob-dashboard-acme;" in https
    assert "try_files $uri =404;" in https
    assert (
        "ssl_certificate /etc/letsencrypt/live/dashboard.185-206-95-141.nip.io/"
        "fullchain.pem;"
    ) in https
    assert (
        "ssl_certificate_key /etc/letsencrypt/live/dashboard.185-206-95-141.nip.io/"
        "privkey.pem;"
    ) in https
    assert 'auth_basic "Limit Order Book Dashboard";' in https
    assert "auth_basic_user_file /etc/nginx/lob-dashboard.htpasswd;" in https
    for header in (
        'add_header X-Content-Type-Options "nosniff" always;',
        'add_header X-Frame-Options "DENY" always;',
        'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        'add_header Strict-Transport-Security "max-age=31536000" always;',
    ):
        assert header in https
    for setting in (
        "proxy_pass http://127.0.0.1:8652;",
        "proxy_http_version 1.1;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header Upgrade $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_buffering off;",
    ):
        assert setting in https
    assert "8642" not in https and "6080" not in https


def test_rollback_is_explicitly_project_scoped_and_fail_closed():
    rollback = (ROOT / "deploy/rollback.sh").read_text()

    assert "set -euo pipefail" in rollback
    assert 'EXPECTED_APP_DIR="/home/ubuntu/limit-order-book"' in rollback
    assert 'APP_DIR="$(realpath -e -- "$EXPECTED_APP_DIR")"' in rollback
    assert '[[ "$APP_DIR" == "$EXPECTED_APP_DIR" ]]' in rollback
    assert (
        "docker compose -p limit-order-book -f deploy/docker-compose.production.yml "
        "--env-file deploy/demo.env down"
    ) in rollback
    assert 'AVAILABLE_SITE="/etc/nginx/sites-available/lob-dashboard"' in rollback
    assert 'ENABLED_SITE="/etc/nginx/sites-enabled/lob-dashboard"' in rollback
    assert 'AUTH_FILE="/etc/nginx/lob-dashboard.htpasswd"' in rollback
    assert '[[ -L "$ENABLED_SITE" ]]' in rollback
    assert 'readlink -- "$ENABLED_SITE"' in rollback
    assert '[[ "$(readlink -- "$ENABLED_SITE")" == "$AVAILABLE_SITE" ]]' in rollback
    assert "targets_to_remove=()" in rollback
    assert 'if [[ -e "$AVAILABLE_SITE" || -L "$AVAILABLE_SITE" ]]; then' in rollback
    assert 'if [[ -e "$ENABLED_SITE" || -L "$ENABLED_SITE" ]]; then' in rollback
    assert 'if [[ -e "$AUTH_FILE" || -L "$AUTH_FILE" ]]; then' in rollback
    assert '[[ ! -L "$AVAILABLE_SITE" && -f "$AVAILABLE_SITE" ]]' in rollback
    assert '[[ ! -L "$AUTH_FILE" && -f "$AUTH_FILE" ]]' in rollback
    assert 'targets_to_remove+=("$AVAILABLE_SITE")' in rollback
    assert 'targets_to_remove+=("$ENABLED_SITE")' in rollback
    assert 'targets_to_remove+=("$AUTH_FILE")' in rollback
    assert 'sudo rm -- "${targets_to_remove[@]}"' in rollback
    assert "sudo nginx -t" in rollback
    assert "sudo systemctl reload nginx" in rollback
    assert (
        "sudo certbot delete --cert-name dashboard.185-206-95-141.nip.io"
    ) in rollback
    for forbidden in (
        "docker system prune",
        "docker volume prune",
        "docker network prune",
        "docker container prune",
        "rm -r",
        "rm -R",
        "rm -rf",
        "find ",
        "xargs",
    ):
        assert forbidden not in rollback
