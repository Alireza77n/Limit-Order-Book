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
   ledger was reconstructed from future-session desired values. The worker
   entry path now calls the public `PaperLedger.persist_initial_state()` only
   after acquiring `WorkerLease` and before config reload/run. Construction
   remains read-only, so a concurrent process that loses the lease cannot
   write shared ledger state. The original regression failed with
   `(7, 900, 0.02)` versus `(0, 30000000, 0.001)` before the fix.
3. Production liveness uses the worker's exclusive `flock`. Docker Desktop on
   macOS does not expose a container-held bind-mount flock to the host, so the
   local agent incorrectly reported both healthy workers stopped and rotated a
   session without restarting them. Local mode now queries the fixed Compose
   service allow-list; production keeps the authoritative lease check.
4. A failed local Compose status query was initially treated as “worker
   stopped.” This allowed destructive actions to continue while liveness was
   indeterminate, and recovery could unlink one pair's lock before a later
   query failed. Status exceptions and nonzero exits now produce an explicit
   `liveness` failure; every destructive handler resolves all required worker
   statuses before changing sessions, files, backups, or locks.
5. `docker compose start <worker>` could start the worker's `session-init`
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
   dependency-free. After each worker owns its lease, the new ledger seed is
   persisted before config reload/run and consumes all three pending values.

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

Standards-review follow-up RED/GREEN evidence:

```text
python3 -m pytest -q market_maker/tests/test_control_agent.py \
  -k 'liveness or preflights'
# RED: 13 failed, 1 passed, 29 deselected
# GREEN: 14 passed, 29 deselected in 0.84s

python3 -m pytest -q \
  market_maker/tests/test_runtime_config_sync.py::test_make_bot_does_not_persist_ledger_before_worker_lease \
  market_maker/tests/test_runtime_paths.py::test_demo_worker_lease_uses_the_state_root
# RED before the first test was strengthened/renamed: 2 failed

python3 -m pytest -q \
  market_maker/tests/test_runtime_config_sync.py::test_competing_make_bot_constructions_do_not_persist_before_worker_lease \
  market_maker/tests/test_runtime_paths.py::test_demo_worker_lease_uses_the_state_root \
  market_maker/tests/test_runtime_config_sync.py::test_same_session_restart_before_first_round_keeps_session_fields_pending \
  market_maker/tests/test_runtime_config_sync.py::test_same_session_restart_keeps_commission_pending
# GREEN: 4 passed in 1.49s

python3 -m pytest -q market_maker/tests/test_control_agent.py \
  market_maker/tests/test_runtime_config_sync.py \
  market_maker/tests/test_runtime_paths.py
# GREEN: 70 passed in 3.36s

python3 -m pytest -q market_maker/tests/test_demo_config.py \
  market_maker/tests/test_runtime_config_sync.py \
  market_maker/tests/test_control_agent.py \
  market_maker/tests/test_admin_auth.py \
  market_maker/tests/test_admin_actions.py \
  market_maker/tests/test_admin_html_views.py \
  market_maker/tests/test_dashboard.py \
  market_maker/tests/test_deployment_contract.py \
  market_maker/tests/test_runtime_paths.py
# FINAL FOCUSED: 248 passed, 2 warnings in 84.82s
```

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

### Reproducible local E2E commands

The following is the repeatable command sequence used for the checks above.
Credentials are read interactively into protected temporary files and never
placed in command arguments or exported environment variables. Before
starting the agent, inspect `market_maker/results/control/commands/`; move each
pre-existing command to a named temporary backup and record its exact path so
it cannot execute during validation.

