# Limit Order Book Deployment Progress

Last updated: 2026-08-01 14:11 UTC

## Current checkpoint

Phase 1 (read-only audit) is complete enough to continue design and preparation, but no deployment files have been prepared and no server state has been changed. The Limit Order Book stack is not deployed. The operator authorized a complete deployment and asked for the safest alternative to hosting Dash at a path on Sarkhatti's hostname.

Git scope is restricted to `parsaoryani/algo1-refactor-testability` and a future `production` branch. `main` must not be checked out, merged into, committed to, or pushed as part of this deployment. At this checkpoint, `production` does not exist in the local or fetched remote branch list.

The operator explicitly selected `git@github.com:Alireza77n/Limit-Order-Book.git` as the deployment repository. Verified commits must be pushed on the current `parsaoryani` branch, promoted by fast-forward to `production`, and fetched from that repository on the server. Archive transfer is no longer the deployment method.

## Completed work

- Confirmed both supplied SSH targets resolve to `185.206.95.141`.
- Connected to the Ubuntu server and performed read-only host, Docker, Nginx, certificate, port, capacity, and Sarkhatti checks.
- Captured Sarkhatti's container identity, restart count, health, ports, mounts, network, file hashes, resource use, and local latency baseline.
- Confirmed `/home/ubuntu/limit-order-book` is absent.
- Confirmed proposed host port `127.0.0.1:8652` is free.
- Confirmed proposed container names `lob-btc-demo`, `lob-usdt-demo`, and `lob-dashboard` are unused.
- Confirmed no Docker volumes exist and only `bridge`, `host`, `none`, and `sarkhatti_default` networks exist.
- Confirmed Nginx configuration passes `nginx -t` and only the `sarkhatti` site is enabled.
- Confirmed the repository already contains a Dash dashboard, paper ledger, Demo exchange adapter, Demo startup guard, credential-stripping tests, MySQL-blocking tests, and shared-session tests.
- Confirmed `dashboard.<existing-arvan-hostname>` and `lob.<existing-arvan-hostname>` do not currently have A records.
- Evaluated path-based proxying and rejected it as the default design because it would modify Sarkhatti's existing server block and require Dash pathname-prefix changes.
- Initially selected `dashboard.185-206-95-141.sslip.io`, then rejected it after the Ubuntu server's ISP resolver returned private address `10.10.34.36`. Selected `dashboard.185-206-95-141.nip.io` instead after the same server resolved it correctly to `185.206.95.141`. The hostname remains a third-party DNS dependency and should later be replaced by an operator-controlled domain.
- Received explicit authority to complete all remaining tasks autonomously and to use the configured GitHub repository for branch push and server fetch.
- Confirmed the local origin is `git@github.com:Alireza77n/Limit-Order-Book.git` for fetch and push.
- Confirmed authenticated read access to that repository from both the workstation and Ubuntu server. The server has Git `2.43.0`.
- Recorded GitHub's host key in `/home/ubuntu/.ssh/known_hosts` during the first server-side authenticated read using `StrictHostKeyChecking=accept-new`; this is the only server mutation since the Phase 1 audit.
- Confirmed remote `main` remains `ba44366ba266b4e00d9da9467a888ef4f8492727` and that neither `parsaoryani/algo1-refactor-testability` nor `production` exists remotely yet.
- Added the fail-closed Demo process entrypoint in commits `347652f` and `f4d7b90`. The initial task review found that absolute paths to `run.py` and lexical `..` runtime paths bypassed validation; both now have regression tests and the re-review is clean.
- Task 1 verification evidence: 26 focused entrypoint/Demo-safety/end-to-end tests passed; the full suite passed 191 tests with one pre-existing Dash deprecation warning. The zero-real-call end-to-end assertion remained exact.
- Added environment-controlled result and state roots in commit `998b1cd`. Demo CSV/SQLite outputs remain in the result root; paper ledgers, fee state, archives, and worker lease files use the state root. Native defaults remain compatible.
- Task 2 verification evidence: 19 focused runtime/config/session tests and 194 full-suite tests passed with the same pre-existing Dash warning; task review was clean.
- Added the isolated production Compose stack in commits `79d448d`, `d8cfb35`, and `de41192`. Review initially blocked deployment because forwarded scripts were not executable, the Docker-internal network blocked public market-data egress, stop grace was absent, stale lock files could pass health checks, and raw-YAML tests were too weak. All findings were fixed and re-reviewed.
- Task 3 final evidence: exact Compose config and image build exited 0; a constrained read-only container printed both the Demo guard and forwarded-command smoke markers; 11 focused contract tests and 205 full-suite tests passed. The resolved Compose JSON enforces four services, one egress-capable dedicated bridge, dashboard-only `127.0.0.1:8652`, exact mounts/hardening, active lease health, and 128/128/192 MiB long-running limits (448 MiB total).
- Added the dedicated ACME bootstrap site, authenticated HTTPS proxy, and exact-target rollback in commit `dcb0fb3`. Rollback tolerates absent dedicated targets but aborts on any unexpected existing type or symlink target; it retains runtime data, ACME root, and certificate by default.
- Task 4 evidence: 14 focused deployment-contract tests, `bash -n deploy/rollback.sh`, and 208 full-suite tests passed; independent review found no auth bypass, upstream exposure, rollback-scope, deletion-ordering, quoting, symlink/type, partial-state, or Nginx failure-handling defect.

