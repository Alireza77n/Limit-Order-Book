# Admin configuration synchronization validation — 2026-08-09

## Outcome

The local Docker stack, authenticated Admin API, local host control agent,
worker runtime snapshots, Dashboard callback, same-session restart,
new-session ledger initialization, and full Compose recreation were exercised
end to end. BTC and USDT remained pair-isolated. Six fields applied at round
boundaries and three stayed pending until a new session. No current field
requires a restart.

This was a local validation only. No production host, Nginx, systemd unit, or
real-order path was changed or exercised.

## Root causes found

1. `test_runtime_paths.py` still supplied the pre-synchronization fake-bot
   contract. `run.main()` now calls
   `config_sync.reload_at_round_boundary()` before `run()`, so the stub failed
   with `AttributeError`. The fixture now supplies that public interface.
2. A newly constructed paper ledger was not persisted until its first
   state-changing round. If a worker restarted first, a missing same-session
   ledger was reconstructed from future-session desired values. `make_bot()`
   now persists the initial ledger seed immediately; the regression test
   failed with `(7, 900, 0.02)` versus `(0, 30000000, 0.001)` before the fix.
3. Production liveness uses the worker's exclusive `flock`. Docker Desktop on
   macOS does not expose a container-held bind-mount flock to the host, so the
   local agent incorrectly reported both healthy workers stopped and rotated a
   session without restarting them. Local mode now queries the fixed Compose
   service allow-list; production keeps the authoritative lease check.
4. `docker compose start <worker>` could start the worker's `session-init`
   dependency and rotate the stack session after `new_session` had already
   returned an ID. The fixed start action is `compose up -d --no-deps
   --no-recreate <worker>`, keeping session rotation explicit and truthful.

Algorithm 1 logic and the Admin web service's read-only/mutation authority
boundary were not changed.

## Field and mode mapping

| Field | Mode | Runtime behavior |
|---|---|---|
| `order_size` | hot | Next worker round boundary |
| `inv_max` | hot | Next worker round boundary |
| `inv_min` | hot | Next worker round boundary |
| `cancel_after_minutes` | hot | Next worker round boundary |
| `depth_max` | hot | Next worker round boundary |
| `time_step` | hot | Next worker round boundary |
| `demo_inv_initial` | new session | New paper ledger only |
| `cash_initial` | new session | New paper ledger only |
| `commission_perc` | new session | New paper ledger only |

Synchronization flow:

1. Admin validates a complete pair schema and queues `apply_config`.
2. The host agent independently validates and atomically writes only that
   pair's `demo_config_<pair>.json`, returning a stable desired digest.
3. The worker reloads at a round boundary, mutates only hot config, and writes
   `runtime_config_<pair>.json` with applied/pending values, session identity,
   digest, timestamp, status, and error.
4. Admin and Dashboard compare desired values with only a current-session,
   current-shape worker snapshot. Missing, malformed, wrong-pair, or
   wrong-session snapshots are stale rather than evidence of application.
5. `new_session` stops workers, rotates the shared session, and starts workers
   dependency-free. The new ledgers consume all three pending values.

## Automated and Compose evidence

Commands run:

```text
python3 -m pytest -q market_maker/tests/test_demo_config.py \
  market_maker/tests/test_runtime_config_sync.py \
  market_maker/tests/test_control_agent.py \
  market_maker/tests/test_admin_auth.py \
  market_maker/tests/test_admin_actions.py \
  market_maker/tests/test_admin_html_views.py \
  market_maker/tests/test_dashboard.py \
  market_maker/tests/test_deployment_contract.py \
  market_maker/tests/test_runtime_paths.py
python3 -m pytest -q
LOB_TELEGRAM_DISABLED=0 python3 -m pytest -q
docker compose config --quiet
docker compose -f deploy/docker-compose.production.yml \
  --env-file deploy/demo.env config --quiet
```

Initial focused result: `231 passed, 1 failed` at the stale runtime-path stub.
After its focused fix: `232 passed`. The final focused run, including the two
Docker-discovered regressions, was `234 passed, 2 warnings` in 84.03 seconds.

The final exact default-environment full run produced `454 passed, 6 failed,
2 warnings` in 96.81 seconds. All six are `test_telegram_lifecycle.py` and are caused by the
ignored local `market_maker/.env` setting `LOB_TELEGRAM_DISABLED=1`. The same
six reproduced against pre-Task-1 source (`c480edd^`) with that flag, while the
current Telegram module passed `12/12` with the flag set to `0`. The final
clean-environment full run was `460 passed, 2 warnings` in 96.24 seconds. The
kill switch and its tests were intentionally not changed.

