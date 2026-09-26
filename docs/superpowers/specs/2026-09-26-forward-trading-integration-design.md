# Forward Trading Integration Design

## Goal

Merge the complete reviewed `/opt/market-maker-forward` implementation into the Git project, repair the defects found during review, configure Nobitex API-key authentication securely, and redeploy the isolated forward services on the existing server hostname.

## Scope

The integration includes the teammate's live-forward control plane for BTC/IRT and USDT/IRT:

- Ed25519 Nobitex API-key authentication and signed private API requests.
- Account wallet snapshots before live runs and on live-config page loads.
- Dedicated forward configuration, command queue, process locks, logs, and result storage.
- Live-config and forward-control admin pages.
- A read-only forward results dashboard.
- Dedicated agent, admin, and dashboard systemd units.
- Live-order ownership checks and shutdown cancellation.
- Exposure guards that are mandatory and capped at 25%.
- Tests supplied with the forward implementation.

Generated environments, runtime state, result files, credentials, experimental notebooks, and backup source files are not source code and will not be merged.

## Repository Integration

The teammate's changes will be integrated into the existing project rather than replacing it wholesale. Existing Demo behavior and deployment files remain intact. Forward-specific files are added alongside them, while shared modules receive reviewed changes needed by both modes.

The merge will include new forward modules, templates, service definitions, environment examples, authentication tests, and changes to shared exchange, engine, admin, dashboard, configuration, portfolio, order-status, and runner modules.

The integration branch is `feat/forward-trading-integration`, based on deployed commit `9887ea7`.

## Required Repairs

### Control-directory isolation

Forward admin actions must always use `LOB_FORWARD_CONTROL_DIR`. Legacy Demo routes must not fall back to a relative `runtime/control` path inside the hardened forward-admin service. Forward-only deployments will hide or reject Demo mutation routes cleanly rather than attempting a write to the read-only application directory.

### Worker lock lifecycle

The control agent must reap completed child processes and remove stale lock files after normal exit, failed startup, or SIGTERM shutdown. Before treating a lock as active, it must verify that its recorded PID is alive and belongs to the expected worker. Regression tests will cover stale locks and stopped workers.

### Nobitex commission handling

Nobitex fees are based on quote market, rolling account tier, and maker/taker status; there is no single BTC-specific rate. Pre-trade profitability and risk checks will use a conservative configured rate appropriate to the market. Actual fills will be reconciled from Nobitex's returned monetary fee. The configuration will keep BTC/IRT and BTC/USDT fee assumptions explicit and separate where applicable.

The authenticated profile fee fields may inform configuration, but they will not be treated as a complete maker/taker schedule because the API does not document separate fields for all four rates.

### DNS readiness

Deployment verification must prove the server can resolve and establish TLS connectivity to `api.nobitex.ir`. Forward trading remains disabled if DNS or connectivity checks fail.

## Credential Handling

The API and secret keys are never committed, logged, placed in command output, or copied into example files. They are stored only in `/etc/market-maker-forward.env`, using strict `KEY=value` syntax without spaces around `=`, owned by root with mode `0600`.

Only the forward agent receives the trading credentials. The admin and dashboard services do not receive them. Logs and verification output expose only presence and authentication status, never values or signatures.

Because the credentials were pasted into chat, rotating them after deployment is recommended.

## Deployment Architecture

The existing Demo deployment remains active. The forward system runs beside it:

- `market-maker-forward-agent`: credentialed command agent and live worker supervisor.
- `market-maker-forward-admin`: authenticated control UI on `127.0.0.1:18653`.
- `market-maker-forward-dashboard`: read-only charts on `127.0.0.1:18652`.

The initial public hostname is `server-27920007-d64a-40b2-bf0e-50326d9d3d83.ir-thr-fr1.arvancompute.ir`. Nginx will expose the forward interfaces without exposing the credentialed agent. Existing routes must continue working. A configuration test and local HTTP probes are required before reload.

## Deployment Safety and Rollback

Before deployment, the current application tree, systemd units, Nginx configuration, and environment file will be backed up to a timestamped server directory. Source deployment will be atomic where practical.

Services will be restarted in dependency order and checked with `systemctl`, journal logs, listening sockets, and HTTP probes. Existing Demo health checks must pass after deployment.

The three forward services may run automatically, but no BTC or USDT live worker will be started automatically. Placing real orders requires a separate authenticated start action with the existing confirmation phrase.

If verification fails, services and configuration will be restored from the backup and the previous deployment restarted.

## Testing and Acceptance

Acceptance requires:

1. All existing and new unit tests pass locally.
2. Authentication signing tests cover exact method, path, query, and request body.
3. Regression tests prove stale locks are cleared and forward paths never use relative Demo control storage.
4. No credential value is present in the Git diff or repository history.
5. Server DNS and TLS connectivity to Nobitex succeed.
6. All three forward services are active without tracebacks.
7. Existing Demo services remain healthy.
8. Admin and dashboard endpoints respond through Nginx on the approved hostname.
9. Wallet reads succeed, while no real order is placed during deployment verification.

