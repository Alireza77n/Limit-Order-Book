import pytest

import run
from config import MMConfig
from engine import SimpleMarketMaker
from exchange.base import ExchangeClient
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


def _linear_bot(**overrides):
    return _bot(
        inventory_sizing_strength=1.0,
        inventory_sizing_min_fraction=0.0,
        **overrides,
    )


@pytest.mark.parametrize(
    "inventory, expected_buy, expected_sell",
    [
        (0.0, 20.0, 0.0),
        (25.0, 15.0, 5.0),
        (50.0, 10.0, 10.0),
        (75.0, 5.0, 15.0),
        (100.0, 0.0, 20.0),
        (140.0, 0.0, 20.0),
    ],
)
def test_full_strength_linear_sizing(inventory, expected_buy, expected_sell):
    buy_size, sell_size = _linear_bot()._inventory_sized_order_targets(inventory)

    assert buy_size == pytest.approx(expected_buy)
    assert sell_size == pytest.approx(expected_sell)
    assert buy_size + sell_size == pytest.approx(20.0)


def test_open_buys_count_as_inventory_for_the_buy_side_only():
    buy_size, sell_size = _linear_bot()._inventory_sized_order_targets(
        50.0, open_buy_amount=25.0
    )

    assert buy_size == pytest.approx(5.0)
    assert sell_size == pytest.approx(10.0)


def test_open_sells_reduce_inventory_for_the_sell_side_only():
    buy_size, sell_size = _linear_bot()._inventory_sized_order_targets(
        50.0, open_sell_amount=25.0
    )

    assert buy_size == pytest.approx(10.0)
    assert sell_size == pytest.approx(5.0)


def test_trading_profiles_enable_full_strength_linear_sizing():
    from config import TRADING_PROFILES

    for profile in TRADING_PROFILES.values():
        assert profile["inventory_sizing_enabled"] is True
        assert profile["inventory_sizing_strength"] == 1.0
        assert profile["inventory_sizing_min_fraction"] == 0.0


class _RestingOrdersExchange(ExchangeClient):
    """Live-mode stand-in: accepts every order and leaves it open forever."""

    is_demo = False

    def __init__(self, base_balance, quote_balance):
        self.balances = {"usdt": base_balance, "rls": quote_balance}
        self.open_orders = []

    def get_orderbook(self, symbol):
        return {
            "bids": [["99", "10"], ["98", "10"]],
            "asks": [["100", "10"], ["101", "10"]],
            "lastTradePrice": "100",
        }

    def wallet_balance(self, currency):
        return self.balances[currency]

    def send_order(self, order_type, srcCurrency, dstCurrency, amount, price,
                   clientOrderId=None):
        self.open_orders.append({
            "clientOrderId": clientOrderId,
            "pair": "USDTIRT",
            "type": order_type,
            "status": "Active",   # what Nobitex reports for a resting order
            "price": float(price),
            "amount": float(amount),
            "matchedAmount": 0.0,
            "unmatchedAmount": float(amount),
        })
        return clientOrderId

    def get_order_list_status(self, status="open", **kwargs):
        return list(self.open_orders) if status == "open" else []

    def cancel_order(self, order_id):
        raise AssertionError("not used directly")

    def cancel_all_open_orders(self, **kwargs):
        return set()

    def cancel_old_orders(self, **kwargs):
        return set()


class _FixedQuote:
    def decide_order(self, ob, inv_current):
        return {"bid": 90.0, "ask": 110.0, "bid level": 1, "ask level": 1}


def test_live_loop_sizes_orders_from_wallet_inventory_and_resting_orders(
    monkeypatch, tmp_path
):
    """The real-mode loop with the profile's own sizing settings. Inventory 50
    of 100 starts both sides at order_size. Resting buys then count as
    inventory for the buy side and resting sells are subtracted for the sell
    side, so both shrink: 10, then 2*10*(1 - 60/100) = 8, then 6.4."""
    monkeypatch.setattr("engine.RESULTS_DIR", str(tmp_path))
    monkeypatch.delenv("LOB_FORWARD_CONFIG_DIR", raising=False)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "order_size", 10.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inv_max", 100.0)
    cfg = run.make_cfg("usdt", mode="real", session_id="live-sizing-session")
    cfg.output_csv = str(tmp_path / "usdt_track.csv")
    cfg.max_rounds = 3
    cfg.time_step = 0
    exchange = _RestingOrdersExchange(base_balance=50.0, quote_balance=1e9)
    bot = SimpleMarketMaker(
        cfg, None, exchange, strategy=_FixedQuote(), persist_to_db=False
    )

    frame = bot.run()

    assert frame["Buy Size"].tolist() == pytest.approx([10.0, 8.0, 6.4])
    assert frame["Sell Size"].tolist() == pytest.approx([10.0, 8.0, 6.4])
    assert [order["amount"] for order in exchange.open_orders
            if order["type"] == "buy"] == pytest.approx([10.0, 8.0, 6.4])
