import pytest

from config import TRADING_PROFILES
from demo_config import OverlayValidationError
from forward_config import (
    FORWARD_MIN_COMMISSION_PERC,
    desired_config,
    validate_forward_config,
)


def test_forward_defaults_use_verified_irt_fee_for_btc_and_usdt(tmp_path):
    for pair in ("btc", "usdt"):
        values = desired_config(pair, TRADING_PROFILES, tmp_path)
        assert values["commission_perc"] == FORWARD_MIN_COMMISSION_PERC
        assert values["exp_ret_min"] == FORWARD_MIN_COMMISSION_PERC


def test_forward_config_rejects_a_fee_below_verified_irt_rate(tmp_path):
    values = desired_config("btc", TRADING_PROFILES, tmp_path)
    values["commission_perc"] = 0.001

    with pytest.raises(OverlayValidationError, match="at least 0.0025"):
        validate_forward_config(values, "btc", TRADING_PROFILES, tmp_path)


SIZING_FIELDS = (
    "inventory_sizing_enabled",
    "inventory_sizing_strength",
    "inventory_sizing_min_fraction",
)


def test_forward_config_exposes_inventory_sizing_with_profile_defaults(tmp_path):
    for pair in ("btc", "usdt"):
        values = desired_config(pair, TRADING_PROFILES, tmp_path)
        for field in SIZING_FIELDS:
            assert values[field] == TRADING_PROFILES[pair][field]


def test_forward_config_saves_inventory_sizing_and_the_worker_config_uses_it(
    tmp_path, monkeypatch
):
    import run
    from forward_config import save_config

    saved = save_config(
        {
            "inventory_sizing_enabled": False,
            "inventory_sizing_strength": 0.5,
            "inventory_sizing_min_fraction": 0.2,
        },
        "usdt", TRADING_PROFILES, tmp_path,
    )
    assert saved["inventory_sizing_enabled"] is False

    monkeypatch.setenv("LOB_FORWARD_CONFIG_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="real", session_id="forward-sizing")

    assert cfg.inventory_sizing_enabled is False
    assert cfg.inventory_sizing_strength == 0.5
    assert cfg.inventory_sizing_min_fraction == 0.2
    # The other pair keeps its own profile defaults.
    btc = run.make_cfg("btc", mode="real", session_id="forward-sizing")
    assert btc.inventory_sizing_enabled is True


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("inventory_sizing_strength", 1.5),
        ("inventory_sizing_strength", -0.1),
        ("inventory_sizing_min_fraction", 1.2),
        ("inventory_sizing_enabled", 1),
    ],
)
def test_forward_config_rejects_out_of_range_inventory_sizing(tmp_path, field, bad):
    values = desired_config("btc", TRADING_PROFILES, tmp_path)
    values[field] = bad

    with pytest.raises(OverlayValidationError):
        validate_forward_config(values, "btc", TRADING_PROFILES, tmp_path)
