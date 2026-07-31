"""DemoExchangeClient must use real market data but never place a real
order. These tests use a stub in place of NobitexExchangeClient so no
network call happens either way -- the stub's send_order raises if
called, proving DemoExchangeClient never reaches it.
"""

import pytest

from exchange.base import ExchangeClient
from exchange.demo import DemoExchangeClient
from helpers import make_orderbook


class _StubRealExchange(ExchangeClient):
    """Records calls; send_order raises if DemoExchangeClient ever calls it."""

    def __init__(self, orderbook):
        self._orderbook = orderbook
        self.get_orderbook_calls = []
        self.wallet_balance_calls = []

    def get_orderbook(self, symbol):
        self.get_orderbook_calls.append(symbol)
        return self._orderbook

    def wallet_balance(self, currency):
        self.wallet_balance_calls.append(currency)
        return 123.45

    def send_order(self, *args, **kwargs):
        raise AssertionError("DemoExchangeClient must never call the real exchange's send_order")

    def get_order_list_status(self, *args, **kwargs):
        raise AssertionError("DemoExchangeClient must never query the real exchange for order status")

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("DemoExchangeClient must never cancel a real order")

    def cancel_all_open_orders(self, *args, **kwargs):
        raise AssertionError("DemoExchangeClient must never cancel real orders")

    def cancel_old_orders(self, *args, **kwargs):
        raise AssertionError("DemoExchangeClient must never cancel real orders")


def test_get_orderbook_delegates_to_real_market_data():
    ob = make_orderbook()
    stub = _StubRealExchange(ob)
    demo = DemoExchangeClient(market_data=stub, pair="BTCIRT")

    result = demo.get_orderbook("BTCIRT")

    assert result is ob
    assert stub.get_orderbook_calls == ["BTCIRT"]


def test_wallet_balance_delegates_to_real_market_data():
    stub = _StubRealExchange(make_orderbook())
    demo = DemoExchangeClient(market_data=stub, pair="BTCIRT")

    assert demo.wallet_balance("btc") == 123.45
    assert stub.wallet_balance_calls == ["btc"]


def test_send_order_never_touches_real_exchange_and_fills_instantly():
    stub = _StubRealExchange(make_orderbook())
    demo = DemoExchangeClient(market_data=stub, pair="BTCIRT")

    clid = demo.send_order("buy", "btc", "rls", 0.001, 100.0)

    assert clid is not None
    assert demo.get_order_list_status(status="open") == []
    closed = demo.get_order_list_status(status="close")
    assert len(closed) == 1
    order = closed[0]
    assert order["clientOrderId"] == clid
    assert order["type"] == "buy"
    assert order["status"] == "done"
    assert order["matchedAmount"] == 0.001
    assert order["unmatchedAmount"] == 0.0
    assert order["price"] == 100.0
    assert order["pair"] == "BTCIRT"


def test_send_order_respects_explicit_client_order_id():
    demo = DemoExchangeClient(market_data=_StubRealExchange(make_orderbook()), pair="USDTIRT")

    clid = demo.send_order("sell", "usdt", "rls", 1.0, 50000.0, clientOrderId="my-id-123")

    assert clid == "my-id-123"
    assert demo.get_order_list_status(status="close")[0]["clientOrderId"] == "my-id-123"


def test_cancel_methods_are_safe_no_ops():
    demo = DemoExchangeClient(market_data=_StubRealExchange(make_orderbook()), pair="BTCIRT")

    assert demo.cancel_all_open_orders(max_pages=10) is None
    tracked = {"buy": set(), "sell": set()}
    assert demo.cancel_old_orders(max_age_minutes=10, df=None, tracked_orders=tracked) is tracked
    result = demo.cancel_order(12345)
    assert result["status"] == "ok"
