from datetime import datetime, timedelta, timezone
import itertools
import signal

import pytest

import run
from exchange.base import ExchangeClient
import pandas as pd


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


class DeterministicMarketData(ExchangeClient):
    def __init__(self):
        self.orderbook_calls = 0

    def get_orderbook(self, symbol):
        self.orderbook_calls += 1
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


class FixedLevelOneStrategy:
    def decide_order(self, ob, inv_current):
        return {
            "bid": 90.0,
            "ask": 110.0,
            "bid level": 1,
            "ask level": 1,
        }


class RecordingReporter:
    def __init__(self):
        self.submissions = []
        self.changes = []
        self.no_quote_alerts = []
        self.no_quote_recoveries = []
        self.round_summaries = []

    def round_summary(self, text):
        self.round_summaries.append(text)
        return 700 + len(self.round_summaries)

    def order_submitted(self, ledger, order, *, reply_to_message_id=None):
        self.submissions.append((order.client_order_id, order.status, reply_to_message_id))

    def order_changed(self, ledger, event):
        self.changes.append((event.order_id, event.status, event.reason))

    def no_quote_alert(self, decision):
        self.no_quote_alerts.append(decision)
        return 501

    def no_quote_recovered(self, decision):
        self.no_quote_recoveries.append(decision)
        return 502