## Commands executed

All server commands in this phase were read-only. SSH used `BatchMode=yes`, a ten-second connection timeout, and an ephemeral known-hosts file:

```bash
dig +short 185.206.95.141
dig +short server-31d9221a-ed1b-42e0-9289-41041f70fab6.ir-thr-si1.arvancompute.ir
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null ubuntu@185.206.95.141 '<read-only audit script>'
```

The remote audit script executed these command families:

```bash
date -u +%FT%TZ
hostname -f
uptime
uname -srmo
docker version --format '{{.Server.Version}}'
docker compose version --short
docker ps -a --no-trunc
docker inspect sarkhatti
docker stats --no-stream
docker network ls
docker volume ls
docker system df
sudo -n ss -lntup
free -h
df -hT / /var/lib/docker
df -i / /var/lib/docker
sudo -n nginx -t
sudo -n nginx -T
find /etc/nginx/sites-enabled -maxdepth 1 -type l
sudo -n certbot certificates
curl http://127.0.0.1:8642/
curl http://127.0.0.1:6080/
sha256sum /home/ubuntu/sarkhatti/docker-compose.yml
sha256sum /home/ubuntu/sarkhatti/.env
sha256sum /etc/nginx/sites-available/sarkhatti
stat /home/ubuntu/sarkhatti/docker-compose.yml
stat /home/ubuntu/sarkhatti/.env
stat /etc/nginx/sites-available/sarkhatti
```

Local repository discovery used the codebase knowledge graph first, followed by read-only Git/config searches:

```bash
git status --short --branch
git branch -vv
git branch -r
git log --graph --decorate --oneline --all -20
dig +short A dashboard.server-31d9221a-ed1b-42e0-9289-41041f70fab6.ir-thr-si1.arvancompute.ir
dig +short A lob.server-31d9221a-ed1b-42e0-9289-41041f70fab6.ir-thr-si1.arvancompute.ir
git ls-remote --heads origin main parsaoryani/algo1-refactor-testability production
ssh ubuntu@185.206.95.141 \
  'GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new" \
   git ls-remote --heads git@github.com:Alireza77n/Limit-Order-Book.git \
   production parsaoryani/algo1-refactor-testability main'
```

## Audit results and immutable Sarkhatti baseline

Server:

- Host: `ubuntu---2-vcpu---2-gb-ram`
- Ubuntu kernel: `Linux 6.8.0-136-generic x86_64`
- Docker Engine: `29.1.3`
- Docker Compose: `2.40.3+ds1-0ubuntu1~24.04.1`
- Nginx: `1.24.0`
- Host Nginx owns public ports 80/443.
- Existing certificate name/domain: `server-31d9221a-ed1b-42e0-9289-41041f70fab6.ir-thr-si1.arvancompute.ir`; valid until 2026-10-24.

Sarkhatti baseline:

- Container ID: `4ffa1cdf90fef73f8f068fd646c84c1bf0f85cb5f0ef02a21c676d87425d57f5`
- Container name: `sarkhatti`
- Image: `sarkhatti-sarkhatti`
- State/health: `running` / `healthy`
- Restart count: `0`
- Started: `2026-08-01T04:46:32.135594947Z`
- OOM killed: `false`
- Ports: `127.0.0.1:6080 -> 6080/tcp`, `127.0.0.1:8642 -> 8642/tcp`
- Mount: `/home/ubuntu/sarkhatti/data -> /app/data` (bind, read/write)
- Network: `sarkhatti_default`, container IP `172.18.0.2`
- Resource sample: `0.52%` CPU, `311.7 MiB` memory, 48 PIDs
- `/` latency on port 8642: 4.7–9.4 ms across five requests
- `/` latency on port 6080: 12.3–857.3 ms across three requests; the first request was the outlier and the following two were 25.4 ms and 12.3 ms

Hashes that must remain unchanged:

```text
66563a50ac2bf2eb4aae9a77ccc3502259434bef8ca2c70499d42e9e221daca2  /home/ubuntu/sarkhatti/docker-compose.yml
106edceda30de0935ef78b460ec24c5b16a78faa980a7e84eda44ba8bdfbeac0  /home/ubuntu/sarkhatti/.env
0403e33fe949754bcd99f1740cfa93ba6e4d4c37b9c4569a74ba2c276b333454  /etc/nginx/sites-available/sarkhatti
```

