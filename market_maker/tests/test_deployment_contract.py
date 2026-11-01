import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
from dotenv import dotenv_values
from werkzeug.security import check_password_hash

from demo_entrypoint import SENSITIVE_ENV


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "deploy/docker-compose.production.yml"
LOCAL_COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
ENV_PATH = ROOT / "deploy/demo.env"
DEMO_SERVICES = ("btc-demo", "usdt-demo", "eth-demo", "xaut-demo", "trx-demo")
SERVICES = ("session-init",) + DEMO_SERVICES + ("dashboard", "admin")
CONTAINER_NAMES = {
    "session-init": "lob-session-init",
    "btc-demo": "lob-btc-demo",
    "usdt-demo": "lob-usdt-demo",
    "eth-demo": "lob-eth-demo",
    "xaut-demo": "lob-xaut-demo",
    "trx-demo": "lob-trx-demo",
    "dashboard": "lob-dashboard",
    "admin": "lob-admin",
}
COMMANDS = {
    "session-init": [
        "python", "demo_entrypoint.py", "python", "session.py", "stack-init",
        "--path", "/app/runtime/state/stack_session.json",
        "--worker-lock", "/app/runtime/state/.worker_btcirt.lock",
        "--worker-lock", "/app/runtime/state/.worker_usdtirt.lock",
        "--worker-lock", "/app/runtime/state/.worker_ethirt.lock",
        "--worker-lock", "/app/runtime/state/.worker_xautirt.lock",
        "--worker-lock", "/app/runtime/state/.worker_trxirt.lock",
    ],
    "btc-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "btc",
        "--mode", "demo", "--continuous",
    ],
    "usdt-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "usdt",
        "--mode", "demo", "--continuous",
    ],
    "eth-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "eth",
        "--mode", "demo", "--continuous",
    ],
    "xaut-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "xaut",
        "--mode", "demo", "--continuous",
    ],
    "trx-demo": [
        "python", "demo_entrypoint.py", "python", "run.py", "--profile", "trx",
        "--mode", "demo", "--continuous",
    ],
    "dashboard": ["python", "demo_entrypoint.py", "python", "dashboard.py"],
    "admin": ["python", "demo_entrypoint.py", "python", "admin_app.py"],
}


def _service_blocks(text: str) -> dict[str, str]:
    services_text = text.split("\nservices:\n", 1)[1]
    if "\nnetworks:\n" in services_text:
        services_text = services_text.split("\nnetworks:\n", 1)[0]
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


