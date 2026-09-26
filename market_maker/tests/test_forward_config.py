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
