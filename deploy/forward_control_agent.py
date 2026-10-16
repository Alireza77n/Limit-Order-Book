"""Narrow control agent for authenticated real forward-test workers.

The web app writes validated command files. This process accepts only fixed
BTC/USDT actions and fixed Python argv; request data is never shell syntax.
Run it as a dedicated systemd unit with the trade credentials in its private
EnvironmentFile, separate from the web app.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "market_maker"
sys.path.insert(0, str(PACKAGE))
PAIRS = {"btc": "BTCIRT", "usdt": "USDTIRT"}
POLL_SECONDS = 0.25
STOP_TIMEOUT = 35.0


@dataclass
class AgentPaths:
    control: Path
    config: Path
    locks: Path
    results: Path
    logs: Path


def paths_from_env():
    return AgentPaths(
        Path(os.environ.get("LOB_FORWARD_CONTROL_DIR", "/var/lib/market-maker-forward/control")),
        Path(os.environ.get("LOB_FORWARD_CONFIG_DIR", "/var/lib/market-maker-forward/config")),
        Path(os.environ.get("LOB_FORWARD_LOCK_DIR", "/var/lib/market-maker-forward/locks")),
        Path(os.environ.get("LOB_RESULTS_DIR", "/var/lib/market-maker-forward/results")),
        Path(os.environ.get("LOB_FORWARD_LOG_DIR", "/var/log/market-maker-forward")),
    )


def _lock_path(paths, pair):
    return paths.locks / f".worker_live_{PAIRS[pair].lower()}.lock"


def _worker_pid(paths, pair):
    path = _lock_path(paths, pair)
    if not path.exists():
        return None
    try:
        with path.open("r") as stream:
            line = stream.readline().strip()
        marker = " pid="
        if marker not in line or not line.startswith(f"{PAIRS[pair]}-live"):
            return None
        pid = int(line.rsplit(marker, 1)[1])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        if "run.py" not in cmdline or "--mode real" not in cmdline or f"--profile {pair}" not in cmdline:
            return None
        return pid
    except (OSError, ValueError):
        return None


def worker_running(paths, pair):
    return _worker_pid(paths, pair) is not None


def _stop_worker(paths, pair):
    pid = _worker_pid(paths, pair)
    if pid is None:
        return {"running": False, "stopped": True}
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not worker_running(paths, pair):
            return {"running": False, "stopped": True, "cleanup": "worker exited after SIGTERM"}
        time.sleep(0.25)
    return {"running": worker_running(paths, pair), "stopped": False, "error": "worker did not exit before timeout; no forced kill was sent"}


def _start_worker(paths, pair, algorithm="algo1"):
    """Start the pair's live worker with a fixed argv; ``algorithm`` is the
    validated live configuration's choice (strategy/catalog.py)."""
    if os.environ.get("FORWARD_TRADING_ENABLED") != "1":
        raise RuntimeError("FORWARD_TRADING_ENABLED is not set to 1")
    if not os.environ.get("NOBITEX_API_KEY") or not os.environ.get("NOBITEX_SECRET_KEY"):
        raise RuntimeError("Nobitex API credentials are missing from the forward-agent environment")
    if worker_running(paths, pair):
        return {"running": True, "already_running": True}
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.locks.mkdir(parents=True, exist_ok=True)
    paths.results.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "LOB_FORWARD_CONFIG_DIR": str(paths.config),
        "LOB_FORWARD_LOCK_DIR": str(paths.locks),
        "LOB_RESULTS_DIR": str(paths.results),
    })
    log_path = paths.logs / f"{pair}-live.log"
    log = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [sys.executable, "run.py", "--profile", pair, "--algorithm", algorithm, "--exchange", "nobitex", "--mode", "real", "--continuous"],
            cwd=PACKAGE, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if worker_running(paths, pair):
            return {"running": True, "pid": process.pid, "log": str(log_path)}
        if process.poll() is not None:
            raise RuntimeError(f"worker exited during startup with code {process.returncode}; inspect its private log")
        time.sleep(0.2)
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass
    raise RuntimeError("worker did not acquire its live lock before startup timeout")


_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _delete_live_session(paths, session_id):
    """Remove one live session's rows from every pair's real-money round
    history (<pair>_track.csv), after copying the files to a backup folder.
    Refused while a live worker runs: the running worker appends to these
    files and its own session must stay intact."""
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        raise ValueError("invalid or missing session_id")
    running = [pair for pair in PAIRS if worker_running(paths, pair)]
    if running:
        raise RuntimeError(
            "stop the live workers before deleting a live session (running: "
            + ", ".join(running) + ")"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = paths.results / "backups" / f"delete-live-session-{stamp}"
    removed = {}
    for pair in PAIRS:
        source = paths.results / f"{pair}_track.csv"
        if not source.exists():
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_dir / source.name)
        temporary = source.with_name(f".{source.name}.{os.getpid()}.tmp")
        dropped = 0
        with source.open("r", newline="", encoding="utf-8") as src:
            with temporary.open("w", newline="", encoding="utf-8") as dst:
                reader = csv.reader(src)
                writer = csv.writer(dst)
                header = next(reader, None)
                if header is None:
                    continue
                writer.writerow(header)
                column = header.index("Session_id") if "Session_id" in header else None
                for row in reader:
                    if column is not None and column < len(row) and row[column] == session_id:
                        dropped += 1
                        continue
                    writer.writerow(row)
        os.chmod(temporary, source.stat().st_mode & 0o777)
        os.replace(temporary, source)
        removed[pair] = dropped
    if not any(removed.values()):
        raise RuntimeError("session not found")
    return {"session_id": session_id, "removed": removed, "backup": str(backup_dir)}