def _rendered_compose(extra_env=None) -> dict:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is unavailable")
    compose_version = subprocess.run(
        [docker, "compose", "version"], text=True, capture_output=True, check=False
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose CLI is unavailable")
    env = os.environ.copy()
    env.pop("LOB_REVISION", None)
    env.pop("LOB_BUILD_DATE", None)
    if extra_env:
        env.update(extra_env)
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
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_image_build_provenance_is_resolved_and_stamped_as_oci_labels():
    revision = "test-revision-not-a-hardcoded-commit"
    build_date = "2026-08-01T13:59:00Z"
    model = _rendered_compose({
        "LOB_REVISION": revision,
        "LOB_BUILD_DATE": build_date,
    })

    assert model["services"]["session-init"]["build"]["args"] == {
        "LOB_BUILD_DATE": build_date,
        "LOB_REVISION": revision,
    }

    dockerfile = DOCKERFILE_PATH.read_text()
    assert "ARG LOB_REVISION=uncommitted" in dockerfile
    assert "ARG LOB_BUILD_DATE=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${LOB_REVISION}"' in dockerfile
    assert 'org.opencontainers.image.created="${LOB_BUILD_DATE}"' in dockerfile
    provenance_start = dockerfile.index("ARG LOB_REVISION=uncommitted")
    assert provenance_start > dockerfile.index(
        "RUN pip install --no-cache-dir -r requirements.txt"
    )
    assert provenance_start > dockerfile.index("COPY market_maker/ ./market_maker/")


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


def test_dashboard_and_admin_are_the_only_services_with_loopback_ports():
    text = COMPOSE_PATH.read_text()
    blocks = _service_blocks(text)

    assert _list(blocks["dashboard"], "    ports:") == ["127.0.0.1:8652:8050"]
    assert _list(blocks["admin"], "    ports:") == ["127.0.0.1:8653:8051"]
    for name in ("session-init",) + DEMO_SERVICES:
        assert "\n    ports:" not in blocks[name]
    assert "privileged:" not in text
    assert "network_mode: host" not in text
    assert "/var/run/docker.sock" not in text
    assert "seccomp:unconfined" not in text
    # No new *public* exposure: both loopback ports are 127.0.0.1-only.
    for name in ("dashboard", "admin"):
        for port_line in _list(blocks[name], "    ports:"):
            assert port_line.startswith("127.0.0.1:")


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
        'user: "1000:1000"',
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
        # Workers: the per-round store work was measured at ~90 MB after the
        # 2026-10-11 fix; the UI containers decode result rows into pandas
        # object frames for every page and were OOM-killed at 192 MB on a
        # 5,700-row store, so they get 384 MB.
        "btc-demo": ("unless-stopped", "30s", 128, 320),
        "usdt-demo": ("unless-stopped", "30s", 128, 320),
        "eth-demo": ("unless-stopped", "30s", 128, 320),
        "xaut-demo": ("unless-stopped", "30s", 128, 320),
        "trx-demo": ("unless-stopped", "30s", 128, 320),
        "dashboard": ("unless-stopped", "20s", 128, 384),
        "admin": ("unless-stopped", "20s", 128, 384),
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

    # Five demo workers (1 November 2026) plus the two UI containers on a
    # 4 GB host: the limits add up to 2.4 GB; the workers use 80–210 MB each.
    long_running_memory_mib = sum(expected[name][3] for name in SERVICES[1:])
    assert long_running_memory_mib <= 2432


def test_mounts_are_limited_to_separate_results_and_state_roots():
    blocks = _service_blocks(COMPOSE_PATH.read_text())

    assert _list(blocks["session-init"], "    volumes:") == [
        "../runtime/state:/app/runtime/state"
    ]
    for name in DEMO_SERVICES:
        assert _list(blocks[name], "    volumes:") == [
            "../runtime/results:/app/runtime/results",
            "../runtime/state:/app/runtime/state",
        ]
        assert "/app/runtime/state/stack_session.json" in blocks[name]
    assert _list(blocks["dashboard"], "    volumes:") == [
        "../runtime/results:/app/runtime/results:ro",
        "../runtime/state:/app/runtime/state:ro",
        "/var/lib/market-maker-forward/results:/app/runtime/forward/results:ro",
    ]
    assert _list(blocks["admin"], "    volumes:") == [
        "../runtime/state:/app/runtime/state:ro",
        "../runtime/results:/app/runtime/results:ro",
        "../runtime/backups:/app/runtime/backups:ro",
        "../runtime/control:/app/runtime/control",
        "/var/lib/market-maker-forward/control:/app/runtime/forward/control",
        "/var/lib/market-maker-forward/config:/app/runtime/forward/config",
        "/var/lib/market-maker-forward/locks:/app/runtime/forward/locks:ro",
        "/var/lib/market-maker-forward/results:/app/runtime/forward/results:ro",
    ]

    local_blocks = _service_blocks(LOCAL_COMPOSE_PATH.read_text())
    assert _list(local_blocks["dashboard"], "    volumes:") == [
        "./market_maker/results:/app/market_maker/results:ro"
    ]
    assert _list(local_blocks["admin"], "    volumes:") == [
        "./market_maker/results:/app/market_maker/results:ro",
        "./market_maker/results/control:/app/market_maker/results/control",
    ]
    for web_service in ("dashboard", "admin"):
        assert "/var/run/docker.sock" not in blocks[web_service]
        if web_service in local_blocks:
            assert "/var/run/docker.sock" not in local_blocks[web_service]


def test_healthchecks_are_guarded_and_worker_health_requires_an_active_lock():
    blocks = _service_blocks(COMPOSE_PATH.read_text())

    for block in blocks.values():
        health_argv = _list(block, "      test:")
        assert health_argv[:4] == ["CMD", "python", "demo_entrypoint.py", "python"]
        assert "CMD-SHELL" not in block

    for name, lock_name in (
        (service, f".worker_{service[:-len('-demo')]}irt.lock") for service in DEMO_SERVICES
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

    assert len(lines) == 7
    assert values == {
        "LOB_RESULTS_DIR": "/app/runtime/results",
        "LOB_STATE_DIR": "/app/runtime/state",
        "DEMO_SESSION_FILE": "/app/runtime/state/stack_session.json",
        "LOB_CONTROL_DIR": "/app/runtime/control",
        "LOB_BACKUPS_DIR": "/app/runtime/backups",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LOB_TELEGRAM_DISABLED": "1",
    }


def test_local_env_example_has_working_admin_admin_without_blank_session_key():
    values = dotenv_values(ROOT / "market_maker/.env.example")

    assert values["ADMIN_USERNAME"] == "admin"
    assert check_password_hash(values["ADMIN_PASSWORD_HASH"], "admin")
    assert "ADMIN_SESSION_SECRET" not in values


def test_local_admin_is_loopback_only_and_receives_no_trading_secrets():
    blocks = _service_blocks(LOCAL_COMPOSE_PATH.read_text())
    admin = blocks["admin"]

    assert _list(admin, "    ports:") == ["127.0.0.1:8051:8051"]
    assert "\n    env_file:" not in admin
    assert "ADMIN_USERNAME: admin" in admin
    assert "ADMIN_PASSWORD_HASH:" in admin
    assert "ADMIN_SESSION_SECRET:" in admin
    for sensitive_name in SENSITIVE_ENV:
        assert sensitive_name not in admin


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
    admin_ports = services["admin"]["ports"]
    assert len(admin_ports) == 1
    assert admin_ports[0]["mode"] == "ingress"
    assert admin_ports[0]["host_ip"] == "127.0.0.1"
    assert str(admin_ports[0]["published"]) == "8653"
    assert admin_ports[0]["target"] == 8051
    assert admin_ports[0]["protocol"] == "tcp"
    for name in ("session-init",) + DEMO_SERVICES:
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
        "eth-demo": [
            (ROOT / "runtime/results", "/app/runtime/results", False),
            (ROOT / "runtime/state", "/app/runtime/state", False),
        ],
        "xaut-demo": [
            (ROOT / "runtime/results", "/app/runtime/results", False),
            (ROOT / "runtime/state", "/app/runtime/state", False),
        ],
        "trx-demo": [
            (ROOT / "runtime/results", "/app/runtime/results", False),
            (ROOT / "runtime/state", "/app/runtime/state", False),
        ],
        "dashboard": [
            (ROOT / "runtime/results", "/app/runtime/results", True),
            (ROOT / "runtime/state", "/app/runtime/state", True),
        ],
        "admin": [
            (ROOT / "runtime/state", "/app/runtime/state", True),
            (ROOT / "runtime/results", "/app/runtime/results", True),
            (ROOT / "runtime/backups", "/app/runtime/backups", True),
            (ROOT / "runtime/control", "/app/runtime/control", False),
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
        "eth-demo": (0.5, 128, 128, "unless-stopped", "30s", "30s", "10s", "20s"),
        "xaut-demo": (0.5, 128, 128, "unless-stopped", "30s", "30s", "10s", "20s"),
        "trx-demo": (0.5, 128, 128, "unless-stopped", "30s", "30s", "10s", "20s"),
        "dashboard": (0.5, 192, 128, "unless-stopped", "20s", "30s", "10s", "20s"),
        "admin": (0.5, 192, 128, "unless-stopped", "20s", "30s", "10s", "20s"),
    }
    for name, values in limits.items():
        cpus, memory_mib, pids, restart, grace, interval, timeout, start_period = values
        service = services[name]
        assert service["user"] == "1000:1000"
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
    assert services["admin"]["healthcheck"]["test"] == [
        "CMD", "python", "demo_entrypoint.py", "python", "-c",
        "from urllib.request import urlopen; response = "
        "urlopen('http://127.0.0.1:8051/admin/health', timeout=3); assert response.status == 200",
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

    assert sum(limits[name][1] for name in SERVICES[1:]) == 640


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
    assert 'HASH_CONF="/etc/nginx/conf.d/lob-server-names-hash.conf"' in rollback
    assert '[[ -L "$ENABLED_SITE" ]]' in rollback
    assert 'readlink -- "$ENABLED_SITE"' in rollback
    assert '[[ "$(readlink -- "$ENABLED_SITE")" == "$AVAILABLE_SITE" ]]' in rollback
    assert "targets_to_remove=()" in rollback
    assert 'if [[ -e "$AVAILABLE_SITE" || -L "$AVAILABLE_SITE" ]]; then' in rollback
    assert 'if [[ -e "$ENABLED_SITE" || -L "$ENABLED_SITE" ]]; then' in rollback
    assert 'if [[ -e "$AUTH_FILE" || -L "$AUTH_FILE" ]]; then' in rollback
    assert 'if [[ -e "$HASH_CONF" || -L "$HASH_CONF" ]]; then' in rollback
    assert '[[ ! -L "$AVAILABLE_SITE" && -f "$AVAILABLE_SITE" ]]' in rollback
    assert '[[ ! -L "$AUTH_FILE" && -f "$AUTH_FILE" ]]' in rollback
    assert 'targets_to_remove+=("$AVAILABLE_SITE")' in rollback
    assert 'targets_to_remove+=("$ENABLED_SITE")' in rollback
    assert 'targets_to_remove+=("$AUTH_FILE")' in rollback
    assert 'targets_to_remove+=("$HASH_CONF")' in rollback
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


def test_admin_env_example_has_no_real_secrets_committed():
    example = (ROOT / "deploy/admin.env.example").read_text()
    assert "ADMIN_USERNAME=" in example
    assert "ADMIN_PASSWORD_HASH=replace-with-generated-hash" in example
    assert "ADMIN_SESSION_SECRET=replace-with-generated-secret" in example
    gitignore = (ROOT / ".gitignore").read_text()
    assert "deploy/admin.env" in gitignore.splitlines()


def test_admin_service_has_no_docker_socket_or_shell_exec():
    block = _service_blocks(COMPOSE_PATH.read_text())["admin"]
    assert "/var/run/docker.sock" not in block
    assert "CMD-SHELL" not in block
    assert _list(block, "    command:") == COMMANDS["admin"]


def test_control_agent_systemd_unit_runs_a_fixed_argv_no_shell():
    unit = (ROOT / "deploy/systemd/lob-control-agent.service").read_text()
    assert "ExecStart=/usr/bin/python3 /home/ubuntu/limit-order-book/deploy/control_agent.py" in unit
    assert "ExecStart=/bin/sh" not in unit
    assert "ExecStart=/bin/bash" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "User=ubuntu" in unit
