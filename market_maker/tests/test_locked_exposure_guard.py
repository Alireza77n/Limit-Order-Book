"""Unit tests for the locked-cash / locked-inventory pre-trade risk guard.

SimpleMarketMaker._locked_cash_guard_reason / _locked_inventory_guard_reason
are pure functions of self.cfg plus their explicit arguments (they only touch
self.session/self.now_provider/os.getpid() when actually blocking, to write
the diagnostic log line) -- so they are exercised directly via helpers.make_bot
rather than through a full engine round, matching this test suite's existing
seam for quoting-logic tests (see helpers.py).
"""

from datetime import datetime, timezone

import pytest

from config import MMConfig
from helpers import make_bot


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _bot(**overrides):
    overrides.setdefault("pair", "BTCIRT")
    cfg = MMConfig(**overrides)
    bot = make_bot(cfg)
    bot.session = "test-session"
    bot.now_provider = lambda: NOW
    return bot


# ---------- BUY / locked cash ----------

def test_buy_guard_disabled_never_blocks():
    bot = _bot(buy_locked_cash_limit_enabled=False, buy_locked_cash_limit_ratio=0.0)
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=1_000_000, new_order_cash=1_000_000, cash_base=1_000_000,
    ) is None


def test_buy_guard_allows_order_below_threshold():
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.5)
    # (200_000 + 100_000) / 1_000_000 = 0.30 <= 0.50
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=200_000, new_order_cash=100_000, cash_base=1_000_000,
    ) is None


def test_buy_guard_allows_exactly_at_threshold():
    """<= limit is accepted; only > limit is rejected (spec: prefer this)."""
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.5)
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=400_000, new_order_cash=100_000, cash_base=1_000_000,
    ) is None


def test_buy_guard_rejects_just_over_threshold():
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.5)
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=400_000, new_order_cash=100_000.01, cash_base=1_000_000,
    ) == "locked_cash_limit"


def test_buy_guard_rejects_order_that_would_exceed_threshold():
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.2)
    # (100_000 + 150_000) / 1_000_000 = 0.25 > 0.20
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=100_000, new_order_cash=150_000, cash_base=1_000_000,
    ) == "locked_cash_limit"


def test_buy_guard_aggregates_multiple_existing_orders():
    """Caller sums remaining_amount * limit_price across every open BUY --
    this only checks the guard reacts to that pre-aggregated total."""
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.5)
    already_locked = 300_000 + 250_000  # two resting buys
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=already_locked, new_order_cash=1, cash_base=1_000_000,
    ) == "locked_cash_limit"


def test_buy_guard_only_counts_remaining_amount_of_a_partial_fill():
    """A buy for 1.0 BTC at 100 that filled 0.6 locks only the remaining 0.4
    (40) of cash, not the original 100 -- this is a caller-contract test."""
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.2)
    remaining_locked = 0.4 * 100  # 40, not the original 100
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=remaining_locked, new_order_cash=10, cash_base=1_000,
    ) is None  # (40 + 10) / 1000 = 0.05 <= 0.20


def test_buy_guard_zero_cash_base_with_positive_projection_blocks():
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.5)
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=0.0, new_order_cash=1.0, cash_base=0.0,
    ) == "locked_cash_limit"


def test_buy_guard_zero_cash_base_with_zero_projection_never_divides_by_zero():
    bot = _bot(buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.0)
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=0.0, new_order_cash=0.0, cash_base=0.0,
    ) is None


# ---------- SELL / locked inventory ----------

def test_sell_guard_disabled_never_blocks():
    bot = _bot(sell_locked_inventory_limit_enabled=False, sell_locked_inventory_limit_ratio=0.0)
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=10, new_order_amount=10, inventory_base=10,
    ) is None


def test_sell_guard_allows_order_below_threshold():
    bot = _bot(sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.6)
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=2, new_order_amount=1, inventory_base=10,
    ) is None  # 0.3 <= 0.6


def test_sell_guard_allows_exactly_at_threshold():
    bot = _bot(sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.5)
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=4, new_order_amount=1, inventory_base=10,
    ) is None  # exactly 0.5