def _load_command(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("command must be a JSON object")
    command_id = document.get("command_id")
    action, pair = document.get("action"), document.get("pair")
    if not isinstance(command_id, str) or not command_id.isalnum():
        raise ValueError("invalid command id")
    stack_actions = {"delete_forward_session"}          # address both pairs' history
    if action in stack_actions:
        if pair is not None:
            raise ValueError("invalid pair")
    elif pair not in PAIRS:
        raise ValueError("invalid pair")
    if action not in {"start_forward_worker", "stop_forward_worker", "apply_forward_config", "read_wallet_balances"} | stack_actions:
        raise ValueError("action is not supported by the forward-test agent")
    return document


def _result(paths, command_id, ok, detail):
    result_dir = paths.control / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    value = {
        "command_id": command_id, "ok": ok, "detail": detail,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    target = result_dir / f"{command_id}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temp, target)


def _read_wallet_balances():
    """Read the three spot-wallet balances without exposing API credentials to the web app."""
    api_key = os.environ.get("NOBITEX_API_KEY")
    secret_key = os.environ.get("NOBITEX_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Nobitex API credentials are missing from the forward-agent environment")

    from config import TRADING_PROFILES
    from exchange.nobitex import NobitexExchangeClient

    client = NobitexExchangeClient(
        api_key=api_key,
        secret_key=secret_key,
        url_send_order=None,
        url_wallet_balance=TRADING_PROFILES["btc"]["url_wallet_balance"],
        trading_enabled=False,
    )
    balances = {}
    for currency in ("rls", "btc", "usdt"):
        value = client.wallet_balance(currency)
        if value is None or not math.isfinite(value) or value < 0:
            raise RuntimeError(f"Could not read a valid {currency.upper()} balance from Nobitex")
        balances[currency] = value
    return {
        "balances": balances,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }


def process_command(paths, document):
    action, pair = document["action"], document["pair"]
    payload = document.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("invalid command payload")
    if action == "read_wallet_balances":
        return _read_wallet_balances()
    if action == "start_forward_worker":
        if payload.get("confirmed") is not True:
            raise ValueError("explicit live-trading confirmation is required")
        from forward_config import desired_config, validate_forward_config
        from config import TRADING_PROFILES
        from strategy.catalog import SELECTABLE_ALGORITHMS
        config = validate_forward_config(desired_config(pair, TRADING_PROFILES, paths.config), pair, TRADING_PROFILES, paths.config)
        algorithm = str(config.get("algorithm") or "algo1")
        if algorithm not in SELECTABLE_ALGORITHMS:
            raise ValueError(f"algorithm is not selectable: {algorithm}")
        return _start_worker(paths, pair, algorithm) | {
            "symbol": PAIRS[pair], "algorithm": algorithm,
            "risk_limits_enabled": True, "order_size": config["order_size"],
        }
    if action == "stop_forward_worker":
        result = _stop_worker(paths, pair)
        if result.get("stopped") is not True:
            raise RuntimeError(result.get("error") or "worker is still running")
        return result
    if action == "delete_forward_session":
        if payload.get("confirm") != "DELETE":
            raise ValueError("confirmation required")
        return _delete_live_session(paths, payload.get("session_id"))
    if action == "apply_forward_config":
        from forward_config import save_config
        from config import TRADING_PROFILES
        values = payload.get("overrides")
        if not isinstance(values, dict):
            raise ValueError("validated overrides are required")
        current = save_config(values, pair, TRADING_PROFILES, paths.config)
        return {"saved": True, "restart_required": worker_running(paths, pair), "risk_limits_enabled": True, "fields": sorted(current)}
    raise ValueError("unsupported action")


def run_once(paths):
    command_dir = paths.control / "commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(command_dir.glob("*.json")):
        try:
            doc = _load_command(path)
            detail = process_command(paths, doc)
            _result(paths, doc["command_id"], True, detail)
        except Exception as exc:
            command_id = path.stem
            _result(paths, command_id, False, {"error": str(exc), "phase": "forward_control"})
        try:
            path.unlink()
        except OSError:
            pass


def main():
    paths = paths_from_env()
    paths.control.mkdir(parents=True, exist_ok=True)
    while True:
        run_once(paths)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
