"""Runtime-path isolation for Compose-mounted results and demo state."""

import json
import os
from pathlib import Path
import subprocess
import sys


PACKAGE_DIR = Path(__file__).resolve().parent.parent


def _runtime_values(source: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "LOB_RESULTS_DIR": "/app/runtime/results",
            "LOB_STATE_DIR": "/app/runtime/state",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=PACKAGE_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_runtime_roots_can_be_isolated_by_environment():
    values = _runtime_values(
        "import config, json; print(json.dumps({"
        "'results': config.RESULTS_DIR, "
        "'state': config.DEMO_STATE_DIR, "
        "'paper': config.demo_state_path('paper.json')}))"
    )

    assert Path(values["results"]) == Path("/app/runtime/results")
    assert Path(values["state"]) == Path("/app/runtime/state")
    assert Path(values["paper"]) == Path("/app/runtime/state/paper.json")


def test_make_cfg_writes_outputs_and_state_to_separate_roots():
    values = _runtime_values(
        "import json, run; cfg = run.make_cfg('btc', 'demo', session_id='shared'); "
        "print(json.dumps({'output': cfg.output_csv, 'state': cfg.demo_state_path, "
        "'fee': cfg.demo_fee_state_path, 'archive': cfg.demo_archive_path}))"
    )

    assert Path(values["output"]).parent == Path("/app/runtime/results")
    assert Path(values["state"]).parent == Path("/app/runtime/state")
    assert Path(values["fee"]).parent == Path("/app/runtime/state")
    assert Path(values["archive"]).parent == Path("/app/runtime/state")


def test_demo_worker_lease_uses_the_state_root():
    values = _runtime_values(
        "import argparse, json, run; paths = []; "
        "argparse.ArgumentParser.parse_args = lambda self: argparse.Namespace(profile='btc', mode='demo', "
        "algorithm='algo1', exchange='nobitex', continuous=False); "
        "run.install_shutdown_signal_handlers = lambda: None; "
        "sync = type('Sync', (), {'reload_at_round_boundary': lambda self: "
        "type('Result', (), {'ok': True})()})(); "
        "run.make_bot = lambda *args: type('Bot', (), "
        "{'config_sync': sync, 'run': lambda self: None})(); "
        "run.WorkerLease = lambda path, identity: type('Lease', (), "
        "{'__enter__': lambda self: paths.append(path) or self, "
        "'__exit__': lambda self, *args: None})(); "
        "run.main(); print(json.dumps({'lease': paths[0]}))"
    )

    assert Path(values["lease"]).parent == Path("/app/runtime/state")
