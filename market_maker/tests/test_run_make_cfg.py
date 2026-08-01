"""Fast, offline tests for run.make_cfg()'s real-vs-demo behavior.

Pins the exact bug that shipped once: demo mode's starting inventory must
come from each profile's own `demo_inv_initial` config value (not a
hardcoded literal), and real mode must be completely unaffected.
"""

import pytest

import run


@pytest.mark.parametrize("profile_name", sorted(run.TRADING_PROFILES))
def test_real_mode_keeps_profile_inv_initial(profile_name):
    cfg = run.make_cfg(profile_name, mode="real")
    assert cfg.inv_initial == run.TRADING_PROFILES[profile_name]["inv_initial"]
    assert not cfg.algorithm_id.endswith("-demo")
    assert not cfg.output_csv.endswith("_demo.csv")


@pytest.mark.parametrize("profile_name", sorted(run.TRADING_PROFILES))
def test_demo_mode_uses_configured_demo_inv_initial(profile_name):
    cfg = run.make_cfg(profile_name, mode="demo")
    assert cfg.inv_initial == run.TRADING_PROFILES[profile_name]["demo_inv_initial"]
    assert cfg.algorithm_id.endswith("-demo")
    assert cfg.output_csv.endswith("_demo.csv")


def test_demo_inv_initial_is_independently_configurable_per_profile(monkeypatch):
    monkeypatch.setitem(run.TRADING_PROFILES["btc"], "demo_inv_initial", 1.5)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "demo_inv_initial", 42.0)

    assert run.make_cfg("btc", mode="demo").inv_initial == 1.5
    assert run.make_cfg("usdt", mode="demo").inv_initial == 42.0
    # Real mode is never affected by the demo_inv_initial value.
    assert run.make_cfg("btc", mode="real").inv_initial == run.TRADING_PROFILES["btc"]["inv_initial"]