Capacity and conflicts:

- Memory: 1.9 GiB total, 581 MiB used, 1.4 GiB available.
- Swap: 2.0 GiB total, 62 MiB used, 1.9 GiB free.
- Disk: 28 GiB total, 14 GiB used, 14 GiB free (50%).
- Inodes: 3% used.
- Docker currently reports 8.905 GB of image data and 8.598 GB of build cache. No prune command was run or authorized.
- `/home/ubuntu/limit-order-book`: absent.
- Port 8652: free.
- Proposed container names: free.
- No Docker volumes currently exist.
- No proposed Limit Order Book network exists.

## Files changed

- Created `docs/deployment/DEPLOYMENT_PROGRESS.md` locally as the durable handoff.
- No server file was created, modified, moved, or deleted.
- No Git branch, commit, tag, remote ref, or worktree was changed.

## Backups

No backup was required because Phase 1 made no server changes. Before any future Nginx edit, copy the exact target file to a timestamped backup outside the enabled-site path and record its checksum here.

## Problems and cautions

- The first audit script stopped early because a Docker Go-template variable was interpreted by the remote shell under `set -u`. The unfinished read-only checks were rerun with corrected quoting. No server mutation occurred.
- The current repository Compose file is not suitable for this server: its dashboard publishes `8050` on all interfaces, services have generic names, and it lacks the requested resource/logging isolation.
- `market_maker/.env.example` advertises real Nobitex and MySQL credentials. The deployment environment must instead omit those values and include an explicit startup guard that rejects them.
- Docker storage is material on a 28 GB disk. The new image and build context must be measured before startup; global prune commands remain forbidden.
- A short audit proves only the present baseline, not long-term readiness.
- The local worktree contains the pre-existing untracked directory `.docker-validation-results-pre-cancel-fix-20260801T1148Z/`; it must be preserved and excluded from deployment/build context.
- `dashboard.185-206-95-141.nip.io` depends on third-party DNS. Certificate issuance and external resolution must be proven before it is accepted; failure is a stop condition, not a reason to fall back to Sarkhatti's `/` block. The rejected sslip.io hostname must not be used because regional DNS rewrites it to `10.10.34.36`.

## Rollback status

Nothing has been deployed, so current rollback is a no-op. Future rollback must be scoped exactly to:

```bash
cd /home/ubuntu/limit-order-book
docker compose -p limit-order-book down
```

The final rollback procedure may additionally remove only the dedicated Limit Order Book Nginx site symlink/file and its dedicated certificate after resolving and verifying their exact paths. It must re-run `nginx -t` before `systemctl reload nginx`. It must never reference `/home/ubuntu/sarkhatti`, container `sarkhatti`, network `sarkhatti_default`, or Sarkhatti's certificate.

## Exact next step

Run the fresh full local release gate: tests, compilation, exact Compose rendering, image build/size, and unsafe-startup smoke matrix. Record the evidence, then promote the verified SHA to the allowed GitHub branches.

## Superseded Task 5 evidence (2026-08-01 13:52 UTC)

**Invalidated by review:** commit `fa6f3fb` did not establish image provenance. The inspected image `Config.Created` predated the tested source commit, the smoke commands used the mutable tag rather than an image ID, and the `RepoDigests` field was described as though it proved a registry artifact. Keep the following record only as rejected historical evidence; the corrected gate below supersedes every success claim in this section.

The full local release gate passed at commit `bb5435160a286a5ce8ca8b1ca7a9d07494ecfb8c` on `parsaoryani/algo1-refactor-testability`. This is a local image verification only; no server or hostname asset was changed.

Commands and results:

```bash
python3 -m pytest -q
# 208 passed, 1 warning in 6.53s; exit 0

python3 -m compileall -q market_maker
# exit 0

docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config --format json
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config --quiet
# source, JSON, and quiet validation all exited 0

docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env build
# exit 0; resolved python:3.11-slim digest
# sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93
```

The only test warning was Dash's existing `dash_table.DataTable` deprecation warning. The resolved Compose source and JSON contain the four intended services and only the non-secret demo defaults: `LOB_RESULTS_DIR`, `LOB_STATE_DIR`, `DEMO_SESSION_FILE`, and `PYTHONDONTWRITEBYTECODE`.

Fresh isolated image smoke, each using `docker run --rm limit-order-book-demo:local`:

```text
real worker command with --mode real: exit 1; DemoStartupError:
  worker command must explicitly contain --mode demo
NOBITEX_API_TOKEN=nonempty-smoke-value: exit 1; DemoStartupError:
  NOBITEX_API_TOKEN must be empty in Demo deployment
MYSQL_HOST=nonempty-smoke-host: exit 1; DemoStartupError:
  MYSQL_HOST must be empty in Demo deployment
safe forwarded python -c: exit 0; output:
  DEMO_STARTUP_GUARD_OK real_operations=disabled
  SAFE_FORWARDED_COMMAND_OK
```

Each rejected case stopped in `demo_entrypoint.py` before the forwarded Python command could run, so no application or network behavior was entered. The safe command was:

```bash
docker run --rm limit-order-book-demo:local \
  python demo_entrypoint.py python -c "print('SAFE_FORWARDED_COMMAND_OK')"
```

Image evidence after the successful build:

```text
Image ID:     sha256:35ebd8112a1ece4795aed2508d38f5fe0d230e08aa7fbcceedbb41babe4f7dde
Repo digest:  limit-order-book-demo@sha256:35ebd8112a1ece4795aed2508d38f5fe0d230e08aa7fbcceedbb41babe4f7dde
Created:      2026-08-01T13:20:39.600803804Z
Size:         144569302 bytes (`docker image ls` displays 702MB)
Config.Env:   PATH, LANG, GPG_KEY, PYTHON_VERSION, PYTHON_SHA256, PYTHONUNBUFFERED
```

`docker inspect` showed no deployment secret or user-supplied value in `Config.Env`. A final-rootfs audit with a guardless `/bin/sh` entrypoint found no `.env`, `.docker-validation-results*`, `runtime`, `results`, `.pytest_cache`, `__pycache__`, `*.pyc`, or `tests` path under `/app`. The Docker build context also excluded the preserved local validation directory.

Final local checks:

```bash
git diff --check
# exit 0
git status --short --branch
# only the pre-existing untracked .docker-validation-results-pre-cancel-fix-20260801T1148Z/ directory before this ignored-doc update
```

Problems/cautions: the first two Task 5 build attempts were blocked by transient Docker Hub token-exchange failures; this resumed run rebuilt successfully after registry metadata became available. The image is local-only and has not been pushed. The nip.io hostname requires separate external DNS/certificate proof before server deployment.

Rollback remains a no-op because nothing has been deployed. If a future deployment requires rollback, use only:

```bash
cd /home/ubuntu/limit-order-book
docker compose -p limit-order-book down
```

Exact next step: push and promote this verified SHA through the authorized GitHub branch flow, then complete the separate server-side nip.io DNS, certificate, and authenticated-proxy deployment checks before starting the stack.

## Corrected Task 5 provenance-bound release gate (2026-08-01 14:11 UTC)

The reviewed release gate passes for code commit `fca90fdf6a3417d31f75be8269739e275279bf3c`. Provenance support was introduced in `068e033a64f2397d6f989f69a874a10dba29ed36`; the follow-up `fca90fd` moves revision-dependent OCI metadata after dependency/source layers so a new revision does not invalidate the dependency cache.

TDD evidence:

```text
RED: test_image_build_provenance_is_resolved_and_stamped_as_oci_labels
     KeyError: 'args'
GREEN: focused provenance test: 1 passed
ORDERING RED: assertion failed because ARG LOB_REVISION preceded pip install
ORDERING GREEN: focused provenance test: 1 passed
Focused deployment contract after each implementation: 15 passed
```

The immutable provenance inputs were selected only after the final code commit:

```text
LOB_REVISION=fca90fdf6a3417d31f75be8269739e275279bf3c
LOB_BUILD_DATE=2026-08-01T14:09:11Z
```

Fresh release commands and results:

```bash
python3 -m pytest -q
# 209 passed, 1 warning in 8.33s; exit 0

python3 -m compileall -q market_maker
# exit 0

LOB_REVISION=fca90fdf6a3417d31f75be8269739e275279bf3c \
LOB_BUILD_DATE=2026-08-01T14:09:11Z \
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config
# exit 0

LOB_REVISION=fca90fdf6a3417d31f75be8269739e275279bf3c \
LOB_BUILD_DATE=2026-08-01T14:09:11Z \
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config --format json
# exit 0; rendered build args exactly match both values above

LOB_REVISION=fca90fdf6a3417d31f75be8269739e275279bf3c \
LOB_BUILD_DATE=2026-08-01T14:09:11Z \
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config --quiet
# exit 0

LOB_REVISION=fca90fdf6a3417d31f75be8269739e275279bf3c \
LOB_BUILD_DATE=2026-08-01T14:09:11Z \
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env build
# exit 0; dependency install and source copy were CACHED
```

The full-suite warning remains Dash's existing `dash_table.DataTable` deprecation warning. The final image inspection is:

