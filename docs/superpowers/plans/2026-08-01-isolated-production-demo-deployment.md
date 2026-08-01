# Isolated Production Demo Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the Limit Order Book Demo/Paper workers and Dash dashboard on the Sarkhatti Ubuntu host with strict application, Docker, filesystem, Git, Nginx, TLS, authentication, and rollback isolation.

**Architecture:** Add a fail-closed process entrypoint and environment-controlled runtime roots, then run the existing application in a dedicated Compose project with one internal network and one loopback dashboard port. Promote the tested commit from the current `parsaoryani` branch to `production`, transfer that exact commit, and expose it through an independent authenticated HTTPS Nginx site while continuously comparing Sarkhatti to its immutable baseline.

**Tech Stack:** Python 3.11, pytest, Docker Engine 29, Docker Compose 2.40, Dash, Nginx 1.24, Certbot/Let's Encrypt, Bash, Git.

## Global Constraints

- Never modify, recreate, restart, stop, or share state with Sarkhatti.
- Never check out, modify, merge, reset, rebase, commit to, or push `main`.
- Work only on `parsaoryani/algo1-refactor-testability` and `production`.
- Never run `docker system prune`, `docker volume prune`, or `docker network prune`.
- Never publish a Docker port except `127.0.0.1:8652:8050`.
- Never provide exchange, wallet, Telegram, or MySQL credentials to the Demo stack.
- Never use Chromium, VNC, Xvfb, Playwright, privileged containers, Docker socket mounts, host networking, or `seccomp:unconfined`.
- Keep normal Limit Order Book container memory limits at or below 448 MiB and observed total usage below 512 MiB.
- Preserve `.docker-validation-results-pre-cancel-fix-20260801T1148Z/` untouched and outside the image/archive.
- Run `nginx -t` before every Nginx reload and never restart Nginx unless reload is proven impossible.
- Do not claim long-term readiness from the bounded observation run.

## File structure

- `market_maker/demo_entrypoint.py`: fail-closed command/environment/runtime-root validation, then `exec` the approved process.
- `market_maker/tests/test_demo_entrypoint.py`: unit tests for command, credential, MySQL, and path rejection.
- `market_maker/config.py`: environment-controlled result and Demo state roots.
- `market_maker/run.py`: place worker leases in the Demo state root.
- `market_maker/tests/test_runtime_paths.py`: prove result/state separation and safe defaults.
- `deploy/docker-compose.production.yml`: isolated production-operated Demo stack.
- `deploy/demo.env`: committed credential-empty environment contract.
- `deploy/nginx/lob-dashboard-http.conf`: ACME-only HTTP bootstrap site.
- `deploy/nginx/lob-dashboard-https.conf`: final authenticated HTTPS proxy site.
- `deploy/rollback.sh`: exact-target, project-scoped rollback with path checks.
- `market_maker/tests/test_deployment_contract.py`: static and Compose-model assertions for isolation and forbidden settings.
- `.dockerignore`: exclude all local runtime/evidence/deployment secrets from the image context while retaining required runtime source.
- `docs/deployment/DEPLOYMENT_PROGRESS.md`: append-only operational evidence and exact next step.

---

### Task 1: Fail-closed Demo process entrypoint

**Files:**
- Create: `market_maker/demo_entrypoint.py`
- Create: `market_maker/tests/test_demo_entrypoint.py`

**Interfaces:**
- Consumes: process argv, an environment mapping, and allowed roots `/app/runtime/results` and `/app/runtime/state`.
- Produces: `validate_demo_startup(command: Sequence[str], environ: Mapping[str, str]) -> None` and `main() -> NoReturn`.

- [ ] **Step 1: Write failing command and credential tests**

```python
import pytest

from demo_entrypoint import DemoStartupError, validate_demo_startup


SAFE_WORKER = ["python", "run.py", "--profile", "btc", "--mode", "demo", "--continuous"]


def test_safe_demo_worker_is_accepted():
    validate_demo_startup(SAFE_WORKER, {})


@pytest.mark.parametrize("command", [
    ["python", "run.py", "--profile", "btc", "--mode", "real"],
    ["python", "run.py", "--profile", "btc"],
])
def test_worker_must_explicitly_select_demo(command):
    with pytest.raises(DemoStartupError, match="--mode demo"):
        validate_demo_startup(command, {})


@pytest.mark.parametrize("name", [
    "NOBITEX_API_TOKEN", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
    "MYSQL_DATABASE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
])
def test_sensitive_environment_must_be_empty(name):
    with pytest.raises(DemoStartupError, match=name):
        validate_demo_startup(SAFE_WORKER, {name: "forbidden"})
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest -q market_maker/tests/test_demo_entrypoint.py`