def test_sell_guard_rejects_order_that_would_exceed_threshold():
    bot = _bot(sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.5)
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=4, new_order_amount=1.01, inventory_base=10,
    ) == "locked_inventory_limit"


def test_sell_guard_aggregates_multiple_existing_orders():
    bot = _bot(sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.5)
    already_locked = 3 + 2  # two resting sells
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=already_locked, new_order_amount=0.1, inventory_base=10,
    ) == "locked_inventory_limit"


def test_sell_guard_zero_inventory_base_with_positive_projection_blocks():
    bot = _bot(sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.5)
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=0.0, new_order_amount=0.001, inventory_base=0.0,
    ) == "locked_inventory_limit"


def test_sell_guard_zero_inventory_base_with_zero_projection_never_divides_by_zero():
    bot = _bot(sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.0)
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=0.0, new_order_amount=0.0, inventory_base=0.0,
    ) is None


# ---------- independence ----------

def test_buy_and_sell_guards_are_independent_switches():
    bot = _bot(
        buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.1,
        sell_locked_inventory_limit_enabled=False, sell_locked_inventory_limit_ratio=0.1,
    )
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=0, new_order_cash=200, cash_base=1_000,
    ) == "locked_cash_limit"
    # SELL disabled -- must not block even though the ratio alone would.
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=9, new_order_amount=1, inventory_base=10,
    ) is None


def test_buy_disabled_sell_enabled_is_also_independent():
    bot = _bot(
        buy_locked_cash_limit_enabled=False, buy_locked_cash_limit_ratio=0.1,
        sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.1,
    )
    assert bot._locked_cash_guard_reason(
        locked_buy_cash=0, new_order_cash=200, cash_base=1_000,
    ) is None
    assert bot._locked_inventory_guard_reason(
        locked_sell_inventory=9, new_order_amount=1, inventory_base=10,
    ) == "locked_inventory_limit"


def test_pairs_do_not_leak_configuration():
    """Each MMConfig instance is this pair's own copy -- BTC and USDT are
    configured through separate cfg objects in the real system (make_cfg per
    profile), so setting one never touches the other."""
    btc = _bot(pair="BTCIRT", buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.4)
    usdt = _bot(pair="USDTIRT", buy_locked_cash_limit_enabled=False, buy_locked_cash_limit_ratio=0.9)

    assert btc._locked_cash_guard_reason(
        locked_buy_cash=0, new_order_cash=500, cash_base=1_000,
    ) == "locked_cash_limit"
    assert usdt._locked_cash_guard_reason(
        locked_buy_cash=0, new_order_cash=999, cash_base=1_000,
    ) is None


# ---------- config validation (schema layer) ----------

def test_ratio_out_of_range_is_rejected():
    from demo_config import OverlayValidationError, validate_overrides
    from config import TRADING_PROFILES

    for bad in (-0.01, 1.01, 2.0):
        with pytest.raises(OverlayValidationError):
            validate_overrides(
                {"buy_locked_cash_limit_ratio": bad}, pair="btc", profiles=TRADING_PROFILES,
            )


def test_enabled_flag_rejects_non_boolean():
    from demo_config import OverlayValidationError, validate_overrides
    from config import TRADING_PROFILES

    for bad in (1, 0, "true", 1.0):
        with pytest.raises(OverlayValidationError):
            validate_overrides(
                {"buy_locked_cash_limit_enabled": bad}, pair="btc", profiles=TRADING_PROFILES,
            )


def test_ratio_field_rejects_boolean():
    from demo_config import OverlayValidationError, validate_overrides
    from config import TRADING_PROFILES

    with pytest.raises(OverlayValidationError):
        validate_overrides(
            {"buy_locked_cash_limit_ratio": True}, pair="btc", profiles=TRADING_PROFILES,
        )


def test_valid_ratio_boundaries_are_accepted():
    from demo_config import validate_overrides
    from config import TRADING_PROFILES

    for good in (0.0, 0.5, 1.0):
        validated = validate_overrides(
            {"sell_locked_inventory_limit_ratio": good}, pair="usdt", profiles=TRADING_PROFILES,
        )
        assert validated["sell_locked_inventory_limit_ratio"] == good
