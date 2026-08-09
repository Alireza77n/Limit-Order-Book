from datetime import datetime, timedelta, timezone
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