```text
Image ID:          sha256:bdade84f231d8bed76003782063d8968292c256aa961547c317c5194cb70604a
Config.Created:    2026-08-01T13:20:39.600803804Z
OCI revision:      fca90fdf6a3417d31f75be8269739e275279bf3c
OCI created:       2026-08-01T14:09:11Z
Size:              144570014 bytes
RepoDigests JSON:  ["limit-order-book-demo@sha256:bdade84f231d8bed76003782063d8968292c256aa961547c317c5194cb70604a"]
Config.Env:        PATH, LANG, GPG_KEY, PYTHON_VERSION, PYTHON_SHA256, PYTHONUNBUFFERED
```

`Config.Created` reflects the cached image configuration history and is not used as source provenance. The exact OCI labels bind the image to the tested source commit and selected UTC build date. `RepoDigests` is only local Docker metadata for this unpushed image; it is **not** registry provenance or proof that the image exists in a registry. No secret or user-provided deployment value appears in `Config.Env`. A final-rootfs `find` audit by immutable image ID returned no `.env`, `.docker-validation-results*`, `runtime`, `results`, `.pytest_cache`, `__pycache__`, `*.pyc`, or `tests` path under `/app`.

Every smoke used the immutable image ID. Exact commands, statuses, and complete combined stderr/stdout follow.

Real-mode rejection:

```bash
docker run --rm \
  sha256:bdade84f231d8bed76003782063d8968292c256aa961547c317c5194cb70604a \
  python demo_entrypoint.py python run.py --profile btc --mode real --continuous
# exit 1
```

```text
Traceback (most recent call last):
  File "/app/market_maker/demo_entrypoint.py", line 55, in <module>
    main()
  File "/app/market_maker/demo_entrypoint.py", line 49, in main
    validate_demo_startup(command, os.environ)
  File "/app/market_maker/demo_entrypoint.py", line 37, in validate_demo_startup
    raise DemoStartupError("worker command must explicitly contain --mode demo")
DemoStartupError: worker command must explicitly contain --mode demo
```

The guard success marker was absent, proving the command was rejected before `exec`.

Nobitex-token rejection with execution canary:

```bash
docker run --rm -e NOBITEX_API_TOKEN=nonempty-smoke-value \
  sha256:bdade84f231d8bed76003782063d8968292c256aa961547c317c5194cb70604a \
  python demo_entrypoint.py python -c "print('UNSAFE_CANARY_RAN')"
# exit 1
```

```text
Traceback (most recent call last):
  File "/app/market_maker/demo_entrypoint.py", line 55, in <module>
    main()
  File "/app/market_maker/demo_entrypoint.py", line 49, in main
    validate_demo_startup(command, os.environ)
  File "/app/market_maker/demo_entrypoint.py", line 40, in validate_demo_startup
    raise DemoStartupError(f"{name} must be empty in Demo deployment")
DemoStartupError: NOBITEX_API_TOKEN must be empty in Demo deployment
```

`UNSAFE_CANARY_RAN` and the guard success marker were both absent.

MySQL-host rejection with execution canary:

```bash
docker run --rm -e MYSQL_HOST=nonempty-smoke-host \
  sha256:bdade84f231d8bed76003782063d8968292c256aa961547c317c5194cb70604a \
  python demo_entrypoint.py python -c "print('UNSAFE_CANARY_RAN')"
# exit 1
```

```text
Traceback (most recent call last):
  File "/app/market_maker/demo_entrypoint.py", line 55, in <module>
    main()
  File "/app/market_maker/demo_entrypoint.py", line 49, in main
    validate_demo_startup(command, os.environ)
  File "/app/market_maker/demo_entrypoint.py", line 40, in validate_demo_startup
    raise DemoStartupError(f"{name} must be empty in Demo deployment")
DemoStartupError: MYSQL_HOST must be empty in Demo deployment
```

`UNSAFE_CANARY_RAN` and the guard success marker were both absent.

Safe forwarded-command smoke:

```bash
docker run --rm \
  sha256:bdade84f231d8bed76003782063d8968292c256aa961547c317c5194cb70604a \
  python demo_entrypoint.py python -c "print('SAFE_FORWARDED_COMMAND_OK')"
# exit 0
```

```text
DEMO_STARTUP_GUARD_OK real_operations=disabled
SAFE_FORWARDED_COMMAND_OK
```

Problems/cautions: the initial provenance implementation placed `ARG`/`LABEL` before `pip install`, invalidating dependency cache for every revision. A PyPI timeout exposed the defect. Commit `fca90fd` fixes the ordering and a contract assertion prevents regression. The image remains local and unpushed. nip.io DNS/certificate proof remains a separate pre-deployment gate.

Rollback is still a no-op because no server deployment occurred. Future deployed-stack rollback remains exactly:

```bash
cd /home/ubuntu/limit-order-book
docker compose -p limit-order-book down
```

Exact next step: commit this corrected evidence, push the code and documentation commits through the authorized branch flow, and use only the provenance-bound image ID/revision during the separate server-side nip.io DNS, certificate, authentication, and startup gate.

