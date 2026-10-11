"""The engine with Algorithm 1 v2 quotes: a missing side is skipped, and an
order of ours that already rests at the quoted price is not re-posted."""

from datetime import datetime, timedelta, timezone

import pytest

import run
from engine import SimpleMarketMaker
from exchange.base import ExchangeClient


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


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
            "status": "Active",
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


class _AskOnly:
    def decide_order(self, ob, inv_current):
        return {"bid": None, "ask": 110.0, "bid level": None, "ask level": 1}


class _BidOnly:
    def decide_order(self, ob, inv_current):
        return {"bid": 90.0, "ask": None, "bid level": 1, "ask level": None}


class _FixedPair:
    def decide_order(self, ob, inv_current):
        return {"bid": 90.0, "ask": 110.0, "bid level": 1, "ask level": 1}


class _SyntheticBid:
    """v2 anchors a candidate beyond the last displayed level to a synthetic
    tick-rounded price, which has no book level (None)."""

    def decide_order(self, ob, inv_current):
        return {"bid": 90.0, "ask": 110.0, "bid level": None, "ask level": 1}


def _live_bot(monkeypatch, tmp_path, strategy, *, hysteresis_ticks, rounds=3):
    monkeypatch.setattr("engine.RESULTS_DIR", str(tmp_path))
    monkeypatch.delenv("LOB_FORWARD_CONFIG_DIR", raising=False)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "order_size", 10.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inv_max", 100.0)
    cfg = run.make_cfg("usdt", mode="real", session_id="one-sided-session")
    cfg.output_csv = str(tmp_path / "usdt_track.csv")
    cfg.max_rounds = rounds
    cfg.time_step = 0
    cfg.hysteresis_ticks = hysteresis_ticks
    exchange = _RestingOrdersExchange(base_balance=50.0, quote_balance=1e9)
    bot = SimpleMarketMaker(cfg, None, exchange, strategy=strategy, persist_to_db=False)
    return bot, exchange


def test_live_loop_skips_the_missing_bid_side(monkeypatch, tmp_path):
    bot, exchange = _live_bot(monkeypatch, tmp_path, _AskOnly(), hysteresis_ticks=0)

    frame = bot.run()

    assert frame["Sell Sent"].tolist() == [1, 1, 1]
    assert frame["Buy Sent"].tolist() == [0, 0, 0]
    assert frame["Bid Price Sent"].isna().all()
    assert frame["Buy Status"].tolist() == ["not_sent"] * 3
    assert {o["type"] for o in exchange.open_orders} == {"sell"}
    assert [o["price"] for o in exchange.open_orders] == [110.0] * 3


def test_live_loop_skips_the_missing_ask_side(monkeypatch, tmp_path):
    bot, exchange = _live_bot(monkeypatch, tmp_path, _BidOnly(), hysteresis_ticks=0)

    frame = bot.run()

    assert frame["Buy Sent"].tolist() == [1, 1, 1]
    assert frame["Sell Sent"].tolist() == [0, 0, 0]
    assert {o["type"] for o in exchange.open_orders} == {"buy"}


def test_live_loop_does_not_repost_a_price_that_already_rests(monkeypatch, tmp_path):
    """Round 1 posts both sides; the stand-in exchange keeps them open, so
    rounds 2 and 3 see our own orders at the quoted prices and post nothing."""
    bot, exchange = _live_bot(monkeypatch, tmp_path, _FixedPair(), hysteresis_ticks=5)

    frame = bot.run()

    assert frame["Buy Sent"].tolist() == [1, 0, 0]
    assert frame["Sell Sent"].tolist() == [1, 0, 0]
    assert len(exchange.open_orders) == 2


def test_live_loop_reposts_when_the_guard_is_off(monkeypatch, tmp_path):
    bot, exchange = _live_bot(monkeypatch, tmp_path, _FixedPair(), hysteresis_ticks=0)

    frame = bot.run()

    assert frame["Buy Sent"].tolist() == [1, 1, 1]
    assert len(exchange.open_orders) == 6


# ---------- demo loop ----------

class _DeterministicMarketData(ExchangeClient):
    def get_orderbook(self, symbol):
        return {
            "bids": [["99", "10"], ["98", "10"]],
            "asks": [["100", "10"], ["101", "10"]],
            "lastTradePrice": "100",
        }

    def wallet_balance(self, currency):
        raise AssertionError("authenticated wallet access is forbidden")

    def send_order(self, *args, **kwargs):
        raise AssertionError("real order access is forbidden")

    def get_order_list_status(self, *args, **kwargs):
        raise AssertionError("real order-status access is forbidden")

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_all_open_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_old_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")


