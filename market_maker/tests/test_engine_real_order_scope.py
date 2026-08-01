import pytest

import run
from engine import SimpleMarketMaker
from exchange.base import ExchangeClient


class _Strategy:
    pass


class _ScopedExchange(ExchangeClient):
    is_demo = False

    def __init__(self):
        self.status_kwargs = None
        self.bulk_kwargs = None
        self.stale_kwargs = None

    def get_orderbook(self, symbol):
        return None

    def wallet_balance(self, currency):
        return None

    def send_order(self, *args, **kwargs):
        return "owned-new"

    def get_order_list_status(self, **kwargs):
        self.status_kwargs = kwargs
        return [
            {"clientOrderId": "owned-buy", "pair": "USDTIRT"},
            {"clientOrderId": "foreign", "pair": "USDTIRT"},
        ]

    def cancel_order(self, order_id):
        raise AssertionError("not used directly")

    def cancel_all_open_orders(self, **kwargs):
        self.bulk_kwargs = kwargs
        return {"owned-buy"}

    def cancel_old_orders(self, **kwargs):
        self.stale_kwargs = kwargs
        return {"owned-sell"}


def _bot():
    cfg = run.make_cfg("usdt", mode="real", session_id="real-session")
    exchange = _ScopedExchange()
    bot = SimpleMarketMaker(cfg, None, exchange, strategy=_Strategy(), persist_to_db=False)
    bot.recent_order_ids = ["owned-buy", "owned-sell"]
    bot.tracked_orders = {"buy": {"owned-buy"}, "sell": {"owned-sell"}}
    bot.order_history_ids = {"owned-buy", "owned-sell"}
    return bot, exchange


def test_real_status_lookup_paginates_and_filters_registry_and_pair():
    bot, exchange = _bot()

    orders = bot.get_status_orders("open")

    assert [order["clientOrderId"] for order in orders] == ["owned-buy"]
    assert exchange.status_kwargs["max_pages"] == 100


def test_real_bulk_cleanup_passes_pair_and_registry_then_prunes_active_only():
    bot, exchange = _bot()

    cancelled = bot._cancel_owned_open_orders(max_pages=10)

    assert cancelled == {"owned-buy"}
    assert exchange.bulk_kwargs["pair"] == "USDTIRT"
    assert exchange.bulk_kwargs["owned_client_order_ids"] == {"owned-buy", "owned-sell"}
    assert bot.recent_order_ids == ["owned-sell"]
    assert bot.tracked_orders == {"buy": set(), "sell": {"owned-sell"}}
    assert bot.order_history_ids == {"owned-buy", "owned-sell"}


def test_real_stale_cleanup_passes_pair_and_registry_then_prunes_active_only():
    bot, exchange = _bot()

    cancelled = bot._cancel_owned_stale_orders(max_age_minutes=30)

    assert cancelled == {"owned-sell"}
    assert exchange.stale_kwargs["pair"] == "USDTIRT"
    assert exchange.stale_kwargs["owned_client_order_ids"] == {"owned-buy", "owned-sell"}
    assert bot.recent_order_ids == ["owned-buy"]
    assert bot.order_history_ids == {"owned-buy", "owned-sell"}


def test_real_cumulative_partial_fill_applies_four_then_six_and_no_duplicate(monkeypatch):
    bot, _ = _bot()
    bot.recent_order_ids = ["owned-buy"]
    bot.tracked_orders = {"buy": {"owned-buy"}, "sell": set()}
    updates = iter([
        [{
            "clientOrderId": "owned-buy", "type": "buy", "matchedAmount": 4,
            "unmatchedAmount": 6, "price": 100, "status": "open", "_raw": {"fee": 0.004},
        }],
        [],
        [],
        [{
            "clientOrderId": "owned-buy", "type": "buy", "matchedAmount": 10,
            "unmatchedAmount": 0, "price": 100, "status": "done", "_raw": {"fee": 0.010},
        }],
        [],
        [{
            "clientOrderId": "owned-buy", "type": "buy", "matchedAmount": 10,
            "unmatchedAmount": 0, "price": 100, "status": "done", "_raw": {"fee": 0.010},
        }],
    ])
    monkeypatch.setattr(bot, "get_status_orders", lambda status: next(updates))

    inventory, cash = bot.update_inventory_and_cash("all", 0, 2_000)
    assert inventory == pytest.approx(3.996)
    assert cash == pytest.approx(1_600)
    assert bot.last_round_fee_irt == pytest.approx(0.4)
    assert bot.recent_order_ids == ["owned-buy"]

    inventory, cash = bot.update_inventory_and_cash("all", inventory, cash)
    assert inventory == pytest.approx(9.99)
    assert cash == pytest.approx(1_000)
    assert bot.last_round_fee_irt == pytest.approx(0.6)
    assert bot.recent_order_ids == []
    assert bot.tracked_orders["buy"] == set()

    inventory, cash = bot.update_inventory_and_cash("all", inventory, cash)
    assert inventory == pytest.approx(9.99)
    assert cash == pytest.approx(1_000)
    assert bot.last_round_fee_irt == 0