## 2026-08-01 — Server deployment checkpoint (stack healthy, Nginx pending)

Completed work:

- Re-ran the pre-start Sarkhatti gate and confirmed container ID `4ffa1cdf90fef73f8f068fd646c84c1bf0f85cb5f0ef02a21c676d87425d57f5`, healthy state, restart count `0`, original start time, loopback ports, and `/home/ubuntu/sarkhatti/data:/app/data` mount were unchanged.
- Cloned only the `production` branch into `/home/ubuntu/limit-order-book` at commit `ff5f000ff13f61be3f3d470de26ee1fac3cb69e8` and created isolated `runtime/results`, `runtime/state`, `runtime/logs`, and `runtime/backups` directories.
- Built `limit-order-book-demo:local` on the server. Immutable image ID: `sha256:07951518ba38b63c1a22101ee9a8777ea2de88678c3353f066115d333ded86d2`; OCI revision: `ff5f000ff13f61be3f3d470de26ee1fac3cb69e8`; OCI build date: `2026-08-01T14:20:37Z`; size: `145046369` bytes.
- Started only Compose project `limit-order-book` with `--no-build`. `lob-session-init` exited `0`; `lob-btc-demo`, `lob-usdt-demo`, and `lob-dashboard` became healthy with zero restarts.
- Confirmed workers publish no host ports and the dashboard publishes only `127.0.0.1:8652->8050/tcp`.
- Confirmed all three long-running services executed the Demo startup guard once and contain no Nobitex, MySQL, or Telegram environment variables.
- Ran immutable-image guard canaries: real worker mode, non-empty `NOBITEX_API_TOKEN`, and non-empty `MYSQL_PASSWORD` each exited `1` before the success marker or execution canary; the safe Demo forwarding control exited `0` with both markers.
- Confirmed one shared session ID, `demo-20260801T142243Z-911eaa88ba00`, created once at `2026-08-01T14:22:43.698018+00:00`.
- Measured current memory: BTC `58.1 MiB`, USDT `63.21 MiB`, dashboard `82.98 MiB` (about `204 MiB` combined), with `925 MiB` host RAM available and `1.9 GiB` swap free.
- Rechecked Sarkhatti after build and startup: its identity, health, restart count, start time, ports, and mount remained unchanged.

Commands executed included:

```bash
LOB_REVISION=ff5f000ff13f61be3f3d470de26ee1fac3cb69e8 \
LOB_BUILD_DATE=2026-08-01T14:20:37Z \
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env build

docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env up -d --no-build

docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env ps -a
docker stats --no-stream sarkhatti lob-btc-demo lob-usdt-demo lob-dashboard
```

Files changed on the server: only files under `/home/ubuntu/limit-order-book`, the dedicated Docker image, containers, and network for Compose project `limit-order-book`. No Sarkhatti file or configuration was changed. No Nginx file or certificate has been changed yet.

Problems: two early disposable smoke wrappers had incorrect test assumptions/argument order. They did not mutate either application and did not invalidate the effective Compose configuration. The corrected direct immutable-image commands produced the rejection evidence recorded above.

Rollback at this checkpoint:

```bash
cd /home/ubuntu/limit-order-book
docker compose -p limit-order-book -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env down
```

Exact next step: back up current Nginx configuration, install the dedicated HTTP-only ACME bootstrap server block for `dashboard.185-206-95-141.nip.io`, run `nginx -t`, reload Nginx, obtain the separate certificate, then install the authenticated HTTPS server block after another `nginx -t`.

## 2026-08-01 — Dashboard HTTPS and final short-run validation

Completed work:

- Backed up `/etc/nginx/nginx.conf`, `/etc/nginx/sites-available/sarkhatti`, its enabled-link target, and pre-change hashes to `/home/ubuntu/limit-order-book/runtime/backups/nginx-20260801T1429Z` before any successful Nginx reload.
- Installed the dedicated site `/etc/nginx/sites-available/lob-dashboard`, enabled only that site, created `/etc/nginx/lob-dashboard.htpasswd`, and installed the dedicated compatibility file `/etc/nginx/conf.d/lob-server-names-hash.conf`.
- Obtained a separate Let's Encrypt certificate for `dashboard.185-206-95-141.nip.io`, valid from 2026-08-01 through 2026-10-30. Certbot installed its normal renewal timer.
- Installed the final authenticated HTTPS proxy and reloaded (never restarted) Nginx only after `nginx -t` passed.
- Verified externally: HTTP `301` to HTTPS, unauthenticated HTTPS `401`, authenticated HTTPS `200`, certificate verification result `0`, and direct public access to `185.206.95.141:8652` timed out. The only listener for port 8652 is `127.0.0.1:8652`.
- Verified the new containers use only network `limit-order-book-internal` and mounts rooted under `/home/ubuntu/limit-order-book/runtime`. No Sarkhatti path is mounted.
- After more than six minutes, all long-running services remained healthy with zero restarts and zero OOM events; `lob-session-init` remained successfully exited. Logs contained one startup-guard marker per service, zero traceback/error matches, and zero `stack_shutdown`/cancellation matches.
- Confirmed the shared session file remained exactly one file with the original session ID and creation time. Both BTC and USDT SQLite results contain only that session and `Mode=demo`; synthetic BTC IDs have the local `btcirt-buy-*`/`btcirt-sell-*` form.
- The deterministic two-symbol zero-call test passed as part of the 209-test suite. It explicitly asserts real send, cancel, bulk-cancel, stale-cancel, and MySQL call counts are all zero. Runtime guard canaries separately rejected real mode and credential injection.
- Final measured new-stack memory was about 206 MiB, below the 512 MiB target. Host available RAM was about 1.0 GiB with 1.9 GiB swap free.
- Final Sarkhatti evidence remained identical: container ID, start time, healthy state, restart count `0`, OOM false, mounts, and ports unchanged. Compose, `.env`, and Nginx site hashes remained exactly `66563a50...`, `106edced...`, and `0403e33f...`; five local latency samples were 5.2–6.8 ms.
- Local release verification after the rollback compatibility update: `209 passed, 1 warning`; `bash -n deploy/rollback.sh` passed; rendered Compose validation passed.

Nginx commands executed:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/lob-dashboard-acme \
  -d dashboard.185-206-95-141.nip.io \
  --non-interactive --agree-tos --keep-until-expiring
