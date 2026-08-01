# Isolated Production Demo Deployment Design

Date: 2026-08-01

## Purpose

Deploy the Limit Order Book application to the Ubuntu host at `185.206.95.141` alongside Sarkhatti without modifying, recreating, restarting, stopping, or sharing state with Sarkhatti. The deployed application is a production-operated Demo/Paper stack: it may consume public market data but must be unable to authenticate to Nobitex, place or cancel real orders, mutate real wallets, or connect to production MySQL.

## Safety boundary

Sarkhatti is an immutable external dependency. Its baseline is recorded in `docs/deployment/DEPLOYMENT_PROGRESS.md` and includes container ID, start time, restart count, ports, mount, health, resource sample, latency, and SHA-256 hashes for its Compose file, `.env`, and Nginx site. Every server-changing deployment stage must compare the current state with that baseline and stop immediately on a container ID, start time, restart count, port, mount, file hash, health, OOM, or material latency regression.

The Limit Order Book deployment owns only:

- `/home/ubuntu/limit-order-book`
- Compose project `limit-order-book`
- containers prefixed `lob-`
- network `limit-order-book-internal`
- host port `127.0.0.1:8652`
- `/etc/nginx/sites-available/lob-dashboard`
- `/etc/nginx/sites-enabled/lob-dashboard`
- `/etc/nginx/lob-dashboard.htpasswd`
- `/var/www/lob-dashboard-acme`
- the certificate lineage for `dashboard.185-206-95-141.sslip.io`

No deployment or rollback command may use a broad Docker prune operation or reference `/home/ubuntu/sarkhatti`, container `sarkhatti`, network `sarkhatti_default`, Sarkhatti's ports, database, certificate, `.env`, Compose file, or Nginx site as a mutation target.

## Git workflow

Implementation occurs on `parsaoryani/algo1-refactor-testability`, the currently checked-out branch. Existing unrelated runtime artifacts remain untracked and untouched. Deployment changes are committed in focused commits after tests pass. A `production` branch is created or fast-forwarded to the exact verified deployment commit and pushed when repository credentials permit. `main` is never checked out, modified, merged, rebased, reset, or pushed.

The server receives an archive of the exact `production` commit, so deployment does not depend on mutable working-tree files or server-side Git credentials. The deployed commit SHA is recorded in the progress journal and a release metadata file.

## Application safety

Compose invokes a dedicated Demo entrypoint for both workers. The entrypoint fails closed unless all of these conditions hold:

- command is a worker command containing `--mode demo`
- command does not contain `--mode real`
- `NOBITEX_API_TOKEN` is absent or empty
- all MySQL connection variables are absent or empty
- the Demo session/state paths are inside the dedicated runtime root

The production environment file contains only non-secret Demo configuration and never contains exchange, wallet, Telegram, or database credentials. The application continues to use `DemoExchangeClient`, whose trading capability is disabled and whose simulated order/cancel operations affect only the local paper ledger. Public order-book reads remain allowed.

Automated tests must prove startup rejection for real mode, credentials, MySQL configuration, and paths outside the deployment runtime. Existing and focused tests must prove real exchange order, real cancellation, authenticated wallet, and MySQL call counts remain exactly zero in Demo mode. A container smoke test repeats these assertions using the built image before server startup.

## Compose architecture

The stack uses a deployment-specific Compose file, explicit project name `limit-order-book`, and these containers:

- `lob-session-init`: one-shot shared-session initializer
- `lob-btc-demo`: continuous BTC/IRT Demo worker
- `lob-usdt-demo`: continuous USDT/IRT Demo worker
- `lob-dashboard`: existing Dash application

All services join only `limit-order-book-internal`. No service uses host networking, privileged mode, Docker socket mounts, VNC, Chromium, Xvfb, Playwright, or relaxed seccomp. Only the dashboard publishes a host port, bound exactly as `127.0.0.1:8652:8050`. Workers publish no ports.

Bind mounts are confined to `/home/ubuntu/limit-order-book/runtime` and are split into `results`, `state`, and `logs` where supported by the application. Nothing under `/home/ubuntu/sarkhatti` is mounted or referenced. The image build excludes Git data, local environments, caches, reports, notebooks, tests not needed for runtime, and all runtime result directories.

Each long-running service uses `restart: unless-stopped`, a meaningful health check, `init: true`, a bounded stop grace period, JSON-file logging capped by size and file count, read-only root filesystem where compatible, and per-service memory/PID/CPU limits. Normal long-running limits total no more than 448 MiB: 128 MiB per worker and 192 MiB for Dash. The one-shot initializer has a 64 MiB limit and is not part of normal steady-state use.

