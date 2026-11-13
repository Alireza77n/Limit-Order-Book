"""What branch MM_algo1v2_mazdax ships: MM_algo1v2 only. The rest of the suite runs with
LOB_ALL_ALGORITHMS=1 (tests/conftest.py); this test imports config in a fresh interpreter without it."""
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
CODE = """
import json, config
print(json.dumps({name: [p["algorithm"], p["algo1_enabled"], p["algo3_enabled"], p["algo4_enabled"]]
                  for name, p in config.TRADING_PROFILES.items()}))
d = config.MMConfig(session="s")
print(json.dumps([config.BRANCH_ALGORITHM, d.algo1_enabled, d.algo3_enabled, d.algo4_enabled]))
"""


def test_the_branch_ships_one_algorithm_on_every_market():
    env = {k: v for k, v in os.environ.items() if k != "LOB_ALL_ALGORITHMS"}
    env["LOB_DISABLE_DOTENV"] = "1"
    out = subprocess.run([sys.executable, "-c", CODE], cwd=PACKAGE, env=env, capture_output=True,
                         text=True, check=True).stdout.strip().splitlines()
    profiles, defaults = json.loads(out[0]), json.loads(out[1])
    assert set(profiles) == {"btc", "usdt", "eth", "xaut", "trx", "ahrm1", "roba1", "nogh1", "zfam1"}
    for name, values in profiles.items():
        assert values == ["algo1", True, False, False], name
    assert defaults == ["algo1", True, False, False]