```bash
set -euo pipefail
export LOB_ADMIN_URL=http://127.0.0.1:8051
export LOB_DASHBOARD_URL=http://127.0.0.1:8050
export LOB_STATE_DIR=market_maker/results
VALIDATION_BACKUP="$(mktemp -d)"
export LOB_ADMIN_URL LOB_DASHBOARD_URL LOB_STATE_DIR VALIDATION_BACKUP
umask 077
AGENT_PID=''
cleanup_validation() {
  if [ -n "${AGENT_PID:-}" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
    kill "$AGENT_PID"
    wait "$AGENT_PID" || true
  fi
  rm -f -- "$VALIDATION_BACKUP/admin-user" "$VALIDATION_BACKUP/admin-password"
  python3 - <<'PY'
import json, os, shutil, uuid
from pathlib import Path
state = Path(os.environ["LOB_STATE_DIR"])
backup = Path(os.environ["VALIDATION_BACKUP"])
for name in ("demo_config_btc.json", "demo_config_btc.json.previous",
             "demo_config_usdt.json", "demo_config_usdt.json.previous"):
    target, source = state / name, backup / name
    if (backup / f"{name}.absent").exists():
        target.unlink(missing_ok=True)
    elif source.exists():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
manifest = backup / "queued-command-paths.json"
if manifest.exists():
    for original_name in json.loads(manifest.read_text()):
        original = Path(original_name)
        quarantined = backup / "queued-commands" / original.name
        if quarantined.exists() and not original.exists():
            os.replace(quarantined, original)
PY
}
trap cleanup_validation EXIT INT TERM

read -r -p 'Admin username: ' LOB_ADMIN_USER
read -rs -p 'Admin password: ' LOB_ADMIN_PASSWORD; printf '\n'
printf '%s' "$LOB_ADMIN_USER" > "$VALIDATION_BACKUP/admin-user"
printf '%s' "$LOB_ADMIN_PASSWORD" > "$VALIDATION_BACKUP/admin-password"
unset LOB_ADMIN_USER LOB_ADMIN_PASSWORD

# Back up the four exact overlay targets, including recording original absence.
python3 - <<'PY'
import os, shutil
from pathlib import Path
state = Path(os.environ["LOB_STATE_DIR"])
backup = Path(os.environ["VALIDATION_BACKUP"])
for name in ("demo_config_btc.json", "demo_config_btc.json.previous",
             "demo_config_usdt.json", "demo_config_usdt.json.previous"):
    source = state / name
    if source.exists():
        shutil.copy2(source, backup / name)
    else:
        (backup / f"{name}.absent").touch()
PY

# Only now, after the cleanup trap exists, quarantine queued commands.
python3 - <<'PY'
import json, os
from pathlib import Path
state = Path(os.environ["LOB_STATE_DIR"])
backup = Path(os.environ["VALIDATION_BACKUP"])
commands = state / "control" / "commands"
quarantine = backup / "queued-commands"
quarantine.mkdir()
manifest = []
for source in sorted(commands.glob("*.json")):
    target = quarantine / source.name
    os.replace(source, target)
    manifest.append(str(source))
(backup / "queued-command-paths.json").write_text(json.dumps(manifest))
PY

docker compose up -d --build --wait
python3 deploy/control_agent.py --mode local \
  > "$VALIDATION_BACKUP/control-agent.log" 2>&1 &
AGENT_PID=$!

COOKIE_JAR="$VALIDATION_BACKUP/admin.cookies"
curl -fsS -c "$COOKIE_JAR" "$LOB_ADMIN_URL/admin/login" \
  > "$VALIDATION_BACKUP/login.html"
LOGIN_CSRF="$(python3 - "$VALIDATION_BACKUP/login.html" <<'PY'
import re, sys
print(re.search('name="csrf_token" value="([^"]+)"', open(sys.argv[1]).read()).group(1))
PY
)"
curl -sS -o /dev/null -w 'login HTTP %{http_code}\n' -b "$COOKIE_JAR" \
  -c "$COOKIE_JAR" -X POST "$LOB_ADMIN_URL/admin/login" \
  --data-urlencode "username@$VALIDATION_BACKUP/admin-user" \
  --data-urlencode "password@$VALIDATION_BACKUP/admin-password" \
  --data-urlencode "csrf_token=$LOGIN_CSRF"
rm -f -- "$VALIDATION_BACKUP/admin-user" "$VALIDATION_BACKUP/admin-password"
curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  "$LOB_ADMIN_URL/admin/config/btc" \
  > "$VALIDATION_BACKUP/btc-before.json"
curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  "$LOB_ADMIN_URL/admin/config/usdt" \
  > "$VALIDATION_BACKUP/usdt-before.json"
CSRF="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["csrf_token"])' "$VALIDATION_BACKUP/btc-before.json")"

curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  -X POST "$LOB_ADMIN_URL/admin/config/btc" \
  -d "csrf_token=$CSRF" -d order_size=.00001 -d inv_max=.0005 \
  -d inv_min=.000001 -d cancel_after_minutes=8 -d depth_max=7 \
  -d time_step=5 -d demo_inv_initial=.0001 -d cash_initial=1500000 \
  -d commission_perc=.0012 > "$VALIDATION_BACKUP/btc-config-result.json"
curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  -X POST "$LOB_ADMIN_URL/admin/config/usdt" \
  -d "csrf_token=$CSRF" -d order_size=.5 -d inv_max=20 -d inv_min=1 \
  -d cancel_after_minutes=7 -d depth_max=6 -d time_step=4 \
  -d demo_inv_initial=4 -d cash_initial=2200000 -d commission_perc=.002 \
  > "$VALIDATION_BACKUP/usdt-config-result.json"
```

