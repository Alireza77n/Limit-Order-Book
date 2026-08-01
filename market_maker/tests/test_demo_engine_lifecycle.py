from datetime import datetime, timedelta, timezone

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

    def order_submitted(self, ledger, order):
        self.submissions.append((order.client_order_id, order.status))

    def order_changed(self, ledger, event):
        self.changes.append((event.order_id, event.status, event.reason))


def test_demo_engine_uses_canonical_lifecycle_and_explicit_csv(monkeypatch, tmp_path):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(
        "engine.mysql.connector.connect",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("MySQL forbidden in Demo Mode")),
    )
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.cash_initial = 1_000.0
    cfg.order_size = 1.0
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
    assert set(result["Session_id"]) == {"shared-session"}
    assert set(result["Mode"]) == {"demo"}

    first = result.iloc[0]
    assert first["Buy Status"] == "filled"
    assert first["Buy Matched Amount"] == pytest.approx(1.0)
    assert first["Buy Remaining Amount"] == 0
    assert first["Buy Fill Reason"] == "level1_fast_fill"
    assert first["inv_current"] == pytest.approx(0.999)
    assert first["cash_current"] == pytest.approx(910.0)
    assert first["Round Fee"] == pytest.approx(0.09)
    assert first["Sell Status"] == "not_sent_insufficient_inventory"
    assert first["Sell Not Sent Reason"] == "insufficient_inventory"

    second = result.iloc[1]
    assert second["Buy Status"] == "cancelled"
    assert second["Sell Status"] == "cancelled"
    assert result["inv_current"].iloc[1] == pytest.approx(0.999)
    assert result["cash_current"].iloc[1] == pytest.approx(910.0)
    assert result["Round Fee"].iloc[1] == pytest.approx(0.09)
    assert [status for _, status in reporter.submissions] == ["open", "open", "open"]
    assert [status for _, status, _ in reporter.changes] == ["filled", "cancelled", "cancelled"]


def test_demo_queue_ahead_uses_displayed_selected_level_volume(tmp_path):
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


def test_demo_engine_submits_selected_level_queue_and_round(tmp_path):
    cfg = run.make_cfg("usdt", mode="demo", session_id="queue-session")
    cfg.output_csv = str(tmp_path / "queue.csv")
    cfg.demo_state_path = str(tmp_path / "queue-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "queue-fees.json")
    cfg.demo_queue_enabled = True
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


def test_demo_engine_recovers_after_temporary_market_data_failure(tmp_path):
    class FlakyMarketData(DeterministicMarketData):
        def get_orderbook(self, symbol):
            if self.orderbook_calls == 0:
                self.orderbook_calls += 1
                raise OSError("temporary feed outage")
            return super().get_orderbook(symbol)

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


def test_demo_csv_failure_is_reported_without_terminating(monkeypatch, tmp_path):
    frame = pd.DataFrame({"Round": [1]})
    monkeypatch.setattr(frame, "to_csv", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert run.SimpleMarketMaker._write_demo_csv(frame, str(tmp_path / "result.csv")) is False