Expected: collection error because `demo_entrypoint` does not exist.

- [ ] **Step 3: Add path-rejection tests**

```python
@pytest.mark.parametrize("name,value", [
    ("LOB_RESULTS_DIR", "/home/ubuntu/sarkhatti/data"),
    ("LOB_STATE_DIR", "/app/market_maker/results"),
    ("DEMO_SESSION_FILE", "/tmp/stack_session.json"),
])
def test_runtime_paths_must_stay_in_dedicated_roots(name, value):
    with pytest.raises(DemoStartupError, match=name):
        validate_demo_startup(SAFE_WORKER, {name: value})
```

- [ ] **Step 4: Implement the minimal validator and exec wrapper**

```python
"""Fail-closed entrypoint for the production-operated Demo stack."""

from __future__ import annotations

import os
import sys
from pathlib import PurePosixPath
from typing import Mapping, NoReturn, Sequence


class DemoStartupError(RuntimeError):
    pass


SENSITIVE_ENV = (
    "NOBITEX_API_TOKEN", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
    "MYSQL_DATABASE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)
ALLOWED_PATHS = {
    "LOB_RESULTS_DIR": PurePosixPath("/app/runtime/results"),
    "LOB_STATE_DIR": PurePosixPath("/app/runtime/state"),
    "DEMO_SESSION_FILE": PurePosixPath("/app/runtime/state"),
}


def _is_within(value: str, root: PurePosixPath) -> bool:
    path = PurePosixPath(value)
    return path == root or root in path.parents


def validate_demo_startup(command: Sequence[str], environ: Mapping[str, str]) -> None:
    if not command:
        raise DemoStartupError("an explicit command is required")
    if "run.py" in command:
        pairs = list(zip(command, command[1:]))
        if ("--mode", "demo") not in pairs or ("--mode", "real") in pairs:
            raise DemoStartupError("worker command must explicitly contain --mode demo")
    for name in SENSITIVE_ENV:
        if environ.get(name, "").strip():
            raise DemoStartupError(f"{name} must be empty in Demo deployment")
    for name, root in ALLOWED_PATHS.items():
        value = environ.get(name)
        if value and not _is_within(value, root):
            raise DemoStartupError(f"{name} must remain under {root}")


def main() -> NoReturn:
    command = sys.argv[1:]
    validate_demo_startup(command, os.environ)
    print("DEMO_STARTUP_GUARD_OK real_operations=disabled", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Verify GREEN and existing zero-call tests**

Run: `pytest -q market_maker/tests/test_demo_entrypoint.py market_maker/tests/test_demo_safety.py market_maker/tests/test_demo_end_to_end.py`

Expected: all tests pass; the end-to-end assertion reports the exact zero-count dictionary.

- [ ] **Step 6: Commit the entrypoint**

```bash
git add market_maker/demo_entrypoint.py market_maker/tests/test_demo_entrypoint.py
git commit -m "feat: fail closed at demo deployment startup"
```

### Task 2: Separate runtime result and state roots

**Files:**
- Modify: `market_maker/config.py`
- Modify: `market_maker/run.py`
- Create: `market_maker/tests/test_runtime_paths.py`

**Interfaces:**
- Produces: `RESULTS_DIR`, `DEMO_STATE_DIR`, and `demo_state_path(filename: str) -> str`.
- Consumers: profile output paths, `make_cfg`, worker lease paths, Compose environment.

- [ ] **Step 1: Write failing environment-root tests**

```python
import importlib
from pathlib import Path