Poll for no more than two configured round boundaries and assert pair
isolation, hot application, pending session fields, and exact overlay shape:

```bash
python3 - <<'PY'
import json, os, time
from pathlib import Path
state = Path(os.environ["LOB_STATE_DIR"])
expected = {
 "btc": ({"order_size":.00001,"inv_max":.0005,"inv_min":.000001,
          "cancel_after_minutes":8,"depth_max":7,"time_step":5},
         {"demo_inv_initial":.0001,"cash_initial":1500000,
          "commission_perc":.0012}),
 "usdt": ({"order_size":.5,"inv_max":20,"inv_min":1,
           "cancel_after_minutes":7,"depth_max":6,"time_step":4},
          {"demo_inv_initial":4,"cash_initial":2200000,
           "commission_perc":.002}),
}
deadline = time.time() + 2 * max(values[0]["time_step"] for values in expected.values()) + 5
while True:
    ok = True
    for pair, (hot, pending) in expected.items():
        overlay = json.loads((state / f"demo_config_{pair}.json").read_text())
        snap = json.loads((state / f"runtime_config_{pair}.json").read_text())
        ok &= set(overlay) == set(hot) | set(pending)
        ok &= all(snap["applied"].get(k) == v for k, v in hot.items())
        ok &= all(snap["pending_new_session"].get(k) == v for k, v in pending.items())
    if ok: break
    if time.time() >= deadline: raise SystemExit("FAIL: boundary timeout")
    time.sleep(.25)
print("PASS: isolated overlays, six hot applied, three session fields pending")
PY
shasum -a 256 market_maker/results/demo_config_{btc,usdt}.json
```

Exercise the Dashboard callback through its HTTP boundary and compare every
returned row with the desired/runtime state:

```bash
python3 - <<'PY'
import json, os, requests
from pathlib import Path
base = os.environ["LOB_DASHBOARD_URL"]
state = Path(os.environ["LOB_STATE_DIR"])
output = "..effective-config-panel.style...effective-config-summary.children...effective-config-table.data.."
for pair in ("btc", "usdt"):
    response = requests.post(base + "/_dash-update-component", json={
      "output": output,
      "outputs": [{"id":"effective-config-panel","property":"style"},
                  {"id":"effective-config-summary","property":"children"},
                  {"id":"effective-config-table","property":"data"}],
      "inputs": [{"id":"track","property":"value","value":pair+"_demo"},
                 {"id":"tick","property":"n_intervals","value":1}],
      "changedPropIds": ["tick.n_intervals"], "state": []}, timeout=10)
    response.raise_for_status()
    rows = response.json()["response"]["effective-config-table"]["data"]
    desired = json.loads((state / f"demo_config_{pair}.json").read_text())
    snap = json.loads((state / f"runtime_config_{pair}.json").read_text())
    by_field = {row["Field"]: row for row in rows}
    assert set(by_field) == set(desired)
    assert all(by_field[k]["Desired"] == v for k, v in desired.items())
    assert all(by_field[k]["Applied"] == snap["applied"][k] for k in desired)
print("PASS: Dashboard callback rows match both worker snapshots")
PY
```

Record the current session, restart both workers through Admin, assert the
session/digests/pending values are unchanged, then create a new session and
assert both ledger seeds. The same snapshot polling command above is reused
after each POST.

```bash
export SESSION_BEFORE="$(python3 -c 'import json; print(json.load(open("market_maker/results/stack_session.json"))["session_id"])')"
curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  -X POST "$LOB_ADMIN_URL/admin/workers/restart" \
  -d "csrf_token=$CSRF" -d pair=btc > "$VALIDATION_BACKUP/btc-restart.json"
curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  -X POST "$LOB_ADMIN_URL/admin/workers/restart" \
  -d "csrf_token=$CSRF" -d pair=usdt > "$VALIDATION_BACKUP/usdt-restart.json"
test "$SESSION_BEFORE" = "$(python3 -c 'import json; print(json.load(open("market_maker/results/stack_session.json"))["session_id"])')" \
  && echo 'PASS: worker restarts retained session'
curl -fsS -H 'Accept: application/json' -b "$COOKIE_JAR" \
  -X POST "$LOB_ADMIN_URL/admin/session/new" \
  -d "csrf_token=$CSRF" > "$VALIDATION_BACKUP/new-session.json"
python3 - <<'PY'
import json, os, time
from pathlib import Path
state = Path(os.environ["LOB_STATE_DIR"])
before = os.environ["SESSION_BEFORE"]
current = json.loads((state / "stack_session.json").read_text())["session_id"]
assert current != before
deadline = time.time() + 20
while True:
    try:
        for pair, symbol, seed in (("btc","btcirt",(.0001,1500000,.0012)),
                                   ("usdt","usdtirt",(4,2200000,.002))):
            ledger = json.loads((state / f"paper_{current}_{symbol}.json").read_text())
            actual = (ledger["account"]["inventory"], ledger["account"]["cash"],
                      ledger["config"]["commission_perc"])
            snap = json.loads((state / f"runtime_config_{pair}.json").read_text())
            assert actual == seed and snap["session_id"] == current
            assert not snap["pending_new_session"]
        break
    except (AssertionError, FileNotFoundError, json.JSONDecodeError):
        if time.time() >= deadline: raise
        time.sleep(.25)
print("PASS: new session changed id and consumed both pair seeds")
PY
docker compose up -d --force-recreate --wait
docker compose ps
curl -fsS "$LOB_ADMIN_URL/admin/health"
curl -fsS "$LOB_DASHBOARD_URL/_dash-layout" > /dev/null
python3 - <<'PY'
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path("market_maker").resolve()))
from config import TRADING_PROFILES
from demo_config import desired_digest, load_desired_config
state = Path(os.environ["LOB_STATE_DIR"])
current = json.loads((state / "stack_session.json").read_text())["session_id"]
deadline = time.time() + 20
while True:
    try:
        for pair in ("btc", "usdt"):
            desired = load_desired_config(pair, state, TRADING_PROFILES)
            snapshot = json.loads((state / f"runtime_config_{pair}.json").read_text())
            assert snapshot["session_id"] == current
            assert snapshot["desired_digest"] == desired_digest(desired)
            assert not snapshot["pending_new_session"]
        break
    except (AssertionError, FileNotFoundError, json.JSONDecodeError):
        if time.time() >= deadline: raise
        time.sleep(.25)
print("PASS: recreation healthy and both snapshots match desired digests")
PY
```

