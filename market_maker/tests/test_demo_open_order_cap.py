"""The engine must bound how many paper orders rest concurrently per side.

inv_max/available cash alone allowed ~15-49 concurrent buys to accumulate at
near-identical prices, so a single price touch could fill them all at once
(inventory 0 -> 7.35 USDT in one round in real recorded data). See
reports/demo-portfolio-jump-investigation.md.
"""

from datetime import datetime, timedelta, timezone

import pytest

import run
from exchange.base import ExchangeClient
from exchange.nobitex import NobitexExchangeClient

T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


class DeepBookNoFill(ExchangeClient):
    """A wide, static book: quotes are always generated but never touched,
    so every submitted order simply rests -- exactly the condition under
    which orders used to pile up unbounded."""

    def get_orderbook(self, symbol):
        return {
            "bids": [[str(1_000_000 - level * 10_000), "10"] for level in range(11)],
            "asks": [[str(1_200_000 + level * 10_000), "10"] for level in range(11)],
            "lastTradePrice": "1100000",
        }

    def wallet_balance(self, currency):
        raise AssertionError("authenticated wallet access is forbidden")

    def send_order(self, *args, **kwargs):
        raise AssertionError("real order access is forbidden")

    def get_order_list_status(self, *args, **kwargs):
        raise AssertionError("real status access is forbidden")

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_all_open_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_old_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")


def _run_demo(tmp_path, monkeypatch, *, rounds, cap):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    for name in ("send_order", "cancel_order", "cancel_all_open_orders", "cancel_old_orders"):
        monkeypatch.setattr(NobitexExchangeClient, name, _forbidden(name))

    cfg = run.make_cfg("usdt", mode="demo", session_id="cap-session")
    cfg.max_rounds = rounds
    cfg.time_step = 0
    cfg.cancel_after_minutes = 10_000  # never expire during the test
    # Force orders to actually REST: with fast-fill on, a level-1 order fills
    # immediately regardless of price and the cap would never be exercised.
    cfg.demo_level1_fast_fill_enabled = False
    cfg.demo_max_open_orders_per_side = cap
    cfg.output_csv = str(tmp_path / "usdt_demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt_state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fees.json")
    cfg.demo_archive_path = str(tmp_path / "archive.jsonl")

    bot = run.make_bot(cfg, algorithm="algo1", exchange_name="nobitex", mode="demo")
    bot.exchange._market_data = DeepBookNoFill()
    clock = iter(T0 + timedelta(seconds=i) for i in range(rounds + 5))
    bot.now_provider = lambda: next(clock)
    bot.sleep_fn = lambda _seconds: None
    bot.run()
    return bot


def _forbidden(name):
    def tripwire(*args, **kwargs):
        raise AssertionError(f"forbidden real side effect: {name}")
    return tripwire


def test_open_buy_orders_never_exceed_the_configured_cap(tmp_path, monkeypatch):
    bot = _run_demo(tmp_path, monkeypatch, rounds=12, cap=3)

    ledger = bot.exchange.ledger
    open_buys = [o for o in ledger.active_orders.values() if o.side == "buy"]
    assert len(open_buys) <= 3


def test_cap_is_configurable(tmp_path, monkeypatch):
    bot = _run_demo(tmp_path, monkeypatch, rounds=12, cap=1)

    ledger = bot.exchange.ledger
    open_buys = [o for o in ledger.active_orders.values() if o.side == "buy"]
    assert len(open_buys) <= 1


def test_capped_rounds_are_recorded_with_an_explicit_reason(tmp_path, monkeypatch):
    import pandas as pd

    bot = _run_demo(tmp_path, monkeypatch, rounds=12, cap=2)
    df = pd.read_csv(bot.cfg.output_csv)

    statuses = set(df["Buy Status"].dropna())
    # Once the cap is reached the engine must say so explicitly rather than
    # silently skipping or mislabelling the round.
    assert "not_sent_max_open_orders" in statuses
    capped = df[df["Buy Status"] == "not_sent_max_open_orders"]
    assert (capped["Buy Not Sent Reason"] == "max_open_orders").all()
    assert (capped["Buy Original Amount"] == 0).all()
