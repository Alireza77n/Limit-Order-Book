"""SimpleMarketMaker.persist_to_db must gate every MySQL touchpoint in
__init__. Default (True) must behave exactly as before the flag existed;
False (demo mode) must never call mysql.connector.connect.
"""

from config import MMConfig
from portfolio import Nobitex_portfolio
from engine import SimpleMarketMaker
from exchange.demo import DemoExchangeClient
from exchange.base import ExchangeClient
from strategy.algo1 import Algo1Strategy


class _NoOpExchange(ExchangeClient):
    def get_orderbook(self, symbol):
        return None

    def wallet_balance(self, currency):
        return None

    def send_order(self, *a, **k):
        return None

    def get_order_list_status(self, *a, **k):
        return []

    def cancel_order(self, *a, **k):
        return {}

    def cancel_all_open_orders(self, *a, **k):
        return None

    def cancel_old_orders(self, *a, **k):
        return None


class _FakeCursor:
    def execute(self, *a, **k):
        pass


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass


def test_persist_to_db_true_by_default_connects_to_mysql(monkeypatch):
    calls = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        return _FakeConn()

    monkeypatch.setattr("engine.mysql.connector.connect", fake_connect)

    bot = SimpleMarketMaker(
        MMConfig(), Nobitex_portfolio("token"), _NoOpExchange(),
        strategy=Algo1Strategy(MMConfig()),
    )

    assert len(calls) == 1
    assert bot.persist_to_db is True
    assert bot.conn is not None
    assert bot.cursor is not None


def test_persist_to_db_false_never_touches_mysql(monkeypatch):
    calls = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        return _FakeConn()

    monkeypatch.setattr("engine.mysql.connector.connect", fake_connect)

    bot = SimpleMarketMaker(
        MMConfig(), Nobitex_portfolio("token"), _NoOpExchange(),
        strategy=Algo1Strategy(MMConfig()), persist_to_db=False,
    )

    assert calls == []
    assert bot.persist_to_db is False
    assert bot.conn is None
    assert bot.cursor is None
