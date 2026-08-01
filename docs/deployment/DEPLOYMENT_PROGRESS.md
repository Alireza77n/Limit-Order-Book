# Limit Order Book Deployment Progress

Last updated: 2026-08-01 13:07 UTC

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
- Selected `dashboard.185-206-95-141.sslip.io` as the recommended dedicated hostname. The sslip.io service documents that hostnames containing a dashed IPv4 address resolve to that address and can receive individual Let's Encrypt certificates via HTTP-01. This introduces a third-party DNS dependency and should later be replaced by an operator-controlled domain.
- Received explicit authority to complete all remaining tasks autonomously and to use the configured GitHub repository for branch push and server fetch.
- Confirmed the local origin is `git@github.com:Alireza77n/Limit-Order-Book.git` for fetch and push.
- Confirmed authenticated read access to that repository from both the workstation and Ubuntu server. The server has Git `2.43.0`.
- Recorded GitHub's host key in `/home/ubuntu/.ssh/known_hosts` during the first server-side authenticated read using `StrictHostKeyChecking=accept-new`; this is the only server mutation since the Phase 1 audit.
- Confirmed remote `main` remains `ba44366ba266b4e00d9da9467a888ef4f8492727` and that neither `parsaoryani/algo1-refactor-testability` nor `production` exists remotely yet.
- Added the fail-closed Demo process entrypoint in commits `347652f` and `f4d7b90`. The initial task review found that absolute paths to `run.py` and lexical `..` runtime paths bypassed validation; both now have regression tests and the re-review is clean.
- Task 1 verification evidence: 26 focused entrypoint/Demo-safety/end-to-end tests passed; the full suite passed 191 tests with one pre-existing Dash deprecation warning. The zero-real-call end-to-end assertion remained exact.

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
- `dashboard.185-206-95-141.sslip.io` depends on third-party DNS. Certificate issuance and external resolution must be proven before it is accepted; failure is a stop condition, not a reason to fall back to Sarkhatti's `/` block.

## Rollback status

Nothing has been deployed, so current rollback is a no-op. Future rollback must be scoped exactly to:

```bash
cd /home/ubuntu/limit-order-book
docker compose -p limit-order-book down
```

The final rollback procedure may additionally remove only the dedicated Limit Order Book Nginx site symlink/file and its dedicated certificate after resolving and verifying their exact paths. It must re-run `nginx -t` before `systemctl reload nginx`. It must never reference `/home/ubuntu/sarkhatti`, container `sarkhatti`, network `sarkhatti_default`, or Sarkhatti's certificate.

## Exact next step

Implement separate result/state runtime roots test-first, then implement and validate the isolated production Compose model. Push verified commits to `parsaoryani/algo1-refactor-testability`, fast-forward `production` to the same SHA, push it, and fetch the exact production SHA on the server without touching `main`.