def _demo_bot(monkeypatch, tmp_path, strategy, *, hysteresis_ticks, rounds=3):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inventory_sizing_enabled", False)
    cfg = run.make_cfg("usdt", mode="demo", session_id="one-sided-demo")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.demo_result_db_path = str(tmp_path / "usdt-demo.sqlite3")
    cfg.cash_initial = 10_000.0
    cfg.demo_inv_initial = 20.0
    cfg.order_size = 1.0
    cfg.max_rounds = rounds
    cfg.time_step = 0
    # hot-reloaded every round from the profile, so patch the profile
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "hysteresis_ticks", hysteresis_ticks)
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = _DeterministicMarketData()
    bot.strategy = strategy
    bot.telegram_reporter = None
    times = iter([T0 + timedelta(seconds=n) for n in range(rounds + 2)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None
    return bot


def test_demo_loop_records_the_missing_side_as_one_sided(monkeypatch, tmp_path):
    bot = _demo_bot(monkeypatch, tmp_path, _AskOnly(), hysteresis_ticks=0)

    result = bot.run()

    assert set(result["Quote Status"]) == {"quote_generated"}
    assert set(result["Buy Status"]) == {"not_sent_one_sided_quote"}
    assert set(result["Buy Not Sent Reason"]) == {"one_sided_quote"}
    assert result["Bid Price"].isna().all()
    assert result["Sell Order ID"].notna().all()          # a sell every round
    assert set(result["Sell Status"]) <= {"open", "cancelled"}   # cancelled at session end
    assert (result["Sell Limit Price"] == 110.0).all()


def test_demo_loop_places_an_order_at_a_synthetic_level(monkeypatch, tmp_path):
    """Regression: the paper ledger used to do int(selected_level) and the
    USDT demo worker crash-looped (TypeError) on the first synthetic bid."""
    bot = _demo_bot(monkeypatch, tmp_path, _SyntheticBid(), hysteresis_ticks=0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "demo_queue_enabled", True)

    result = bot.run()

    assert result["Buy Order ID"].notna().all()
    assert set(result["Buy Status"]) <= {"open", "cancelled"}
    assert result["Bid Level"].isna().all()
    assert result["Buy Selected Level"].isna().all()
    assert result["Ask Level"].tolist() == [1, 1, 1]
    buys = [o for o in bot.exchange.ledger.order_history.values() if o.side == "buy"]
    assert len(buys) == 3
    assert all(o.selected_level is None for o in buys)
    assert all(o.queue_ahead == 0.0 for o in buys)
    assert bot._demo_queue_ahead(bot.exchange.get_orderbook("USDTIRT"), "buy", None) == 0.0


def test_live_loop_records_a_synthetic_level_as_missing(monkeypatch, tmp_path):
    bot, exchange = _live_bot(monkeypatch, tmp_path, _SyntheticBid(), hysteresis_ticks=0)

    frame = bot.run()

    assert frame["Buy Sent"].tolist() == [1, 1, 1]
    assert frame["Bid Level"].isna().all()
    assert frame["Ask Level"].tolist() == [1, 1, 1]


def test_demo_csv_export_is_throttled_by_the_interval(monkeypatch, tmp_path):
    """The SQLite store is updated every round; the CSV export runs at start,
    at session end and otherwise at most every demo_csv_export_interval_seconds
    (rewriting it every round cost seconds of CPU on a long-lived store)."""
    from result_store import DemoResultStore

    calls = []
    original = DemoResultStore.export_bounded_csv
    monkeypatch.setattr(
        DemoResultStore, "export_bounded_csv",
        lambda self: calls.append(1) or original(self),
    )

    bot = _demo_bot(monkeypatch, tmp_path, _FixedPair(), hysteresis_ticks=0, rounds=3)
    bot.cfg.demo_csv_export_interval_seconds = 1e9
    bot.run()
    assert len(calls) == 2                        # start + final flush

    calls.clear()
    (tmp_path / "every").mkdir()
    bot = _demo_bot(monkeypatch, tmp_path / "every", _FixedPair(), hysteresis_ticks=0, rounds=3)
    bot.cfg.demo_csv_export_interval_seconds = 0
    result = bot.run()
    assert len(calls) == 2 + 3                    # plus one per round
    assert len(result) == 3


def test_demo_loop_keeps_a_resting_order_instead_of_reposting(monkeypatch, tmp_path):
    bot = _demo_bot(monkeypatch, tmp_path, _FixedPair(), hysteresis_ticks=5)

    result = bot.run()

    assert isinstance(result["Buy Order ID"].iloc[0], str)
    assert result["Buy Status"].tolist()[0] in {"open", "cancelled"}
    assert result["Buy Status"].tolist()[1:] == ["not_sent_resting_order_kept"] * 2
    assert result["Sell Status"].tolist()[1:] == ["not_sent_resting_order_kept"] * 2
    assert len(bot.exchange.ledger.order_history) == 2
