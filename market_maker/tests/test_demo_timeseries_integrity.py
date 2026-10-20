"""The per-round portfolio time series must never be rewritten retroactively.

Each persisted round row is a point-in-time observation: the balances as they
were IN that round. Two code paths used to stamp the CURRENT snapshot onto the
HISTORICAL row that created an order whenever that order later changed state
(engine._sync_demo_order_rows for the in-memory frame, and
engine._update_demo_result_orders -> result_store.update_order_rows for the
canonical SQLite store, which the CSV is exported from).

Because an order opened in round 2 typically fills several rounds later, this
scrambled the whole trajectory -- engine inventory rose a clean +0.999/round
while the store recorded 0.999, 7.992, 8.991, 4.995, ... That is the "large
unexpected jumps" seen on the dashboard, and it made PnL, drawdown and any
strategy comparison untrustworthy. See
reports/demo-portfolio-jump-investigation.md.

The ORDER's own lifecycle fields (status, matched amount, fill reason) MUST
still be back-filled -- that is per-order state, not a time series.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import engine as engine_module
import run
from exchange.base import ExchangeClient

T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


class RestThenTouchBook(ExchangeClient):
    """Wide book that periodically dips, so orders rest for several rounds and
    then fill -- the exact condition that triggered the corruption."""

    def __init__(self):
        self.n = 0

    def get_orderbook(self, symbol):
        self.n += 1
        shift = 0 if (self.n // 3) % 2 == 0 else -8_000
        return {
            "bids": [[str(1_000_000 + shift - i * 2_000), "10"] for i in range(11)],
            "asks": [[str(1_060_000 + shift + i * 2_000), "10"] for i in range(11)],
            "lastTradePrice": str(1_030_000 + shift),
        }

    def wallet_balance(self, currency):
        raise AssertionError("authenticated wallet access is forbidden")

    def send_order(self, *a, **k):
        raise AssertionError("real order access is forbidden")

    def get_order_list_status(self, *a, **k):
        raise AssertionError("real status access is forbidden")

    def cancel_order(self, *a, **k):
        raise AssertionError("real cancellation is forbidden")

    def cancel_all_open_orders(self, *a, **k):
        raise AssertionError("real cancellation is forbidden")

    def cancel_old_orders(self, *a, **k):
        raise AssertionError("real cancellation is forbidden")


class _LevelOneBid:
    """Rests a bid one tick inside the second level; with the matcher's
    level-1 fast fill it fills on the next observation, so orders open in
    one round and fill in a later one -- the condition under test."""

    def decide_order(self, ob, inv_current):
        price = float(ob["bids"][1][0]) + 10.0
        return {"bid": price, "ask": None, "bid level": 1, "ask level": None}


@pytest.fixture
def demo_run(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(engine_module, "TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))

    cfg = run.make_cfg("usdt", mode="demo", session_id="timeseries-session")
    cfg.max_rounds = 12
    cfg.time_step = 0
    cfg.cancel_after_minutes = 2
    cfg.output_csv = str(tmp_path / "usdt_demo.csv")
    cfg.demo_state_path = str(tmp_path / "state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fees.json")
    cfg.demo_archive_path = str(tmp_path / "arch.jsonl")
    cfg.demo_level1_fast_fill_enabled = True
    cfg.demo_level1_fill_delay_seconds = 30
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "hysteresis_ticks", 0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "requote_band_perc", 0.0)

    bot = run.make_bot(cfg, mode="demo")
    bot.strategy = _LevelOneBid()
    bot.exchange._market_data = RestThenTouchBook()
    clock = iter([T0 + timedelta(seconds=20 * i) for i in range(300)])
    bot.now_provider = lambda: next(clock)
    bot.sleep_fn = lambda _s: None

    # Record the ledger's true inventory right after each matching pass.
    ledger = bot.exchange.ledger
    original = ledger.process_market
    observed = []

    def traced(**market):
        events = original(**market)
        observed.append(ledger.inventory)
        return events

    ledger.process_market = traced
    bot.run()
    return bot, cfg, observed


def test_recorded_trajectory_is_strictly_consistent_round_to_round(demo_run):
    """Balances must move smoothly; a retroactive rewrite produced values that
    jumped forward and then BACKWARDS (7.992 -> 8.991 -> 9.990 -> 4.995)."""
    _bot, cfg, _observed = demo_run
    df = pd.read_csv(cfg.output_csv).sort_values("Round")

    inventory = df["inv_current"].tolist()
    # In this scenario only buys ever fill, so inventory can never decrease.
    assert inventory == sorted(inventory), inventory


def test_cash_and_inventory_move_in_opposite_directions_each_round(demo_run):
    """A buy fill must reduce cash and raise inventory in the SAME row. When
    rows were rewritten out of order this invariant broke."""
    _bot, cfg, _observed = demo_run
    df = pd.read_csv(cfg.output_csv).sort_values("Round")

    inv_delta = df["inv_current"].diff()
    cash_delta = df["cash_current"].diff()
    for i, (di, dc) in enumerate(zip(inv_delta[1:], cash_delta[1:]), start=1):
        if abs(di) < 1e-12:
            continue
        assert di * dc < 0, f"row {i}: inventory {di:+.4f} and cash {dc:+.2f} moved together"


def test_order_lifecycle_fields_are_still_backfilled(demo_run):
    """The fix must NOT stop order state from being updated on its own row --
    only the portfolio time series is protected."""
    _bot, cfg, _observed = demo_run
    df = pd.read_csv(cfg.output_csv)

    with_orders = df[df["Buy Order ID"].notna()]
    assert not with_orders.empty
    # Orders that opened in an earlier round and later filled must show their
    # terminal state, not a stale "open".
    assert "filled" in set(with_orders["Buy Status"])
    filled = with_orders[with_orders["Buy Status"] == "filled"]
    assert (filled["Buy Matched Amount"] > 0).all()
    assert (filled["Buy Remaining Amount"] == 0).all()


def test_sqlite_store_and_csv_agree(demo_run):
    """The CSV is exported from the canonical SQLite store; both must carry the
    same, uncorrupted trajectory."""
    _bot, cfg, _observed = demo_run
    from result_store import DemoResultStore

    csv_df = pd.read_csv(cfg.output_csv).sort_values("Round")
    store = DemoResultStore.open_existing(Path(cfg.output_csv).with_suffix(".sqlite3"))
    store_df = store.load_view().sort_values("Round")

    assert csv_df["inv_current"].tolist() == pytest.approx(store_df["inv_current"].tolist())
    assert csv_df["cash_current"].tolist() == pytest.approx(store_df["cash_current"].tolist())
