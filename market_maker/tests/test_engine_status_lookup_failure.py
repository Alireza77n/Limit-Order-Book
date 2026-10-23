"""A failed order-status lookup must not end the live worker.

Both live units exited with code 1 on 23 October 2026 (USDT 09:46 UTC, BTC
10:27 UTC) when one Nobitex request timed out after 20 s inside
``update_inventory_and_cash``; the status client raises ``RuntimeError``
("order status lookup failed") by design, and the round loop had no handler.
The round must keep inventory and cash as they are and try again next round;
the cumulative matched amounts make the skipped round harmless."""
import pytest

import run
from engine import SimpleMarketMaker
from exchange.base import ExchangeClient


class _Strategy:
    pass


class _FlakyExchange(ExchangeClient):
    """Order-status lookups follow a script: an exception is raised, a list
    is returned."""
    is_demo = False

    def __init__(self, script):
        self._script = iter(script)
        self.cancel_calls = 0

    def _next(self):
        item = next(self._script)
        if isinstance(item, BaseException):
            raise item
        return item

    def get_orderbook(self, symbol):
        return None

    def wallet_balance(self, currency):
        return None

    def send_order(self, *args, **kwargs):
        return None

    def get_order_list_status(self, **kwargs):
        return self._next()

    def cancel_order(self, order_id):
        raise AssertionError("not used")

    def cancel_all_open_orders(self, **kwargs):
        return self._next()

    def cancel_old_orders(self, **kwargs):
        self.cancel_calls += 1
        return self._next()


def _bot(script):
    cfg = run.make_cfg("usdt", mode="real", session_id="real-session")
    exchange = _FlakyExchange(script)
    bot = SimpleMarketMaker(cfg, None, exchange, strategy=_Strategy(), persist_to_db=False)
    bot.recent_order_ids = ["owned-buy"]
    bot.tracked_orders = {"buy": {"owned-buy"}, "sell": set()}
    bot.order_history_ids = {"owned-buy"}
    return bot, exchange


TIMEOUT = RuntimeError("order status lookup failed: HTTPSConnectionPool(host='apiv2.nobitex.ir', port=443): Read timed out. (read timeout=20)")
DONE = [{"clientOrderId": "owned-buy", "pair": "USDTIRT", "type": "buy", "matchedAmount": 10,
         "unmatchedAmount": 0, "price": 100, "status": "done", "_raw": {"fee": 0.010}}]


def test_inventory_update_survives_a_status_timeout_and_keeps_the_books(capsys):
    bot, _ = _bot([TIMEOUT])

    inventory, cash = bot.update_inventory_and_cash("all", 1.5, 2_000.0)

    assert (inventory, cash) == (1.5, 2_000.0)
    assert bot.last_round_fee_irt == 0
    assert bot.tracked_orders == {"buy": {"owned-buy"}, "sell": set()}
    assert bot.processed_matched_amounts == {}
    assert "order status lookup failed" in capsys.readouterr().out


def test_the_fill_is_applied_once_after_the_lookup_recovers():
    # round 1: "open" page fails; round 2: "open" empty, "close" has the fill; round 3: nothing new
    bot, _ = _bot([TIMEOUT, [], DONE, [], DONE])

    inventory, cash = bot.update_inventory_and_cash("all", 0.0, 2_000.0)
    assert (inventory, cash) == (0.0, 2_000.0)

    inventory, cash = bot.update_inventory_and_cash("all", inventory, cash)
    assert inventory == pytest.approx(9.99)
    assert cash == pytest.approx(1_000.0)
    assert bot.last_round_fee_irt == pytest.approx(1.0)
    assert bot.tracked_orders["buy"] == set()

    inventory, cash = bot.update_inventory_and_cash("all", inventory, cash)
    assert inventory == pytest.approx(9.99)
    assert cash == pytest.approx(1_000.0)
    assert bot.last_round_fee_irt == 0


def test_a_partial_lookup_changes_nothing_this_round():
    # "open" succeeds, "close" times out: no half-applied round
    bot, _ = _bot([[], TIMEOUT])

    inventory, cash = bot.update_inventory_and_cash("all", 1.0, 500.0)

    assert (inventory, cash) == (1.0, 500.0)
    assert bot.processed_matched_amounts == {}


def test_stale_cancel_survives_a_status_timeout(capsys):
    bot, exchange = _bot([TIMEOUT])

    cancelled = bot._cancel_owned_stale_orders(max_age_minutes=3.0, df=None)

    assert cancelled == set()
    assert exchange.cancel_calls == 1
    assert bot.tracked_orders == {"buy": {"owned-buy"}, "sell": set()}
    assert bot.recent_order_ids == ["owned-buy"]
    assert "stale" in capsys.readouterr().out.lower()


def test_exposure_lookup_failure_skips_the_quote_instead_of_raising(capsys):
    bot, _ = _bot([TIMEOUT, [{"clientOrderId": "owned-buy", "pair": "USDTIRT", "type": "buy",
                               "status": "Active", "price": 100, "unmatchedAmount": 2}]])

    assert bot._open_order_totals_or_none() is None
    assert "live_exposure_lookup_failed" in capsys.readouterr().out

    buy_totals, sell_totals = bot._open_order_totals_or_none()
    assert buy_totals == {"amount": 2.0, "cash": 200.0}
    assert sell_totals == {"amount": 0.0, "cash": 0.0}


def test_an_incomplete_exposure_record_also_only_skips_the_round(capsys):
    bot, _ = _bot([[{"clientOrderId": "owned-buy", "pair": "USDTIRT", "type": "buy", "status": "Active",
                     "price": 100, "unmatchedAmount": None}]])

    assert bot._open_order_totals_or_none() is None
    assert "invalid amount or price" in capsys.readouterr().out