```

Files changed outside the application directory are limited to:

```text
/etc/nginx/sites-available/lob-dashboard
/etc/nginx/sites-enabled/lob-dashboard -> /etc/nginx/sites-available/lob-dashboard
/etc/nginx/lob-dashboard.htpasswd
/etc/nginx/conf.d/lob-server-names-hash.conf
/etc/letsencrypt/live/dashboard.185-206-95-141.nip.io/ (Certbot-managed)
/etc/letsencrypt/archive/dashboard.185-206-95-141.nip.io/ (Certbot-managed)
/etc/letsencrypt/renewal/dashboard.185-206-95-141.nip.io.conf (Certbot-managed)
```

Problem and resolution: the first bootstrap `nginx -t` failed before reload because the existing host-wide `server_names_hash_bucket_size` was insufficient for one more hostname. No running configuration changed. A dedicated removable `lob-server-names-hash.conf` setting the bucket to 128 resolved the compatibility issue; the rollback script now validates and removes that exact file.

Rollback (removes only the new Compose project and its four dedicated Nginx files, then validates and reloads Nginx):

```bash
cd /home/ubuntu/limit-order-book
./deploy/rollback.sh
```

Optional certificate removal after rollback:

```bash
sudo certbot delete --cert-name dashboard.185-206-95-141.nip.io
```

Current status: deployment is complete and passed a short-run validation window. This is not evidence of long-term readiness; ongoing operational monitoring remains required. Exact next step for a future session: compare the session ID, restart counts, memory, logs, and Sarkhatti baseline again after an extended observation period without recreating either stack.

## 2026-08-01 — Admin demo control page (implementation, not deployed)

Branch: `parsaoryani/admin-demo-control` (off `parsaoryani/algo1-refactor-testability`).
Spec: `docs/superpowers/specs/2026-08-01-admin-demo-control-design.md`.
Plan: `docs/superpowers/plans/2026-08-01-admin-demo-control.md`.
Full deployment instructions for the next session: `docs/deployment/ADMIN_PAGE.md`.

Completed all 11 plan tasks, TDD, one commit per task:

1. `market_maker/demo_config.py` — Demo-only per-pair config overlay (closed
   field whitelist, atomic write, single `.previous` rollback slot);
   `run.py:make_cfg` merges it only when `mode == "demo"`.
2. `market_maker/control_queue.py` — file-based command/result queue shared
   by the admin app and control agent (closed action/pair enums, flock-based
   `ControlLock` raising `OperationInProgress` on contention, JSONL audit log).
3. `deploy/control_agent.py` — host-level poller, one handler per allow-listed
   action; worker actions resolve through a fixed argv dict; Docker-invoking
   handlers take an injectable `run_argv` (tests never invoke Docker).
4. `market_maker/admin_app.py` — Flask app factory, login/CSRF/session-timeout.
5. Admin routes: status, config edit/apply/revert, worker start/stop/restart
   (btc/usdt/both), new session, cancel orders.
6. Admin routes: backup list/create, restore (typed `confirm=RESTORE`), reset
   (typed `confirm=RESET`), recovery (GET diagnostic / POST safe-fix-only),
   export (zip built only from `runtime/backups/`).
7. Read-only views (orders/balances/logs/audit); orders reuses
   `dashboard.py`'s `load_track`/`activity_records` rather than reimplementing.
8. `market_maker/tests/test_admin_demo_safety.py` — zero-real-call proof for
   the admin/agent code paths (mirrors `test_demo_safety.py`).
9. Deployment artifacts: new `admin` Compose service (loopback-only
   127.0.0.1:8653, no Docker socket, ro state/results/backups + rw control
   mount only), nginx `location /admin/` with a separate Basic Auth realm,
   `deploy/admin.env.example` (+ `.gitignore` entry for the real file),
   `deploy/systemd/lob-control-agent.service`, `rollback.sh` extended to
   remove the admin htpasswd and stop the systemd unit. Also extended
   `demo_entrypoint.py`'s `ALLOWED_PATHS` to cover the two new runtime dirs
   (`LOB_CONTROL_DIR`, `LOB_BACKUPS_DIR`) so its fail-closed guard covers the
   admin container's launch command exactly like every other service.
10. This documentation checkpoint plus `docs/deployment/ADMIN_PAGE.md`.
11. Final verification (see below).

Test results: full suite `pytest -q` passed throughout, ending at
**298 passed, 1 warning** after task 9's changes (task 10/11 add no new
tests, only docs and a final re-run). `python -m compileall -q market_maker`
clean. `docker compose -p limit-order-book -f deploy/docker-compose.production.yml
--env-file deploy/demo.env config` renders successfully with the new `admin`
service (Docker Compose CLI v5.1.4 was available in this session, so this
was actually run, not just written). `bash -n` clean on `deploy/rollback.sh`
(the only shell script touched or added).

Files changed (22, all under version control; `docs/` entries were force-added
since `docs/` is repo-wide gitignored):

```
.gitignore
deploy/admin.env.example
deploy/control_agent.py
deploy/demo.env
deploy/docker-compose.production.yml
deploy/nginx/lob-dashboard-https.conf
deploy/rollback.sh
deploy/systemd/lob-control-agent.service
market_maker/admin_app.py
market_maker/control_queue.py
market_maker/demo_config.py
market_maker/demo_entrypoint.py
market_maker/run.py
market_maker/tests/test_admin_actions.py
market_maker/tests/test_admin_auth.py
market_maker/tests/test_admin_demo_safety.py
market_maker/tests/test_admin_readonly_views.py
market_maker/tests/test_control_agent.py
market_maker/tests/test_control_queue.py
market_maker/tests/test_demo_config.py
market_maker/tests/test_demo_entrypoint.py
market_maker/tests/test_deployment_contract.py
docs/deployment/ADMIN_PAGE.md
docs/deployment/DEPLOYMENT_PROGRESS.md  (this file)
README.md / usage.md                    (short admin-page pointers)
```

Unfinished / deferred (all deliberate, cross-referenced from the spec's
"Explicitly deferred" section — not overlooked):

- Multi-admin accounts / RBAC (single shared credential by design).
- Tamper-evident (hash-chained) audit log.
- Config change history beyond one `.previous` slot.
- Async job-progress UI (management actions block synchronously, bounded
  by `admin_app.COMMAND_TIMEOUT`, currently 30s).
- Automated backup retention/rotation (backups accumulate under
  `runtime/backups/`; an operational task, not code).
- Production WSGI server for `lob-admin`: it currently runs via Flask's own
  `app.run()`, the same pattern `dashboard.py` already uses in this repo.
  Fine for a single-shared-admin internal tool; swap for gunicorn if
  concurrent admin load ever becomes real.

Known risks for the deploying session to be aware of:

- **Not yet deployed.** No container was built or run, no SSH session was
  opened, nothing was installed on the production host by this session.
  Everything above is code, tests, and documentation only.
- `deploy/admin.env` does not exist in the repo (gitignored, by design) --
  the deploying session must create it (see `ADMIN_PAGE.md` step 2) before
  `docker compose up` will succeed for the `admin` service in a real
  environment; `config` validation tolerates its absence via
  `required: false` on that one `env_file` entry, but the running container
  needs real credentials.
- `runtime/control/` and `runtime/backups/` do not exist yet on the host and
  must be created before first `up` (step 3 in `ADMIN_PAGE.md`) -- Compose
  will otherwise create them as root-owned directories on first mount,
  which is not harmful but is untidy; creating them first avoids that.
- The host-level control agent is a new systemd unit outside Compose's
  reach -- `docker compose down` alone will **not** stop it; the deploying
  session must install and manage it via `systemctl` as documented.

Exact next step: a separate session/tool ("Codex") performs the actual
server deployment by following `docs/deployment/ADMIN_PAGE.md` end to end
against the production host. This session does not deploy anything.
