from datetime import datetime, timedelta, timezone

from no_quote import NoQuoteMonitor
from result_store import DemoResultStore


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
COLUMNS = ["Session_id", "Crypto", "Round"]
DIAGNOSTICS = {
    "reason": "profitability_threshold",
    "commission": 0.001,
    "exp_ret_min": 0.001,
    "required_threshold": 0.002,
    "candidates": [
        {
            "bid_level": 5,
            "ask_level": 10,
            "bid_price": 100.0,
            "ask_price": 100.1,
            "last_price": 100.05,
            "spread": 0.001,
        }
    ],
}


def make_monitor(tmp_path, symbol="USDTIRT", **overrides):
    store = DemoResultStore(tmp_path / "results.sqlite3", COLUMNS)
    options = {
        "alert_rounds": 3,
        "alert_seconds": 60,
        "cooldown_seconds": 120,
    }
    options.update(overrides)
    return NoQuoteMonitor(store, "session-1", symbol, **options)


def test_tracks_consecutive_rounds_resets_on_quote_and_isolates_symbols(tmp_path):
    usdt = make_monitor(tmp_path, "USDTIRT")
    btc = make_monitor(tmp_path, "BTCIRT")

    usdt.record_no_quote(1, T0, "profitability_threshold", DIAGNOSTICS)
    usdt.record_no_quote(2, T0 + timedelta(seconds=10), "profitability_threshold", DIAGNOSTICS)

    assert usdt.snapshot().consecutive_count == 2
    assert usdt.snapshot().reason == "profitability_threshold"
    assert btc.snapshot().consecutive_count == 0

    decision = usdt.record_quote(3, T0 + timedelta(seconds=20))
    assert decision.kind is None
    assert usdt.snapshot().consecutive_count == 0


def test_round_threshold_triggers_once_and_cooldown_allows_reminder(tmp_path):
    monitor = make_monitor(tmp_path)
    assert monitor.record_no_quote(1, T0, "profitability_threshold", DIAGNOSTICS).kind is None
    assert monitor.record_no_quote(2, T0 + timedelta(seconds=10), "profitability_threshold", DIAGNOSTICS).kind is None

    alert = monitor.record_no_quote(3, T0 + timedelta(seconds=20), "profitability_threshold", DIAGNOSTICS)
    assert alert.kind == "alert"
    monitor.mark_delivered(alert, message_id=101)

    assert monitor.record_no_quote(4, T0 + timedelta(seconds=30), "profitability_threshold", DIAGNOSTICS).kind is None
    reminder = monitor.record_no_quote(5, T0 + timedelta(seconds=141), "profitability_threshold", DIAGNOSTICS)
    assert reminder.kind == "reminder"


def test_duration_threshold_and_reason_change_trigger(tmp_path):
    monitor = make_monitor(tmp_path, alert_rounds=99, alert_seconds=30)
    monitor.record_no_quote(1, T0, "profitability_threshold", DIAGNOSTICS)
    alert = monitor.record_no_quote(2, T0 + timedelta(seconds=31), "profitability_threshold", DIAGNOSTICS)
    assert alert.kind == "alert"
    monitor.mark_delivered(alert, 1)

    changed = monitor.record_no_quote(
        3,
        T0 + timedelta(seconds=35),
        "invalid_or_single_level_order_book",
        {**DIAGNOSTICS, "reason": "invalid_or_single_level_order_book"},
    )
    assert changed.kind == "state_change"


def test_failed_delivery_remains_pending_across_restart(tmp_path):
    monitor = make_monitor(tmp_path, alert_rounds=1)
    alert = monitor.record_no_quote(1, T0, "profitability_threshold", DIAGNOSTICS)
    assert alert.kind == "alert"

    reloaded = make_monitor(tmp_path, alert_rounds=1)
    pending = reloaded.pending_decision()
    assert pending.kind == "alert"
    assert pending.snapshot.consecutive_count == 1

    reloaded.mark_delivered(pending, 202)
    assert reloaded.pending_decision().kind is None
    assert reloaded.snapshot().last_message_id == 202


def test_alerted_episode_emits_one_recovery_then_resets(tmp_path):
    monitor = make_monitor(tmp_path, alert_rounds=1)
    alert = monitor.record_no_quote(1, T0, "profitability_threshold", DIAGNOSTICS)
    monitor.mark_delivered(alert, 7)

    recovery = monitor.record_quote(2, T0 + timedelta(seconds=40))

    assert recovery.kind == "recovered"
    assert recovery.payload["episode_count"] == 1
    assert recovery.payload["episode_seconds"] == 40
    assert monitor.snapshot().consecutive_count == 0
    monitor.mark_delivered(recovery, 8)
    assert monitor.record_quote(3, T0 + timedelta(seconds=50)).kind is None


def test_diagnostics_are_persisted_exactly(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.record_no_quote(1, T0, "profitability_threshold", DIAGNOSTICS)

    snapshot = monitor.snapshot()
    assert snapshot.diagnostics == DIAGNOSTICS
    assert snapshot.diagnostics["commission"] == 0.001
    assert snapshot.diagnostics["exp_ret_min"] == 0.001
    assert snapshot.diagnostics["required_threshold"] == 0.002
    assert snapshot.diagnostics["candidates"][0]["spread"] == 0.001