## Shared session and state

`lob-session-init` runs the existing locked stack initializer against one session document in the dedicated results directory. Both workers depend on successful completion and receive the same `DEMO_SESSION_FILE`. Worker lease files remain in the same dedicated results mount. Validation compares the session document, both active ledgers, and the latest BTC/USDT result rows to prove one shared session. Repeated worker health checks and restarts must not generate a new session while either worker is active.

## Nginx, TLS, and authentication

The dashboard uses `dashboard.185-206-95-141.sslip.io`, an address-encoded DNS hostname documented by sslip.io to resolve to `185.206.95.141`. This avoids modifying Sarkhatti's existing hostname and root proxy. It is an operational dependency on third-party DNS and should later be replaced with an operator-controlled hostname.

Nginx receives a new standalone site file. The initial HTTP-only configuration serves the ACME challenge from `/var/www/lob-dashboard-acme`; it does not alter the Sarkhatti file. After `nginx -t`, Nginx is reloaded, never restarted. Certbot obtains a separate certificate using HTTP-01 and the webroot plugin. The final site redirects HTTP to HTTPS, keeps the ACME exception, requires Basic Auth, sets conservative proxy/security headers, supports Dash connection behavior, and proxies only to `http://127.0.0.1:8652`.

A cryptographically random Basic Auth password is generated once, stored only as an htpasswd hash on the server with root-readable permissions, and returned to the operator in the final handoff. Nginx configuration and the Sarkhatti baseline are backed up to the Limit Order Book deployment's backup directory before changes. `nginx -t` must pass before every reload.

## Deployment sequence

1. Revalidate Sarkhatti and host capacity against the recorded baseline.
2. Run local unit/safety tests and validate the deployment Compose model.
3. Build the image locally, run container safety smoke tests, and measure image size.
4. Commit the verified implementation on the `parsaoryani` branch.
5. Create or fast-forward `production` to that exact commit without touching `main`.
6. Transfer an archive of the exact production commit into a new `/home/ubuntu/limit-order-book` directory.
7. Create dedicated runtime directories and the credential-empty production environment.
8. Validate `docker compose -p limit-order-book config` on the server.
9. Build only the new image, revalidate Sarkhatti, then start only the new Compose project.
10. Revalidate Sarkhatti after initializer completion and after long-running services become healthy.
11. Back up Nginx state, install the isolated HTTP site, test, and reload.
12. Obtain the dedicated certificate, install the authenticated HTTPS site, test, and reload.
13. Validate internal and external dashboard access, authentication, Demo safety evidence, shared session, resource limits, logs, and absence of crash loops.
14. Monitor both projects for a bounded observation period and state explicitly that this is not proof of long-term readiness.

## Failure handling

Every stage is fail-closed. If Sarkhatti is unhealthy or changed, resources fall below safe thresholds, Compose validation fails, Demo safety tests fail, image build fails, the new stack crash-loops, memory exceeds the budget, DNS/certificate issuance fails, or Nginx validation fails, stop before the next mutation. Do not compensate with global Docker cleanup, broader file operations, Sarkhatti changes, public Docker port exposure, HTTP-only public access, or disabling security controls.

## Rollback

Rollback is prepared before startup and targets only the explicit `limit-order-book` project from `/home/ubuntu/limit-order-book`. It removes the dedicated Nginx symlink/file and Basic Auth file only after resolving and verifying each exact path, tests Nginx, and reloads it. Certificate deletion is optional and uses the exact certificate name only. Runtime state is retained by default for recovery and evidence; removal requires a separate explicit decision. Rollback finishes by proving Sarkhatti still matches its immutable baseline.

## Acceptance criteria

Completion requires all of the following evidence:

- `main` remains unchanged and deployment commits exist on `parsaoryani` and `production`
- production Compose configuration validates
- only `127.0.0.1:8652` is published by the new stack
- all long-running new containers are healthy with no restart/OOM loop
- steady-state new-stack memory remains below 512 MiB
- BTC and USDT use one stable shared session
- real order, cancellation, authenticated wallet, real wallet mutation, and MySQL operation counts are zero
- HTTPS works at the dedicated hostname and rejects unauthenticated requests
- authenticated dashboard access succeeds
- Sarkhatti retains its original container ID, start time, restart count, ports, mount, file hashes, health, and acceptable resource/latency behavior
- rollback instructions are exact, scoped, recorded, and dry-reviewed
- the handoff states the observation duration and does not claim long-term readiness