def test_runtime_roots_can_be_isolated_by_environment(monkeypatch):
    monkeypatch.setenv("LOB_RESULTS_DIR", "/app/runtime/results")
    monkeypatch.setenv("LOB_STATE_DIR", "/app/runtime/state")
    import config
    importlib.reload(config)
    assert Path(config.RESULTS_DIR) == Path("/app/runtime/results")
    assert Path(config.DEMO_STATE_DIR) == Path("/app/runtime/state")
    assert Path(config.demo_state_path("paper.json")) == Path("/app/runtime/state/paper.json")
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q market_maker/tests/test_runtime_paths.py`

Expected: failure because `DEMO_STATE_DIR` and `demo_state_path` are missing.

- [ ] **Step 3: Implement the runtime roots**

In `config.py`, replace the fixed result constant and add:

```python
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.environ.get("LOB_RESULTS_DIR", os.path.join(_PACKAGE_DIR, "results"))
DEMO_STATE_DIR = os.environ.get("LOB_STATE_DIR", RESULTS_DIR)


def demo_state_path(filename: str) -> str:
    return os.path.join(DEMO_STATE_DIR, filename)
```

In `run.py`, import `DEMO_STATE_DIR` and `demo_state_path`, use `demo_state_path(...)` for paper/fee/archive files, and place `.worker_<pair>.lock` under `DEMO_STATE_DIR`.

- [ ] **Step 4: Add and run an exact `make_cfg` separation test**

```python
def test_make_cfg_writes_outputs_and_state_to_separate_roots(monkeypatch):
    monkeypatch.setenv("LOB_RESULTS_DIR", "/app/runtime/results")
    monkeypatch.setenv("LOB_STATE_DIR", "/app/runtime/state")
    import config, run
    importlib.reload(config)
    importlib.reload(run)
    cfg = run.make_cfg("btc", "demo", session_id="shared")
    assert Path(cfg.output_csv).parent == Path("/app/runtime/results")
    assert Path(cfg.demo_state_path).parent == Path("/app/runtime/state")
    assert Path(cfg.demo_fee_state_path).parent == Path("/app/runtime/state")
    assert Path(cfg.demo_archive_path).parent == Path("/app/runtime/state")
```

Run: `pytest -q market_maker/tests/test_runtime_paths.py market_maker/tests/test_run_make_cfg.py market_maker/tests/test_shared_session.py`

Expected: all pass.

- [ ] **Step 5: Commit runtime isolation**

```bash
git add market_maker/config.py market_maker/run.py market_maker/tests/test_runtime_paths.py
git commit -m "feat: isolate demo runtime state from results"
```

### Task 3: Production Compose and deployment-contract tests

**Files:**
- Create: `deploy/docker-compose.production.yml`
- Create: `deploy/demo.env`
- Create: `market_maker/tests/test_deployment_contract.py`
- Modify: `.dockerignore`

**Interfaces:**
- Compose project: explicit `name: limit-order-book` and `-p limit-order-book` validation.
- Published endpoint: `127.0.0.1:8652:8050` only.
- Runtime mounts: `../runtime/results:/app/runtime/results` and `../runtime/state:/app/runtime/state`.

- [ ] **Step 1: Write failing artifact-contract tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_has_only_loopback_dashboard_publication():
    text = (ROOT / "deploy/docker-compose.production.yml").read_text()
    assert '"127.0.0.1:8652:8050"' in text
    assert '"8050:8050"' not in text
    assert "privileged:" not in text
    assert "network_mode: host" not in text
    assert "/var/run/docker.sock" not in text
    assert "seccomp:unconfined" not in text


def test_demo_environment_contains_no_sensitive_names_or_values():
    text = (ROOT / "deploy/demo.env").read_text()
    for name in ("NOBITEX", "MYSQL", "TELEGRAM"):
        assert name not in text


def test_sarkhatti_is_never_referenced_by_deployment_artifacts():
    for path in (ROOT / "deploy").rglob("*"):
        if path.is_file():
            assert "sarkhatti" not in path.read_text().lower()
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q market_maker/tests/test_deployment_contract.py`

Expected: file-not-found failure.

- [ ] **Step 3: Create the credential-empty environment**

```dotenv
LOB_RESULTS_DIR=/app/runtime/results
LOB_STATE_DIR=/app/runtime/state
DEMO_SESSION_FILE=/app/runtime/state/stack_session.json
PYTHONDONTWRITEBYTECODE=1
```

- [ ] **Step 4: Create the isolated Compose model**

Define four services (`session-init`, `btc-demo`, `usdt-demo`, `dashboard`) with exact `container_name` values `lob-session-init`, `lob-btc-demo`, `lob-usdt-demo`, and `lob-dashboard`. Build one image from `..`, invoke every process through `python demo_entrypoint.py`, attach only `limit-order-book-internal`, mount only the dedicated result/state paths, set `read_only: true`, `init: true`, `/tmp` tmpfs, `pids_limit`, CPU and memory limits, capped JSON logs, health checks, and `restart: unless-stopped` for the three long-running services. The initializer uses `restart: "no"` and workers depend on its successful completion. Only Dash includes:

```yaml
ports:
  - "127.0.0.1:8652:8050"
```

- [ ] **Step 5: Tighten the build context**

Append exact exclusions for `.docker-validation-results*`, `runtime/`, `deploy/backups/`, `deploy/*.secret`, and all `.env` files except committed `deploy/demo.env` to `.dockerignore`.

- [ ] **Step 6: Validate tests and Compose rendering**

Run:

```bash
pytest -q market_maker/tests/test_deployment_contract.py
docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env config
```

Expected: tests pass; Compose renders four isolated services, one isolated network, and one loopback port.

- [ ] **Step 7: Commit Compose isolation**

```bash
git add .dockerignore deploy/docker-compose.production.yml deploy/demo.env market_maker/tests/test_deployment_contract.py
git commit -m "feat: add isolated production demo compose stack"
```

### Task 4: Nginx assets and exact rollback

**Files:**
- Create: `deploy/nginx/lob-dashboard-http.conf`
- Create: `deploy/nginx/lob-dashboard-https.conf`
- Create: `deploy/rollback.sh`
- Modify: `market_maker/tests/test_deployment_contract.py`

**Interfaces:**
- Hostname: `dashboard.185-206-95-141.sslip.io`.
- Upstream: `http://127.0.0.1:8652` only.
- Auth file: `/etc/nginx/lob-dashboard.htpasswd`.
- Certificate: `/etc/letsencrypt/live/dashboard.185-206-95-141.sslip.io/{fullchain,privkey}.pem`.

- [ ] **Step 1: Add failing Nginx/rollback contract tests**

```python
def test_nginx_is_a_separate_authenticated_loopback_proxy():
    https = (ROOT / "deploy/nginx/lob-dashboard-https.conf").read_text()
    assert "server_name dashboard.185-206-95-141.sslip.io;" in https
    assert "proxy_pass http://127.0.0.1:8652;" in https
    assert 'auth_basic "Limit Order Book Dashboard";' in https
    assert "auth_basic_user_file /etc/nginx/lob-dashboard.htpasswd;" in https
    assert "8642" not in https and "6080" not in https


def test_rollback_is_explicitly_project_scoped_and_has_no_prune():
    rollback = (ROOT / "deploy/rollback.sh").read_text()
    assert "docker compose -p limit-order-book" in rollback
    assert "docker system prune" not in rollback
    assert "docker volume prune" not in rollback
    assert "docker network prune" not in rollback
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q market_maker/tests/test_deployment_contract.py`

Expected: missing Nginx/rollback files.

- [ ] **Step 3: Create the HTTP bootstrap site**

Create a server block listening on 80 for only the dedicated hostname. Serve `/.well-known/acme-challenge/` from `/var/www/lob-dashboard-acme` and return `404` for every other path until HTTPS is installed.

- [ ] **Step 4: Create the final HTTPS site**