Both Compose configuration commands exited `0` with no output.

## Local end-to-end evidence

The root stack was built with `docker compose up -d --build --wait`. All four
long-running services were healthy. The host agent ran only for validation:

```text
python3 deploy/control_agent.py --mode local
```

Authentication with the approved local `admin/admin` credentials returned
`GET /admin/login = 200`, `POST /admin/login = 302`, and authenticated BTC and
USDT config GETs returned `200`. No password hash, cookie, CSRF value, or other
secret is recorded here.

Validated desired values:

| Pair | Hot values | New-session values | Desired digest |
|---|---|---|---|
| BTC | `order_size=.00001`, `inv_max=.0005`, `inv_min=.000001`, `cancel_after_minutes=8`, `depth_max=7`, `time_step=5` | `demo_inv_initial=.0001`, `cash_initial=1500000`, `commission_perc=.0012` | `690bad870e61c7e473672a08781a08e2f6083707b2447c4445c1b0516f1aee33` |
| USDT | `order_size=.5`, `inv_max=20`, `inv_min=1`, `cancel_after_minutes=7`, `depth_max=6`, `time_step=4` | `demo_inv_initial=4`, `cash_initial=2200000`, `commission_perc=.002` | `7d0671d00f38d767721c0773a0cac6f77e12483080c4ab84791d88f5fb5ef54b` |

The final ordered run used baseline session
`demo-20260809T153134Z-87ea2121eca2`:

- Config POSTs returned `200`: BTC command
  `fb83efff58454e2facb62613f27a57b9`, USDT command
  `7514fd17549646968531302b95e26ce6`.
- Overlay file SHA-256 values were
  `50807282b63a5fded00744292ce71e3316adffc6edc4d6a6737d48e48b7d3dd2`
  (BTC) and
  `ef825cd4d59a1b830a05a4b636e6d4514e443ff64e49e4ad21e8c232c218e8dd`
  (USDT). Each JSON object contained exactly the nine values for its pair.
- Both hot snapshots applied within their next boundary; all three
  new-session values remained pending.
- Admin-driven restarts returned `200` with command IDs
  `7a455758591f4439bcfc4450deee00d3` and
  `4007f76a7d5544daaa05014fbc2001e2`. The session stayed unchanged and both
  pairs retained pending values.
- Dashboard layout/dependency GETs and both multi-output callbacks returned
  `200`. Pending callback hashes were
  `5678cc694d25640e9a8ce436e022fda58f36d10615a62cc129abf465e1624f97`
  (BTC) and
  `a5fe513ff72cc82bc381dfa809fe0c76b8b4d2d059c38eeb91fcca7ce0505d94`
  (USDT); displayed desired/applied/mode/status rows matched the snapshots.
- `new_session` command `b95acadbd0c34390b379c09548b02500`
  returned `200` and changed the session to
  `demo-20260809T153316Z-fe0bd204a607`. New BTC/USDT ledgers contained the
  requested inventory, cash, and commission, and pending values became empty.
  Applied Dashboard callback hashes were
  `acf105a8539213ed686e3edf06b03faf0f55c8c2f8a73246af260a3c77ce6987`
  and `51b3c3f161d469cb60d588e9cd122451020c0c9541839a5157daf1ebbbac8270`.
- `docker compose up -d --force-recreate --wait` recreated the stack. It
  intentionally ran `session-init`, producing
  `demo-20260809T153343Z-582c6cc7f49c`; both overlays retained the same
  hashes/digests, both snapshots were fully applied, all long-running
  services were healthy, Admin health was `200`, and Dashboard layout was
  `200`.

## Restoration and limitations

No runtime directory or prior ledger/result was deleted. Before the agent was
started, two older queued `new_session` requests were moved to
`/tmp/lob-task7-runtime-backup.vFTzgJ` so validation would not execute them.
After validation, both pairs were submitted with their original profile
defaults. The generated overlay and
`.previous` files were moved into that backup directory, restoring the exact
pre-validation state of **no overlay files**. The original two queued command
files were returned unexecuted. After the full test suite's deterministic
fixtures wrote test-owned snapshot identities, a final default session
`demo-20260809T154543Z-76cc1054a8c6` was created so both worker snapshots once
again acknowledge the live session and default digests (`986019…` BTC,
`c5ceab…` USDT); the local agent was then stopped.

Historical validation ledgers and audit/result records remain because runtime
data preservation was required. The final stack session necessarily differs
from the pre-validation session; restoring it would make the retained new
ledger/session evidence inconsistent. Production systemd/Nginx behavior and
real Nobitex/MySQL paths remain outside this local Demo-only validation.