For restoration, submit the nine `desired` values saved in each
`*-before.json`, request one new session, then restore the four exact raw
overlay/`.previous` targets from `VALIDATION_BACKUP`. If an `*.absent` marker
exists, remove only its corresponding exact target. The following commands
also restore every quarantined command to its recorded path and stop the host
agent. They never remove a runtime directory:

```bash
python3 - <<'PY'
import http.cookiejar, json, os, requests
from pathlib import Path
base = os.environ["LOB_ADMIN_URL"]
backup = Path(os.environ["VALIDATION_BACKUP"])
session = requests.Session()
jar = http.cookiejar.MozillaCookieJar(str(backup / "admin.cookies"))
jar.load(ignore_discard=True, ignore_expires=True)
session.cookies.update(jar)
headers = {"Accept": "application/json"}
config = session.get(base + "/admin/config/btc", headers=headers, timeout=10)
config.raise_for_status()
csrf = config.json()["csrf_token"]
for pair in ("btc", "usdt"):
    desired = json.loads((backup / f"{pair}-before.json").read_text())["desired"]
    response = session.post(base + f"/admin/config/{pair}",
                            headers=headers,
                            data={**desired, "csrf_token": csrf}, timeout=40)
    response.raise_for_status()
rotation = session.post(base + "/admin/session/new",
                        headers=headers, data={"csrf_token": csrf}, timeout=60)
rotation.raise_for_status()
print("PASS: prior desired values submitted and consumed by a restored session")
PY

kill "$AGENT_PID"
wait "$AGENT_PID" || true
AGENT_PID=''
python3 - <<'PY'
import json, os, shutil, uuid
from pathlib import Path
state = Path(os.environ["LOB_STATE_DIR"])
backup = Path(os.environ["VALIDATION_BACKUP"])
for name in ("demo_config_btc.json", "demo_config_btc.json.previous",
             "demo_config_usdt.json", "demo_config_usdt.json.previous"):
    target = state / name
    source = backup / name
    if (backup / f"{name}.absent").exists():
        target.unlink(missing_ok=True)
    else:
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
for original in json.loads((backup / "queued-command-paths.json").read_text()):
    original = Path(original)
    os.replace(backup / "queued-commands" / original.name, original)
print("PASS: exact overlay baseline and quarantined queues restored; agent stopped")
PY
trap - EXIT INT TERM
pgrep -af '[d]eploy/control_agent.py --mode local' && exit 1 || true
python3 - <<'PY'
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path("market_maker").resolve()))
from config import TRADING_PROFILES
from demo_config import desired_digest, load_desired_config
state = Path(os.environ["LOB_STATE_DIR"])
backup = Path(os.environ["VALIDATION_BACKUP"])
current = json.loads((state / "stack_session.json").read_text())["session_id"]
deadline = time.time() + 20
while True:
    try:
        for pair in ("btc", "usdt"):
            snapshot = json.loads((state / f"runtime_config_{pair}.json").read_text())
            desired = load_desired_config(pair, state, TRADING_PROFILES)
            assert snapshot["session_id"] == current
            assert snapshot["desired_digest"] == desired_digest(desired)
            assert not snapshot["pending_new_session"]
        break
    except (AssertionError, FileNotFoundError, json.JSONDecodeError):
        if time.time() >= deadline: raise
        time.sleep(.25)
for name in ("demo_config_btc.json", "demo_config_btc.json.previous",
             "demo_config_usdt.json", "demo_config_usdt.json.previous"):
    target = state / name
    if (backup / f"{name}.absent").exists():
        assert not target.exists()
    else:
        assert target.read_bytes() == (backup / name).read_bytes()
for original in json.loads((backup / "queued-command-paths.json").read_text()):
    assert Path(original).exists()
print("PASS: restored session/snapshots/digests/raw overlays/queues match baseline")
PY
```

