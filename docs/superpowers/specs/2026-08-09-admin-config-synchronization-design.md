# Admin Configuration Synchronization — Design Specification

Status: approved for implementation on 2026-08-09. Demo/paper mode only.

## Goal

Make every Admin configuration control truthful and observable. An accepted
edit must be persisted once, consumed by the correct BTC or USDT worker,
reported as applied or pending, visible on the Dashboard, and preserved across
page refreshes, worker/container restarts, and session rotation. The Admin must
not display fields that are absent from the selected pair's profile or cannot
have a reliable effect in the managed continuous-worker deployment.

This work does not change Algorithm 1's quoting rules. It only changes how
existing configuration values reach the existing strategy, engine, ledger,
Dashboard, and session lifecycle.

## Current flow and root causes

The current intended flow is:

```text
Admin form
  -> runtime/control/commands/apply_config
  -> deploy/control_agent.py
  -> runtime/state/demo_config_<pair>.json
  -> worker restart
  -> run.make_cfg()
  -> MMConfig / SimpleMarketMaker / PaperLedger
```

The implementation breaks that contract in several places:

1. `demo_config.FIELD_SPECS` is a global 19-field UI allow-list. The Admin
   renders every entry for both pairs, even though each pair's actual
   `TRADING_PROFILES` entry contains only ten of them. One rendered field,
   `no_quote_recovery_alert`, has no runtime reader anywhere.
2. `_effective_config_values()` reconstructs an `MMConfig` differently from
   `run.make_cfg()`. It bypasses Demo transforms, session state, and the
   `--continuous` rule, so the Admin's “effective” JSON is not authoritative.
3. `apply_config` treats every edit identically: write the overlay and restart
   the worker. There is no field-level application mode and no applied/pending
   runtime acknowledgement.
4. A restart cannot apply session-bound values to an existing paper ledger.
   `PaperLedger.load()` restores its persisted commission and fill settings.
   `cash_initial` and `demo_inv_initial` are only used to create a new ledger.
   This can leave the strategy's `cfg.commission_perc` different from the
   ledger's persisted commission for the same session.
5. The Dashboard reads trade-result files only. It neither reads the desired
   overlay nor an applied runtime snapshot, and production Compose does not
   mount `runtime/state` into the Dashboard.
6. Local Compose runs the Admin but not the host control agent. Admin writes
   queue commands and waits until timeout; the overlay is never written.
7. `load_overrides()` converts missing, unreadable, malformed, and invalid
   documents into the same empty dictionary. A corrupt saved configuration can
   therefore silently fall back to defaults.
8. Agent lifecycle handlers do not consistently propagate stop/start/restart
   failures. A new-session command can report success even if a worker failed
   to stop or restart.
9. There is no durable record of what a running worker actually applied. The
   UI cannot distinguish desired, applied, pending-next-session, stopped, and
   failed states.

## Considered approaches

### 1. Restart after every edit

Keep the existing overlay and restart behavior, adding better messages. This is
small, but remains incorrect for values persisted inside the current ledger,
causes unnecessary trading interruptions, and cannot prove what was applied.

### 2. Give the Admin direct process/state mutation authority

Let the web app write state and control Docker or signal workers directly. This
would make local development convenient but violates the existing privilege
boundary: the Internet-facing Admin container must not get a Docker socket or
host process authority.

### 3. Central desired config plus worker-owned applied snapshots (selected)

Keep the per-pair overlay as the desired source of truth. Centralize field
metadata and resolution in `demo_config.py`. Let each worker safely hot-reload
the fields that are meaningful mid-session and atomically publish the exact
values it is using. Keep session-bound values pending until session rotation.
The Admin and Dashboard read the same resolver and worker snapshot.

This approach is selected because it is truthful, testable, minimally invasive
to trading logic, and preserves the control-agent security boundary.

## Editable schema

The Admin schema is derived from the selected pair's explicit
`TRADING_PROFILES[pair]` keys and a closed metadata table. A field must satisfy
both conditions to render or validate. This preserves future pair-specific
differences automatically: adding a metadata-supported field to only one
profile exposes it only for that pair.

### Remaining fields