def test_demo_engine_uses_canonical_lifecycle_and_explicit_csv(monkeypatch, tmp_path):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(
        "engine.mysql.connector.connect",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("MySQL forbidden in Demo Mode")),
    )
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.demo_result_db_path = str(tmp_path / "usdt-demo.sqlite3")
    cfg.demo_csv_export_max_rows = 1
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
    # This test pins the order lifecycle with a constant order size; linear
    # inventory sizing has its own tests further down.
    cfg.inventory_sizing_enabled = False
    cfg.max_rounds = 2
    cfg.time_step = 0
    cfg.demo_level1_fill_delay_seconds = 0
    cfg.demo_level1_fill_fraction = 1.0

    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    reporter = RecordingReporter()
    bot.telegram_reporter = reporter
    times = iter([T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None

    result = bot.run()

    required_columns = {
        "Mode", "Round", "Quote Status", "No Quote Reason",
        "Buy Order ID", "Buy Status", "Buy Not Sent Reason",
        "Buy Original Amount", "Buy Matched Amount", "Buy Remaining Amount",
        "Buy Fill Reason", "Buy Selected Level", "Buy Limit Price", "Buy Locked Cash",
        "Sell Order ID", "Sell Status", "Sell Not Sent Reason",
        "Sell Original Amount", "Sell Matched Amount", "Sell Remaining Amount",
        "Sell Fill Reason", "Sell Selected Level", "Sell Limit Price", "Sell Locked Inventory",
        "Round Fee", "Session Pair Fee", "Session All Symbols Fee",
        "Pair All Time Fee", "All Symbols All Time Fee",
    }
    assert required_columns.issubset(result.columns)
    assert len(result) == 2
    assert (tmp_path / "usdt-demo.sqlite3").exists()
    assert len(pd.read_csv(cfg.output_csv)) == 1
    assert set(result["Session_id"]) == {"shared-session"}
    assert set(result["Mode"]) == {"demo"}

    # Round 1: the buy is SUBMITTED here. Its lifecycle fields are back-filled
    # onto this row once it terminates (that is per-order state), but the
    # portfolio columns must stay as observed in round 1 -- the fill happens in
    # round 2's matching pass, so round 1 still shows the untouched starting
    # balances. These previously read 0.999/910.0/0.09 because round 2's
    # snapshot was being stamped backwards onto this row; see
    # test_demo_timeseries_integrity.py and
    # reports/demo-portfolio-jump-investigation.md.
    first = result.iloc[0]
    assert first["Buy Status"] == "filled"
    assert first["Buy Matched Amount"] == pytest.approx(1.0)
    assert first["Buy Remaining Amount"] == 0
    assert first["Buy Fill Reason"] == "level1_fast_fill"
    assert first["inv_current"] == pytest.approx(0.0)
    assert first["cash_current"] == pytest.approx(1_000.0)
    assert first["Round Fee"] == pytest.approx(0.0)
    assert first["Sell Status"] == "not_sent_insufficient_inventory"
    assert first["Sell Not Sent Reason"] == "insufficient_inventory"

    # Round 2: the fill actually lands here, so THIS row carries the balance
    # change and the fee.
    second = result.iloc[1]
    assert second["Buy Status"] == "cancelled"
    assert second["Sell Status"] == "cancelled"
    assert result["inv_current"].iloc[1] == pytest.approx(0.999)
    assert result["cash_current"].iloc[1] == pytest.approx(910.0)
    assert result["Round Fee"].iloc[1] == pytest.approx(0.09)
    assert [status for _, status, _ in reporter.submissions] == ["open", "open", "open"]
    assert [parent for _, _, parent in reporter.submissions] == [701, 702, 702]
    assert len(reporter.round_summaries) == 2
    assert "🧪 <b>DEMO MODE</b>" in reporter.round_summaries[0]
    assert "<b>USDTIRT</b> · Round 1" in reporter.round_summaries[0]
    assert "<b>Orders</b>" in reporter.round_summaries[0]
    assert [status for _, status, _ in reporter.changes] == ["filled", "cancelled", "cancelled"]
    assert [reason for _, _, reason in reporter.changes] == [
        "level1_fast_fill", "session_end", "session_end",
    ]


def _sizing_bot(monkeypatch, tmp_path, cash_initial):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="sizing-session")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.demo_result_db_path = str(tmp_path / "usdt-demo.sqlite3")
    cfg.demo_inv_initial = 0.0
    cfg.inv_initial = 0.0
    cfg.cash_initial = cash_initial
    cfg.order_size = 1.0
    cfg.max_rounds = 1
    cfg.time_step = 0
    cfg.demo_level1_fill_delay_seconds = 0
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0, T0 + timedelta(seconds=1)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None
    return bot.run()


def test_buy_clamped_to_dust_by_low_cash_is_not_sent(monkeypatch, tmp_path):
    """Cash affording only 5.6% of order_size used to place a real order --
    that is how a worker spent its last rial on a 594 IRT sliver and stalled.
    Bid is 90, so 5.0 cash affords 0.056 of the configured size 1.0."""
    frame = _sizing_bot(monkeypatch, tmp_path, cash_initial=5.0)

    assert frame["Buy Status"].iloc[0] == "not_sent_insufficient_cash"
    assert not frame["Buy Original Amount"].iloc[0]


def test_buy_clamped_above_the_floor_still_sends_a_partial_order(monkeypatch, tmp_path):
    """The floor must not suppress ordinary reduced capacity: 45.0 cash at
    bid 90 affords 0.5, half the configured size, which is a real order."""
    frame = _sizing_bot(monkeypatch, tmp_path, cash_initial=45.0)

    assert frame["Buy Status"].iloc[0] != "not_sent_insufficient_cash"
    assert frame["Buy Original Amount"].iloc[0] == pytest.approx(0.5)


def test_inventory_sizing_changes_submitted_buy_and_sell_amounts(monkeypatch, tmp_path):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inventory_sizing_enabled", True)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inventory_sizing_strength", 0.75)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inventory_sizing_min_fraction", 0.25)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "order_size", 10.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inv_max", 100.0)
    cfg = run.make_cfg("usdt", mode="demo", session_id="inventory-sizing-session")
    cfg.output_csv = str(tmp_path / "sizing.csv")
    cfg.demo_state_path = str(tmp_path / "sizing-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "sizing-fees.json")
    cfg.demo_result_db_path = str(tmp_path / "sizing.sqlite3")
    cfg.demo_inv_initial = cfg.inv_initial = 75.0
    cfg.cash_initial = 1_000_000.0
    cfg.max_rounds = 1
    cfg.time_step = 0
    cfg.demo_level1_fast_fill_enabled = False
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    bot.now_provider = lambda: T0
    bot.sleep_fn = lambda seconds: None

    frame = bot.run()

    assert frame["Buy Original Amount"].iloc[0] == pytest.approx(6.25)
    assert frame["Sell Original Amount"].iloc[0] == pytest.approx(13.75)


def _profile_default_sizing_frame(monkeypatch, tmp_path, *, rounds, inventory):
    """Demo rounds on the profile's own sizing settings (linear, full
    strength, no floor) with order_size 10 inside a 0..100 inventory band.
    The book never touches bid 90 / ask 110, so every order rests."""
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "order_size", 10.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inv_max", 100.0)
    cfg = run.make_cfg("usdt", mode="demo", session_id="linear-sizing-session")
    cfg.output_csv = str(tmp_path / "linear.csv")
    cfg.demo_state_path = str(tmp_path / "linear-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "linear-fees.json")
    cfg.demo_result_db_path = str(tmp_path / "linear.sqlite3")
    cfg.demo_inv_initial = cfg.inv_initial = inventory
    cfg.cash_initial = 1_000_000.0
    cfg.max_rounds = rounds
    cfg.time_step = 0
    cfg.demo_level1_fast_fill_enabled = False
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    clock = itertools.count()
    bot.now_provider = lambda: T0 + timedelta(seconds=next(clock))
    bot.sleep_fn = lambda seconds: None
    return bot.run()


def test_profile_default_sizing_counts_resting_buys_as_inventory(monkeypatch, tmp_path):
    """Empty inventory: the first buy is 2 x order_size and nothing is sold.
    Each later buy shrinks because the buys already resting count toward the
    band: 20, then 2*10*(1 - 20/100) = 16, then 2*10*(1 - 36/100) = 12.8."""
    frame = _profile_default_sizing_frame(
        monkeypatch, tmp_path, rounds=3, inventory=0.0
    )

    assert frame["Buy Original Amount"].tolist() == pytest.approx([20.0, 16.0, 12.8])
    assert set(frame["Sell Status"]) == {"not_sent_insufficient_inventory"}


def test_buy_at_top_of_inventory_band_is_reported_as_inventory_sizing(monkeypatch, tmp_path):
    """Full inventory scales the buy to zero. With a million in cash that must
    not be logged as a cash shortage."""
    frame = _profile_default_sizing_frame(
        monkeypatch, tmp_path, rounds=1, inventory=100.0
    )

    assert frame["Buy Status"].iloc[0] == "not_sent_inventory_sizing"
    assert frame["Buy Not Sent Reason"].iloc[0] == "inventory_sizing"
    assert frame["Sell Original Amount"].iloc[0] == pytest.approx(20.0)


def test_sell_near_bottom_of_inventory_band_is_reported_as_inventory_sizing(monkeypatch, tmp_path):
    """Inventory 2 of 100 gives a sell target of 2*10*0.02 = 0.4, below the
    tradable minimum of 1.0, while 2.0 is free to sell: the sizing rule held
    the sell back, not a lack of inventory."""
    frame = _profile_default_sizing_frame(
        monkeypatch, tmp_path, rounds=1, inventory=2.0
    )

    assert frame["Sell Status"].iloc[0] == "not_sent_inventory_sizing"
    assert frame["Sell Not Sent Reason"].iloc[0] == "inventory_sizing"
    assert frame["Buy Original Amount"].iloc[0] == pytest.approx(19.6)


def test_buy_inventory_cap_uses_actual_clamped_buy_amount(monkeypatch, tmp_path):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="buy-cap-session")
    cfg.output_csv = str(tmp_path / "buy-cap.csv")
    cfg.demo_state_path = str(tmp_path / "buy-cap-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "buy-cap-fees.json")
    cfg.demo_result_db_path = str(tmp_path / "buy-cap.sqlite3")
    cfg.demo_inv_initial = cfg.inv_initial = 49.6
    cfg.inv_max = 50.0
    cfg.cash_initial = 27.0
    cfg.order_size = 1.0
    # Fixed size on purpose: the cap must use the cash-clamped amount (0.3),
    # which linear sizing would shrink further at this inventory.
    cfg.inventory_sizing_enabled = False
    cfg.max_rounds = 1
    cfg.time_step = 0
    cfg.demo_level1_fast_fill_enabled = False
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    bot.now_provider = lambda: T0
    bot.sleep_fn = lambda seconds: None

    frame = bot.run()

    assert frame["Buy Status"].iloc[0] != "not_sent_inventory_cap"
    assert frame["Buy Original Amount"].iloc[0] == pytest.approx(0.3)


def test_fully_locked_inventory_is_not_reported_as_insufficient(monkeypatch, tmp_path):
    """Holding stock whose every unit is already committed to resting sells is
    not the same as holding nothing. Calling both 'insufficient_inventory' sent
    an operator hunting a phantom accounting bug when inv_current showed plenty."""
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="locked-session")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fees.json")
    cfg.demo_result_db_path = str(tmp_path / "db.sqlite3")
    cfg.demo_inv_initial = cfg.inv_initial = 1.0
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
    # Fixed size so round 1 sells the whole 1.0 and round 2 finds it locked.
    cfg.inventory_sizing_enabled = False
    cfg.max_rounds = 2
    cfg.time_step = 0
    # Keep the round-1 sell resting so round 2 finds all inventory locked.
    cfg.demo_level1_fast_fill_enabled = False
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None
    frame = bot.run()

    second = frame.iloc[1]
    assert second["Locked Inventory"] > 0
    assert second["Available Inventory"] == pytest.approx(0)
    assert second["Sell Not Sent Reason"] == "inventory_locked_in_open_orders"
    assert second["Sell Status"] == "not_sent_inventory_locked_in_open_orders"


_HOT_LOCKED_GUARD_FIELDS = {
    "order_size", "inv_max",
    "buy_locked_cash_limit_enabled", "buy_locked_cash_limit_ratio",
    "sell_locked_inventory_limit_enabled", "sell_locked_inventory_limit_ratio",
}


def _locked_guard_bot(monkeypatch, tmp_path, *, rounds, **cfg_overrides):
    """4 rounds against a book that never touches either side's limit price
    (bid 90 / ask 110 vs a constant best bid 99 / ask 100), so orders rest
    open and locked exposure accumulates round over round instead of being
    released by an immediate fill.

    HOT-mode fields (the four new guard fields, order_size, inv_max) must be
    patched onto TRADING_PROFILES rather than set on cfg directly: the demo
    worker reloads hot config from the profile+overlay at every round
    boundary (RuntimeConfigSynchronizer.reload_at_round_boundary), which
    would otherwise silently revert a direct cfg.attr assignment back to the
    profile default after round 1 -- exactly the bug this test caught in
    itself the first time it was written, with the guard reading as
    permanently disabled.
    """
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    for key, value in cfg_overrides.items():
        if key in _HOT_LOCKED_GUARD_FIELDS:
            monkeypatch.setitem(run.TRADING_PROFILES["usdt"], key, value)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "order_size", 1.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inv_max", 1000.0)
    cfg = run.make_cfg("usdt", mode="demo", session_id="locked-guard-session")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.demo_result_db_path = str(tmp_path / "usdt-demo.sqlite3")
    cfg.demo_level1_fast_fill_enabled = False
    # The guard arithmetic in these tests assumes one order_size per round.
    cfg.inventory_sizing_enabled = False
    cfg.max_rounds = rounds
    cfg.time_step = 0
    for key, value in cfg_overrides.items():
        if key not in _HOT_LOCKED_GUARD_FIELDS:
            setattr(cfg, key, value)
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0 + timedelta(seconds=i) for i in range(rounds + 2)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None
    return bot.run()


def test_locked_cash_guard_blocks_only_the_order_that_would_exceed_ratio(monkeypatch, tmp_path):
    """cash_initial=1000, bid=90, order_size=1.0 -> 90 locked per resting buy.
    Limit 30% of 1000 = 300: 3 orders (270) fit, a 4th (360) does not.

    The bot cancels every still-resting order when the session ends, which
    retroactively updates their historical rows to 'cancelled' (see
    _finish_demo_session) -- so accepted rounds are asserted by the absence
    of the guard's reason, not by a terminal 'open' status.
    """
    frame = _locked_guard_bot(
        monkeypatch, tmp_path, rounds=4,
        demo_inv_initial=0.0, cash_initial=1_000.0,
        buy_locked_cash_limit_enabled=True, buy_locked_cash_limit_ratio=0.3,
    )
    statuses = frame["Buy Status"].tolist()
    assert all(status != "not_sent_locked_cash_limit" for status in statuses[:3])
    assert statuses[3] == "not_sent_locked_cash_limit"


def test_locked_cash_guard_disabled_lets_all_four_orders_through(monkeypatch, tmp_path):
    frame = _locked_guard_bot(
        monkeypatch, tmp_path, rounds=4,
        demo_inv_initial=0.0, cash_initial=1_000.0,
        buy_locked_cash_limit_enabled=False, buy_locked_cash_limit_ratio=0.3,
    )
    assert all(status != "not_sent_locked_cash_limit" for status in frame["Buy Status"])
    assert frame["Buy Original Amount"].astype(bool).all()


def test_locked_inventory_guard_blocks_only_the_order_that_would_exceed_ratio(monkeypatch, tmp_path):
    """demo_inv_initial=10, order_size=1.0 -> 1 locked per resting sell.
    Limit 30% of 10 = 3: 3 orders fit exactly (<=), a 4th does not."""
    frame = _locked_guard_bot(
        monkeypatch, tmp_path, rounds=4,
        demo_inv_initial=10.0, cash_initial=1_000_000.0,
        sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.3,
    )
    statuses = frame["Sell Status"].tolist()
    assert all(status != "not_sent_locked_inventory_limit" for status in statuses[:3])
    assert statuses[3] == "not_sent_locked_inventory_limit"


def test_locked_inventory_guard_unaffected_when_buy_guard_disabled(monkeypatch, tmp_path):
    """BUY and SELL guards are independent switches: SELL enabled and tight
    while BUY stays fully unrestricted must still block only the SELL side."""
    frame = _locked_guard_bot(
        monkeypatch, tmp_path, rounds=4,
        demo_inv_initial=10.0, cash_initial=1_000_000.0,
        buy_locked_cash_limit_enabled=False,
        sell_locked_inventory_limit_enabled=True, sell_locked_inventory_limit_ratio=0.3,
    )
    assert all(status != "not_sent_locked_cash_limit" for status in frame["Buy Status"])
    assert frame["Sell Status"].iloc[3] == "not_sent_locked_inventory_limit"


def test_cancelling_a_locked_order_releases_its_exposure(monkeypatch, tmp_path):
    """3 resting buys hit the 30% ratio (270/1000); cancelling one before
    round 4 must free its share so a new order is accepted again."""
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    # HOT fields: must be patched onto the profile, not set on cfg directly --
    # see _locked_guard_bot's docstring for why a direct cfg assignment gets
    # silently reverted at the first round-boundary reload.
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "order_size", 1.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inv_max", 1000.0)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "buy_locked_cash_limit_enabled", True)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "buy_locked_cash_limit_ratio", 0.3)
    cfg = run.make_cfg("usdt", mode="demo", session_id="locked-cancel-session")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.demo_result_db_path = str(tmp_path / "usdt-demo.sqlite3")
    cfg.demo_level1_fast_fill_enabled = False
    cfg.inventory_sizing_enabled = False  # 90 locked per resting buy, as below
    cfg.demo_inv_initial = 0.0
    cfg.cash_initial = 1_000.0
    cfg.max_rounds = 4
    cfg.time_step = 0
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0 + timedelta(seconds=i) for i in range(6)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None
    ledger = bot.exchange.ledger
    original_process_market = ledger.process_market

    def process_market_and_cancel_first_buy(**kwargs):
        events = original_process_market(**kwargs)
        first_buy_id = next(
            (oid for oid, o in ledger.active_orders.items() if o.side == "buy"), None,
        )
        if first_buy_id is not None:
            cancel_event = ledger.cancel_order(
                first_buy_id, now=kwargs["now"], reason="test_release",
            )
            if cancel_event is not None:
                events = list(events) + [cancel_event]
            ledger.process_market = original_process_market  # cancel once only
        return events

    ledger.process_market = process_market_and_cancel_first_buy
    frame = bot.run()

    # Round 4 would have been blocked with all 3 originals still resting
    # (270/1000 -> +90 = 360 > 300); one cancellation frees 90 of headroom.
    assert frame["Buy Status"].iloc[3] != "not_sent_locked_cash_limit"


def test_demo_queue_ahead_uses_displayed_selected_level_volume(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.demo_state_path = str(tmp_path / "state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fees.json")
    cfg.demo_queue_enabled = True
    bot = run.make_bot(cfg, mode="demo")
    book = {
        "bids": [["100", "2"], ["99", "7.5"]],
        "asks": [["101", "3"], ["102", "8.5"]],
    }

    assert bot._demo_queue_ahead(book, "buy", 1) == pytest.approx(7.5)
    assert bot._demo_queue_ahead(book, "sell", 1) == pytest.approx(8.5)

    cfg.demo_queue_enabled = False
    assert bot._demo_queue_ahead(book, "buy", 1) == 0


def test_demo_engine_submits_selected_level_queue_and_round(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "demo_queue_enabled", True)
    cfg = run.make_cfg("usdt", mode="demo", session_id="queue-session")
    cfg.output_csv = str(tmp_path / "queue.csv")
    cfg.demo_state_path = str(tmp_path / "queue-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "queue-fees.json")
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
    cfg.max_rounds = 1
    cfg.time_step = 0

    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0, T0 + timedelta(seconds=1)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None

    bot.run()

    buy = next(order for order in bot.exchange.ledger.order_history.values() if order.side == "buy")
    assert buy.selected_level == 1
    assert buy.queue_ahead == pytest.approx(10.0)
    assert buy.round_no == 1


def test_demo_engine_recovers_after_temporary_market_data_failure(monkeypatch, tmp_path):
    class FlakyMarketData(DeterministicMarketData):
        def get_orderbook(self, symbol):
            if self.orderbook_calls == 0:
                self.orderbook_calls += 1
                raise OSError("temporary feed outage")
            return super().get_orderbook(symbol)

    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="recovery-session")
    cfg.output_csv = str(tmp_path / "recovery.csv")
    cfg.demo_state_path = str(tmp_path / "recovery-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "recovery-fees.json")
    cfg.cash_initial = 1_000.0
    cfg.max_rounds = 2
    cfg.time_step = 0

    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = FlakyMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None

    result = bot.run()

    assert len(result) == 1
    assert result.iloc[0]["Round"] == 2
    assert all(
        order.fill_reason != "stack_shutdown"
        for order in bot.exchange.ledger.order_history.values()
    )


def test_telegram_reporter_failure_does_not_cancel_or_stop_rounds(monkeypatch, tmp_path):
    class FailingReporter(RecordingReporter):
        def round_summary(self, text):
            raise OSError("telegram unavailable")

        def order_submitted(self, ledger, order, *, reply_to_message_id=None):
            raise OSError("telegram unavailable")

    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="telegram-retry-session")
    cfg.output_csv = str(tmp_path / "telegram.csv")
    cfg.demo_state_path = str(tmp_path / "telegram-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "telegram-fees.json")
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
    cfg.max_rounds = 1
    cfg.time_step = 0
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    bot.telegram_reporter = FailingReporter()
    bot.now_provider = lambda: T0
    bot.sleep_fn = lambda seconds: None

    result = bot.run()

    assert len(result) == 1
    assert {order.fill_reason for order in bot.exchange.ledger.order_history.values()} == {
        "session_end"
    }


def test_worker_fatal_exit_preserves_orders_for_same_session_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="fatal-retry-session")
    cfg.output_csv = str(tmp_path / "fatal.csv")
    cfg.demo_state_path = str(tmp_path / "fatal-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fatal-fees.json")
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
    cfg.max_rounds = 2
    cfg.time_step = 0
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    bot.now_provider = lambda: T0
    bot.sleep_fn = lambda seconds: None
    bot._ensure_demo_operations(cfg.output_csv)
    bot.result_store.append_round = lambda row: (_ for _ in ()).throw(OSError("disk fault"))

    with pytest.raises(OSError, match="disk fault"):
        bot.run()

    assert bot.exchange.ledger.active_orders
    assert all(
        order.fill_reason != "stack_shutdown"
        for order in bot.exchange.ledger.active_orders.values()
    )

    restored = run.make_bot(cfg, mode="demo")
    assert set(restored.exchange.ledger.active_orders) == set(bot.exchange.ledger.active_orders)


def test_coordinated_signal_is_the_only_stack_shutdown_cancellation(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="coordinated-session")
    cfg.output_csv = str(tmp_path / "coordinated.csv")
    cfg.demo_state_path = str(tmp_path / "coordinated-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "coordinated-fees.json")
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
    cfg.max_rounds = 2
    cfg.time_step = 1
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = FixedLevelOneStrategy()
    times = iter([T0, T0 + timedelta(seconds=1)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: (_ for _ in ()).throw(
        run.CoordinatedShutdown(signal.SIGTERM)
    )

    result = bot.run()

    assert len(result) == 1
    assert bot.exchange.ledger.active_orders == {}
    cancelled = [
        order for order in bot.exchange.ledger.order_history.values()
        if order.status == "cancelled"
    ]
    assert cancelled
    assert {order.fill_reason for order in cancelled} == {
        "coordinated_stack_shutdown"
    }


def test_demo_csv_failure_is_reported_without_terminating(monkeypatch, tmp_path):
    frame = pd.DataFrame({"Round": [1]})
    monkeypatch.setattr(frame, "to_csv", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert run.SimpleMarketMaker._write_demo_csv(frame, str(tmp_path / "result.csv")) is False


class AlwaysNoQuoteStrategy:
    def decide_order(self, ob, inv_current):
        return None

    def no_quote_reason(self, ob, inv_current):
        return "profitability_threshold"

    def diagnostics(self, ob, inv_current, *, round_no, session_id):
        return {
            "symbol": "USDTIRT",
            "session_id": str(session_id),
            "round": round_no,
            "commission_perc": 0.001,
            "exp_ret_min": 0.001,
            "required_threshold": 0.002,
            "candidate_results": [{
                "bid_level": 1,
                "ask_level": 1,
                "bid_price": 99.0,
                "ask_price": 100.0,
                "last_price": 100.0,
                "spread_ratio": 0.01,
                "passed": False,
            }],
        }


def test_demo_engine_records_no_quote_episode_and_alert_state(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="no-quote-session")
    cfg.output_csv = str(tmp_path / "no-quote.csv")
    cfg.demo_result_db_path = str(tmp_path / "no-quote.sqlite3")
    cfg.demo_state_path = str(tmp_path / "no-quote-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "no-quote-fees.json")
    cfg.no_quote_alert_rounds = 2
    cfg.no_quote_alert_seconds = 999
    cfg.no_quote_alert_cooldown_seconds = 999
    cfg.max_rounds = 3
    cfg.time_step = 0
    bot = run.make_bot(cfg, mode="demo")
    bot.exchange._market_data = DeterministicMarketData()
    bot.strategy = AlwaysNoQuoteStrategy()
    reporter = RecordingReporter()
    bot.telegram_reporter = reporter
    times = iter([T0 + timedelta(seconds=n) for n in range(4)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None

    result = bot.run()

    assert list(result["No Quote Consecutive Count"]) == [1, 2, 3]
    assert set(result["No Quote Reason"]) == {"profitability_threshold"}
    assert len(reporter.no_quote_alerts) == 1
    diagnostics = result.iloc[-1]["No Quote Candidate Diagnostics"]
    assert '"commission":0.001' in diagnostics
    assert '"spread":0.01' in diagnostics


def test_demo_restart_continues_round_numbers_in_same_session(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="restart-session")
    cfg.output_csv = str(tmp_path / "restart.csv")
    cfg.demo_result_db_path = str(tmp_path / "restart.sqlite3")
    cfg.demo_state_path = str(tmp_path / "restart-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "restart-fees.json")
    cfg.max_rounds = 1
    cfg.time_step = 0

    for offset in (0, 2):
        bot = run.make_bot(cfg, mode="demo")
        bot.exchange._market_data = DeterministicMarketData()
        bot.strategy = AlwaysNoQuoteStrategy()
        bot.telegram_reporter = RecordingReporter()
        times = iter([T0 + timedelta(seconds=offset), T0 + timedelta(seconds=offset + 1)])
        bot.now_provider = lambda: next(times)
        bot.sleep_fn = lambda seconds: None
        result = bot.run()

    assert list(result["Round"]) == [1, 2]
    assert list(result["No Quote Consecutive Count"]) == [1, 2]
