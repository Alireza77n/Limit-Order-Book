"""DemoExchangeClient must use real market data but never place a real
order. These tests use a stub in place of NobitexExchangeClient so no
network call happens either way -- the stub's send_order raises if
called, proving DemoExchangeClient never reaches it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from exchange.base import ExchangeClient
from exchange.demo import DemoExchangeClient
from helpers import make_orderbook
from paper import PaperLedger


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def make_demo(pair="BTCIRT", **ledger_overrides):
    values = {
        "session_id": "shared",
        "symbol": pair,
        "initial_inventory": 10,
        "initial_cash": 10_000,
        "commission_perc": 0.01,
        "valuation_price": 100,
        "level1_fast_fill_enabled": False,
    }
    values.update(ledger_overrides)
    ledger = PaperLedger(**values)
    return DemoExchangeClient(
        market_data=_StubRealExchange(make_orderbook()), pair=pair, ledger=ledger
    )


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


def test_wallet_balance_does_not_reach_authenticated_real_market_data():
    stub = _StubRealExchange(make_orderbook())
    demo = DemoExchangeClient(market_data=stub, pair="BTCIRT")

    assert demo.wallet_balance("btc") is None
    assert stub.wallet_balance_calls == []


def test_send_order_never_touches_real_exchange_and_starts_open():
    stub = _StubRealExchange(make_orderbook())
    ledger = PaperLedger(
        session_id="shared", symbol="BTCIRT", initial_inventory=10,
        initial_cash=10_000, commission_perc=0.01, valuation_price=100,
        level1_fast_fill_enabled=False,
    )
    demo = DemoExchangeClient(market_data=stub, pair="BTCIRT", ledger=ledger)

    clid = demo.send_order(
        "buy", "btc", "rls", 0.001, 100.0,
        selected_level=3, now=T0,
    )

    assert clid is not None
    open_orders = demo.get_order_list_status(status="open")
    assert len(open_orders) == 1
    assert demo.get_order_list_status(status="close") == []
    order = open_orders[0]
    assert order["clientOrderId"] == clid
    assert order["type"] == "buy"
    assert order["status"] == "open"
    assert order["matchedAmount"] == 0
    assert order["unmatchedAmount"] == 0.001
    assert order["price"] == 100.0
    assert order["pair"] == "BTCIRT"


def test_send_order_respects_explicit_client_order_id():
    demo = make_demo("USDTIRT")

    clid = demo.send_order(
        "sell", "usdt", "rls", 1.0, 50000.0,
        clientOrderId="my-id-123", selected_level=3, now=T0,
    )

    assert clid == "my-id-123"
    assert demo.get_order_list_status(status="open")[0]["clientOrderId"] == "my-id-123"


def test_cancel_methods_only_cancel_local_simulated_orders():
    demo = make_demo("BTCIRT")
    clid = demo.send_order(
        "buy", "btc", "rls", 1.0, 100.0,
        clientOrderId="local", selected_level=3, now=T0,
    )

    result = demo.cancel_order(clid, now=T0 + timedelta(seconds=1))
    assert result["status"] == "ok"
    assert demo.get_order_list_status(status="open") == []
    assert demo.get_order_list_status(status="close")[0]["status"] == "cancelled"