| Admin field | Real configuration location | Runtime consumer | Mode |
|---|---|---|---|
| `order_size` | `TRADING_PROFILES[pair].order_size` + pair overlay | `SimpleMarketMaker._run_demo()` order sizing | Hot reload |
| `inv_max` | `TRADING_PROFILES[pair].inv_max` + pair overlay | `Algo1Strategy` inventory skew and engine buy cap | Hot reload |
| `inv_min` | `TRADING_PROFILES[pair].inv_min` + pair overlay | `Algo1Strategy` inventory skew and engine sell cap | Hot reload |
| `cancel_after_minutes` | `TRADING_PROFILES[pair].cancel_after_minutes` + pair overlay | `PaperLedger.cancel_stale()` | Hot reload |
| `depth_max` | `TRADING_PROFILES[pair].depth_max` + pair overlay | `Algo1Strategy.depth_cap()` | Hot reload |
| `time_step` | `TRADING_PROFILES[pair].time_step` + pair overlay | engine round scheduler | Hot reload |
| `demo_inv_initial` | `TRADING_PROFILES[pair].demo_inv_initial` + pair overlay | new `PaperLedger.initial_inventory` | New session |
| `cash_initial` | `TRADING_PROFILES[pair].cash_initial` + pair overlay | new `PaperLedger.initial_cash` | New session |
| `commission_perc` | `TRADING_PROFILES[pair].commission_perc` + pair overlay | strategy threshold, `PaperLedger`, Demo exchange | New session |

No remaining field requires a worker restart. The restart application mode
remains part of the schema/status model so a future real profile field cannot
be mislabeled, but this change does not invent a restart-only setting merely
to populate the category.

### Removed fields

These are inherited `MMConfig` defaults rather than explicit per-pair profile
configuration and are removed from the Admin:

- `demo_level1_fast_fill_enabled`
- `demo_level1_fill_delay_seconds`
- `demo_level1_fill_fraction`
- `demo_queue_enabled`
- `no_quote_alert_rounds`
- `no_quote_alert_seconds`
- `no_quote_alert_cooldown_seconds`
- `no_quote_recovery_alert` (also has no runtime consumer)
- `algo1_diagnostics_enabled`

`max_rounds` is also removed. It exists in both profiles, but Admin-managed
Docker workers are launched with `--continuous` and `restart: unless-stopped`.
A finite round cap therefore cannot reliably stop a managed worker and is an
unsupported control on this surface.

Existing overlay files containing removed keys are migrated on read/write:
known legacy keys are ignored for effective Admin configuration and omitted on
the next successful write. Unknown keys and malformed documents remain hard
errors; they never silently reset to defaults.

## Source of truth and data model

There are three intentionally distinct views, all produced by
`demo_config.py`:

1. **Base** — the selected `TRADING_PROFILES[pair]` values.
2. **Desired** — base plus the validated `demo_config_<pair>.json` overlay.
3. **Applied** — `runtime_config_<pair>.json`, atomically published by the
   worker and containing what its engine and ledger are actually using.

The overlay is the only writable desired configuration. For compatibility it
remains a bare map containing only validated editable values; its pair identity
comes from the fixed `demo_config_<pair>.json` path selected by trusted code.
Admin/control-agent callers always supply the pair separately and validate the
map against that pair before writing. Worker runtime acknowledgements are the
self-identifying envelopes: they include schema version, pair, session id,
values, update timestamp, and deterministic desired-config digest. A runtime
snapshot is not another source of defaults and is never edited by the Admin.

`load_desired_config(pair, state_dir)` is the only merge path. `run.make_cfg`,
Admin rendering, Dashboard rendering, the control agent, and tests all call
this shared code rather than duplicating profile/default merging.

## Apply protocol

### Admin and control agent

1. Admin validates submitted keys against the selected pair's derived schema.
2. Admin submits the complete desired editable set to `apply_config`.
3. The control agent independently validates it, atomically writes the overlay,
   and returns the desired digest plus per-field modes. It does not restart a
   worker when all changed fields are hot or new-session-bound.
4. The Admin reads desired and applied snapshots and renders one of:
   `Applied now`, `Saved; waiting for running worker`, `Saved; applies next
   session`, `Worker stopped; applies when started`, or `Failed to apply`.
5. Rollback follows the same protocol and status calculation.

Validation is cross-field as well as per-field: `inv_min < inv_max`,
`order_size > 0`, nonnegative initial balances, `commission_perc` in `[0, 1]`,
positive `depth_max`, and positive `time_step`. A failed write or apply command
does not modify the previous overlay.

### Worker hot reload

At a safe boundary before each Demo round, the worker checks the desired digest.
If it changed, it validates and copies only hot fields onto the existing
`MMConfig`. Strategy objects already hold that same config object, so Algorithm
1 and the engine observe the update on the next decision without reconstruction.

The reload is all-or-nothing. Invalid/unreadable desired config leaves the
previous runtime values active and publishes a failed status. `time_step`
changes the next scheduled interval; it does not interrupt an in-progress
sleep. No existing orders are rewritten.

