import pytest

import run
from config import MMConfig
from engine import SimpleMarketMaker
from exchange.base import ExchangeClient
from exchange.demo import DemoExchangeClient
from portfolio import Nobitex_portfolio
from strategy.algo1 import Algo1Strategy


class UnsafeRealExchange(ExchangeClient):
    def get_orderbook(self, symbol):
        return None

    def wallet_balance(self, currency):
        return None

    def send_order(self, *args, **kwargs):
        raise AssertionError("unsafe real send reached")

    def get_order_list_status(self, *args, **kwargs):
        return []

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("unsafe real cancel reached")

    def cancel_all_open_orders(self, *args, **kwargs):
        raise AssertionError("unsafe real bulk cancel reached")

    def cancel_old_orders(self, *args, **kwargs):
        raise AssertionError("unsafe real stale cancel reached")


def _strategy(cfg):
    return Algo1Strategy(cfg)


def test_demo_engine_rejects_non_demo_exchange():
    cfg = MMConfig(mode="demo")

    with pytest.raises(RuntimeError, match="Demo Mode"):
        SimpleMarketMaker(
            cfg,
            Nobitex_portfolio(None),
            UnsafeRealExchange(),
            strategy=_strategy(cfg),
            persist_to_db=False,
        )


def test_demo_engine_rejects_mysql_persistence_before_connecting(monkeypatch):
    calls = []
    monkeypatch.setattr("engine.mysql.connector.connect", lambda **kwargs: calls.append(kwargs))
    cfg = MMConfig(mode="demo")
    market_data = UnsafeRealExchange()
    demo = DemoExchangeClient(market_data=market_data, pair="BTCIRT")

    with pytest.raises(RuntimeError, match="MySQL"):
        SimpleMarketMaker(
            cfg,
            Nobitex_portfolio(None),
            demo,
            strategy=_strategy(cfg),
            persist_to_db=True,
        )

    assert calls == []


def test_make_bot_demo_strips_authenticated_trading_capability(monkeypatch):
    cfg = run.make_cfg("btc", mode="demo")
    cfg.token = "must-not-propagate"

    bot = run.make_bot(cfg, mode="demo")
    market_data = bot.exchange._market_data

    assert bot.nobi_portfo.session.headers["Authorization"] == "Token None"
    assert market_data.token is None
    assert market_data.trading_enabled is False


def test_disabled_nobitex_adapter_blocks_order_and_cancel_calls(monkeypatch):
    cfg = run.make_cfg("btc", mode="demo")
    bot = run.make_bot(cfg, mode="demo")
    market_data = bot.exchange._market_data

    def forbidden_network(*args, **kwargs):
        raise AssertionError("network transport reached before Demo Mode safety guard")

    monkeypatch.setattr("exchange.nobitex.requests.post", forbidden_network)

    with pytest.raises(RuntimeError, match="Demo Mode"):
        market_data.send_order("buy", "btc", "rls", 1, 1)
    with pytest.raises(RuntimeError, match="Demo Mode"):
        market_data.cancel_order(1)
    with pytest.raises(RuntimeError, match="Demo Mode"):
        market_data.cancel_all_open_orders()
    with pytest.raises(RuntimeError, match="Demo Mode"):
        market_data.cancel_old_orders(10)


def test_make_bot_demo_owns_configured_persistent_paper_ledger(tmp_path):
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fees.json")
    cfg.demo_level1_fast_fill_enabled = True
    cfg.demo_level1_fill_delay_seconds = 3.0
    cfg.demo_level1_fill_fraction = 0.5
    cfg.demo_queue_enabled = True

    bot = run.make_bot(cfg, mode="demo")
    ledger = bot.exchange.ledger

    assert ledger.session_id == "shared-session"
    assert ledger.symbol == "USDTIRT"
    assert ledger.inventory == 0.0
    assert ledger.cash == 30_000_000
    assert ledger.state_path == tmp_path / "usdt-state.json"
    assert ledger.fee_state_path == tmp_path / "fees.json"
    assert ledger.level1_fill_delay_seconds == 3.0
    assert ledger.level1_fill_fraction == 0.5
    assert ledger.queue_enabled is True
