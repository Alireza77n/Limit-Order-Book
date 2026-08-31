import pytest

from config import MMConfig
from helpers import make_bot


def _bot(**overrides):
    values = {
        "pair": "USDTIRT",
        "order_size": 10.0,
        "inv_min": 0.0,
        "inv_max": 100.0,
        "inventory_sizing_enabled": True,
        "inventory_sizing_strength": 0.75,
        "inventory_sizing_min_fraction": 0.25,
    }
    values.update(overrides)
    return make_bot(MMConfig(**values))


def test_inventory_sizing_disabled_keeps_base_order_size():
    bot = _bot(inventory_sizing_enabled=False)

    assert bot._inventory_sized_order_targets(90.0) == (10.0, 10.0)


def test_high_inventory_shrinks_buy_and_grows_sell():
    bot = _bot()

    buy_size, sell_size = bot._inventory_sized_order_targets(100.0)

    assert buy_size == pytest.approx(2.5)
    assert sell_size == pytest.approx(17.5)


def test_low_inventory_grows_buy_and_shrinks_sell():
    bot = _bot()

    buy_size, sell_size = bot._inventory_sized_order_targets(0.0)

    assert buy_size == pytest.approx(17.5)
    assert sell_size == pytest.approx(2.5)


def test_min_fraction_clamps_inventory_sizing():
    bot = _bot(inventory_sizing_strength=1.0, inventory_sizing_min_fraction=0.4)

    buy_size, sell_size = bot._inventory_sized_order_targets(100.0)

    assert buy_size == pytest.approx(4.0)
    assert sell_size == pytest.approx(20.0)


def test_invalid_inventory_band_falls_back_to_base_order_size():
    bot = _bot(inv_min=5.0, inv_max=5.0)

    assert bot._inventory_sized_order_targets(5.0) == (10.0, 10.0)