After reload, the worker atomically publishes its applied snapshot. Hot values
match desired. Session-bound values continue to report the values owned by the
current ledger, with the differing desired values listed under
`pending_new_session`.

### Restart and new session

A worker restart in the same session rebuilds engine configuration and reloads
the same ledger. Hot fields use the latest desired values. The runtime snapshot
uses ledger-owned commission/account state for session-bound truth, avoiding a
cfg/ledger commission split.

`new_session` stops the running workers and aborts on any stop failure. It then
creates the new shared session and starts exactly the workers that were running.
Fresh ledgers consume desired `demo_inv_initial`, `cash_initial`, and
`commission_perc`. Each worker publishes a snapshot with no pending fields.
Any start failure is returned as a failed/partial lifecycle result and shown in
the Admin; it is never reported as success.

Docker/container restart does not change the desired overlay. Restarting only a
worker preserves the current shared session and ledger. Recreating the entire
stack follows the existing session initializer semantics; the applied snapshot
always identifies which session consumed which settings.

## Dashboard

Add a compact “Effective Demo Configuration” section for the selected Demo
pair. It displays desired, applied, and pending values plus worker/session
status. Real-history tracks do not show Demo configuration.

Dashboard auto-refresh reads the same resolver and applied snapshots, so no
service restart is required to refresh the display. Production Dashboard gets a
read-only `runtime/state` mount; local Dashboard already shares the results/state
directory. The Dashboard remains read-only.

## Local control-agent support

The host agent gains an explicit local mode that uses the root Compose file and
`market_maker/results` for state/results/control/backups. Production remains the
default and keeps its fixed production argv. Local mode still runs as a separate
host process; neither Admin nor Dashboard receives a Docker socket.

Documentation and an executable validation command will start the local agent
beside Compose for Admin lifecycle/config actions. This closes the current local
timeout gap without weakening production isolation.

## Failure handling

- Missing overlay: use profile base values.
- Malformed, unreadable, wrong-shaped, or unknown-key overlay: explicit error;
  keep the last applied runtime config.
- Atomic overlay write failure: command fails and previous file remains intact.
- Worker reload failure: runtime remains on the last applied snapshot and
  publishes the error.
- Stopped worker: save desired configuration and report that it will apply on
  start; never claim immediate application.
- New-session stop/start failure: return exact pair/action failures and do not
  claim full success.
- Missing/stale runtime snapshot: show `unknown/stale`, not desired-as-applied.

## Test strategy

Tests are added before implementation and observed failing for the expected
missing behavior.

1. Schema contract: every rendered field is present in that pair profile,
   carries metadata, and has a real runtime consumer; all ten removed fields are
   absent from HTML and rejected if newly submitted.
2. Persistence: atomic overlay round-trip, legacy-key cleanup, corrupt/unknown
   document errors, rollback, refresh, and BTC/USDT isolation.
3. Hot reload: each of the six hot fields changes the next applicable engine or
   strategy behavior without reconstructing the worker; invalid batches apply
   nothing.
4. Same-session restart: hot values survive and apply; current ledger commission
   remains authoritative while a desired commission change stays pending.
5. New session: desired inventory, cash, and commission create the new ledger
   and clear pending status.
6. Dashboard: selected Demo pair shows desired/applied/pending configuration and
   auto-refresh observes snapshot changes; pair isolation is preserved.
7. Admin status: applied, pending, stopped, stale, and failed states render
   truthfully; no failed command produces a success flash.
8. Control agent: no unnecessary restart for hot/session-bound edits, rollback
   parity, local/production fixed argv, and lifecycle failure propagation.
9. Deployment contracts: read-only Dashboard state mount, no Docker socket in
   web services, and local-agent invocation documentation.
10. End-to-end Docker: submit BTC and USDT edits through authenticated Admin,
    inspect overlays and runtime snapshots, observe Dashboard, restart workers,
    rotate session, and verify persistence and ledger consumption.

## Git scope

The repository already contains unrelated modified/deleted/untracked files.
Commits for this work will stage explicit paths only. No unrelated working-tree
change, runtime result, screenshot, credential, or generated control-queue file
will be included.

## Remaining limitations

- Admin configuration remains Demo-only; Real Mode profiles are never mutated.
- Runtime hot reload occurs at a round boundary, not midway through a decision or
  sleep.
- Local lifecycle actions require the separate local host agent to be running.
- Existing legacy overlay keys are cleaned but not promoted into new per-pair
  profile settings.
- Configuration history remains the existing single `.previous` rollback slot.