If validation began with intentional overlays rather than this task's
no-overlay baseline, the raw backup is authoritative and those overlays must
remain present.

Explicit outcome ledger:

| Check | Result | Evidence |
|---|---|---|
| Authenticated login/config GET | PASS | HTTP `200/302/200` |
| BTC/USDT config POST and isolation | PASS | HTTP `200`; exact nine-key overlays and distinct SHA-256 values |
| Hot/pending boundary acknowledgement | PASS | Six applied and three pending for both pairs |
| Dashboard callback consistency | PASS | Both callback POSTs HTTP `200`; row hashes recorded above |
| Same-session restart persistence | PASS | Both restart POSTs HTTP `200`; baseline session unchanged |
| New-session seed consumption | PASS | Session changed to `demo-20260809T153316Z-fe0bd204a607`; both ledgers exact |
| Full Compose recreation | PASS | Four long-running services healthy; overlays/digests coherent |
| Restoration | PASS | This task's original no-overlay baseline restored; queues restored; agent stopped |

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

## Continuation handoff for another agent

This section is the durable resume point if later work is delegated to a new
agent. The implementation is on branch `parsaoryani/admin-demo-control`; the
configuration-synchronization commit range starts after `02f4c49`, Task 7's
initial validation commit is `97eb468`, and the standards-review follow-up is
the next scoped commit in `git log`. Read these documents before editing:

1. `docs/superpowers/specs/2026-08-09-admin-config-synchronization-design.md`
2. `docs/superpowers/plans/2026-08-09-admin-config-synchronization.md`
3. this validation report
4. `docs/deployment/ADMIN_PAGE.md`

The checkout intentionally has unrelated pre-existing modified, deleted, and
untracked files. Do not clean, reset, restore, or broadly stage the worktree.
Inspect `git status --short`, stage explicit task files or hunks only, and keep
runtime data, screenshots, `.env` files, and `.superpowers/sdd/` scratch files
out of commits. In particular, do not add `.playwright-mcp/`,
`admin-overview.png`, `dashboard.png`, or `market_maker/results/control/`.

Preserve these architectural boundaries:

- Demo/paper configuration only; Real Mode configuration is out of scope.
- Do not alter Algorithm 1 trading decisions for configuration plumbing.
- Admin and Dashboard remain unable to control Docker or host processes.
- `demo_config_<pair>.json` is the desired overlay; workers alone write
  `runtime_config_<pair>.json` acknowledgements.
- BTC and USDT validation, persistence, snapshots, and ledgers stay isolated.
- Hot fields change only at safe round boundaries; initial inventory, cash,
  and commission change only when a new session creates a new ledger.
- Demo ledger construction is read-only. Only the process holding
  `WorkerLease` calls the public `persist_initial_state()` before initial
  config reload/run.
- Indeterminate worker liveness is an explicit failure. Destructive control
  actions preflight every required pair before mutating shared state.

Before making a new completion claim, rerun the focused suite and both Compose
checks shown above. For the full suite, explicitly set
`LOB_TELEGRAM_DISABLED=0`; the ignored local `.env` currently disables
Telegram and otherwise causes the six known lifecycle-test failures. When
auditing **this report's restored baseline**, verify that no local
control-agent process is left running, both live snapshots match the current
`stack_session.json`, pending values are empty, and pair overlay files are
absent. Those empty/absent expectations describe this task's recorded
baseline only: if a later task intentionally changes desired configuration,
do not remove its overlays or rotate its session merely to recreate the old
baseline. Production Nginx/systemd and real exchange/MySQL validation are the
only known environment-level follow-ups; they require access to the
production host and must not be inferred from this local Docker result.