Create separate port-80 redirect and port-443 TLS server blocks. Exempt the ACME path, use the dedicated certificate paths, require Basic Auth at `/`, set `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and HSTS, and proxy only to `127.0.0.1:8652` with HTTP/1.1 and forwarded headers.

- [ ] **Step 5: Create the guarded rollback script**

The script must use `set -euo pipefail`, resolve `/home/ubuntu/limit-order-book`, verify it equals the expected path, validate that each Nginx target is exactly one of the three dedicated paths, run `docker compose -p limit-order-book -f deploy/docker-compose.production.yml down`, remove only the dedicated enabled symlink/site/auth file, run `sudo nginx -t`, and `sudo systemctl reload nginx`. It retains runtime directories and the certificate by default and prints the exact optional certificate deletion command without executing it.

- [ ] **Step 6: Run tests and shell syntax checks**

```bash
pytest -q market_maker/tests/test_deployment_contract.py
bash -n deploy/rollback.sh
```

Expected: all pass.

- [ ] **Step 7: Commit exposure and rollback assets**

```bash
git add deploy/nginx deploy/rollback.sh market_maker/tests/test_deployment_contract.py
git commit -m "feat: add isolated dashboard proxy and rollback"
```

### Task 5: Full local verification and image safety smoke

**Files:**
- Modify: `docs/deployment/DEPLOYMENT_PROGRESS.md`

**Interfaces:**
- Verification produces a tested commit SHA and an image with an immutable ID/size.

- [ ] **Step 1: Run the complete deterministic suite**

Run: `pytest -q`

Expected: all tests pass with no network, exchange, wallet, Telegram, or MySQL dependency.

- [ ] **Step 2: Compile and validate configuration**

```bash
python -m compileall -q market_maker
docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env config --quiet
```

Expected: exit 0.

- [ ] **Step 3: Build only the new image**

Run: `docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env build`

Expected: the Limit Order Book image builds without runtime artifacts or secrets.

- [ ] **Step 4: Prove unsafe container startup is rejected**

Run the built image with real mode, a nonempty Nobitex token, and a nonempty MySQL host in three separate `docker run --rm` commands. Each must exit nonzero with `DemoStartupError`. Run the safe entrypoint with `python -c` and expect `DEMO_STARTUP_GUARD_OK` plus exit 0.

- [ ] **Step 5: Record local evidence and commit**

Append test counts, Compose validation, image ID/size, smoke results, commands, problems, rollback, and the exact next step to `docs/deployment/DEPLOYMENT_PROGRESS.md`.

```bash
git add -f docs/deployment/DEPLOYMENT_PROGRESS.md
git commit -m "docs: record deployment build verification"
```

### Task 6: Promote the verified commit to production

**Files:**
- No source file changes.

**Interfaces:**
- Produces: `production` pointing exactly to the verified `parsaoryani` commit.

- [ ] **Step 1: Verify branch and worktree scope**

```bash
git status --short --branch
git branch --show-current
git rev-parse main
git rev-parse origin/main
git rev-parse HEAD
```

Expected: current branch is `parsaoryani/algo1-refactor-testability`; only the preserved runtime directory is untracked; local and remote-tracking `main` remain at their original SHAs.

- [ ] **Step 2: Create or safely fast-forward production**

If `production` is absent, create it at the verified `HEAD` without checking it out using `git branch production HEAD`. If it exists, require `git merge-base --is-ancestor production HEAD` before `git branch -f production HEAD`. Never force a non-fast-forward rewrite.

- [ ] **Step 3: Push only allowed branches when credentials permit**

```bash
git push -u origin parsaoryani/algo1-refactor-testability
git push -u origin production
```

Expected: both refs point to the verified commit; `origin/main` is unchanged. If authentication fails, record it and continue deployment from the exact local production archive.

### Task 7: Revalidate and deploy the isolated Compose stack

**Files:**
- Server create: `/home/ubuntu/limit-order-book/**`
- Server create: `/home/ubuntu/limit-order-book/runtime/{results,state,logs}`
- Modify: `docs/deployment/DEPLOYMENT_PROGRESS.md`

**Interfaces:**
- SSH target: `ubuntu@185.206.95.141`.
- Deployment source: `git archive production`, not the dirty local worktree.

- [ ] **Step 1: Revalidate Sarkhatti baseline and capacity**

Run read-only inspect/hash/latency/resource commands from Phase 1. Require exact ID, start time, restart count 0, ports, mount, hashes, health, and OOM state. Require port 8652 and proposed names to remain free and at least 700 MiB host memory available before build.

- [ ] **Step 2: Transfer the exact production archive**

Create `/home/ubuntu/limit-order-book` only after proving it is absent. Stream `git archive --format=tar production` over SSH into that exact directory. Write the production SHA to `DEPLOYED_COMMIT` and verify it matches locally.

- [ ] **Step 3: Create runtime directories and validate Compose**

Create only `/home/ubuntu/limit-order-book/runtime/results`, `state`, `logs`, and `backups`; set ownership to `ubuntu:ubuntu` and conservative modes. Run:

```bash
cd /home/ubuntu/limit-order-book
docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env config
```

Expected: exact isolated model and loopback port.

- [ ] **Step 4: Prepare rollback before startup**

Run `bash -n deploy/rollback.sh`, inspect its resolved targets without executing destructive branches, and record the exact rollback command.

- [ ] **Step 5: Build only the new stack and revalidate Sarkhatti**

Run `docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env build`, then repeat the immutable Sarkhatti checks before startup.

- [ ] **Step 6: Start only the new project**

Run:

```bash
docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env up -d
```

Expected: initializer exits 0; two workers and dashboard become healthy. Immediately compare Sarkhatti to its baseline.

- [ ] **Step 7: Record deployment evidence locally and on the server**

Append exact commands, image IDs, container IDs, ports, networks, mounts, health, restart/OOM state, resource use, problems, rollback, and next step to the progress journal; copy the updated journal in the next exact-archive refresh only if no runtime-owned files are overwritten.

### Task 8: Install authenticated HTTPS without touching Sarkhatti

**Files:**
- Server create: dedicated Nginx site, auth file, ACME root, certificate lineage, timestamped backups under the deployment backup directory.

**Interfaces:**
- Public URL: `https://dashboard.185-206-95-141.sslip.io/`.
- Basic Auth user: `lob-team`.

- [ ] **Step 1: Verify DNS externally and from the server**

Require the dedicated hostname to resolve to `185.206.95.141`. If it does not, stop without installing a public proxy.

- [ ] **Step 2: Back up Nginx evidence**

Copy `/etc/nginx/nginx.conf`, `/etc/nginx/sites-available/sarkhatti`, and the enabled-link metadata into a timestamped `/home/ubuntu/limit-order-book/backups/nginx-*` directory. Record hashes before and after; never modify the source files.

- [ ] **Step 3: Generate Basic Auth safely**

Generate a cryptographically random password, create `/etc/nginx/lob-dashboard.htpasswd` for user `lob-team`, set owner `root:root` and mode `640`, and retain the clear password only for the final operator handoff.

- [ ] **Step 4: Install HTTP bootstrap, test, and reload**

Create `/var/www/lob-dashboard-acme`, copy only the dedicated HTTP site, create only its enabled symlink, run `sudo nginx -t`, then `sudo systemctl reload nginx`. Revalidate Sarkhatti.

- [ ] **Step 5: Obtain the separate certificate**

Run Certbot noninteractively with the webroot plugin, existing account, exact domain, and no Nginx installer mutation. Require the new lineage to contain only the dashboard hostname.

- [ ] **Step 6: Install HTTPS site, test, and reload**

Replace only `/etc/nginx/sites-available/lob-dashboard` with the reviewed HTTPS asset, run `sudo nginx -t`, reload, and revalidate Sarkhatti file hashes/container baseline.

- [ ] **Step 7: Validate authentication and TLS**

Require unauthenticated HTTPS to return 401, authenticated HTTPS to return 200, certificate SAN/issuer/expiry to match, HTTP to redirect to HTTPS, and direct public access to port 8652 to fail.

### Task 9: Bounded production validation and handoff

**Files:**
- Modify: `docs/deployment/DEPLOYMENT_PROGRESS.md`

**Interfaces:**
- Produces final evidence, credentials handoff, scoped rollback, and explicit limited-readiness statement.

- [ ] **Step 1: Validate shared-session stability**

Read the shared session document and both workers' latest ledger/result records. Require exactly one session ID across BTC and USDT and verify it remains unchanged across multiple health intervals.

- [ ] **Step 2: Validate zero real operations**

Inspect startup guard markers and run focused safety assertions inside the production image. Require all counters for real send, cancel, bulk cancel, stale cancel, authenticated wallet, wallet mutation, and MySQL to equal zero.

- [ ] **Step 3: Monitor containers and resources**

During a bounded observation period, sample `docker stats --no-stream`, health, restart count, OOM state, session ID, and recent logs multiple times. Require no crash loops, no unexpected `stack_shutdown` cancellations, and observed total Limit Order Book memory below 512 MiB.

- [ ] **Step 4: Revalidate Sarkhatti at the end**

Repeat exact container/file/port/mount/hash/latency checks. Require no change and no meaningful latency or memory regression.

- [ ] **Step 5: Finalize and commit the journal**

Record completed work, every command family, every file changed, backups, results, problems, exact rollback, observation duration, and exact continuation step. Explicitly state that the bounded run does not establish long-term readiness.

```bash
git add -f docs/deployment/DEPLOYMENT_PROGRESS.md
git commit -m "docs: complete isolated demo deployment handoff"
git branch -f production HEAD
git push origin parsaoryani/algo1-refactor-testability production
```

Expected: final deployment evidence is present on both allowed branches if push authentication succeeds; `main` remains unchanged.
