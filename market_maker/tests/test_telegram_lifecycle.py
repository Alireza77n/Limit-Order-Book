from datetime import datetime, timezone

import pytest

from paper import PaperLedger
from no_quote import NoQuoteMonitor
from result_store import DemoResultStore
from telegram_lifecycle import PaperTelegramReporter, TelegramClient


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _telegram_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LOB_TELEGRAM_DISABLED", raising=False)


class _Response:
    def __init__(self, status_code=200, body=None, json_error=None):
        self.status_code = status_code
        self._body = body
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._body


class _Post:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _ledger(tmp_path):
    return PaperLedger(
        session_id="shared-session",
        symbol="USDTIRT",
        initial_inventory=10,
        initial_cash=1_000,
        commission_perc=0.001,
        valuation_price=100,
        state_path=tmp_path / "paper.json",
    )


def _order(ledger):
    return ledger.submit_order(
        side="buy",
        limit_price=100,
        amount=1,
        selected_level=2,
        client_order_id="buy-1",
        now=NOW,
    )


@pytest.mark.parametrize(
    "response",
    [
        _Response(500, {"ok": True, "result": {"message_id": 1}}),
        _Response(200, {"ok": False, "description": "rejected"}),
        _Response(200, {"status": "success"}),
        _Response(200, json_error=ValueError("bad json")),
        TimeoutError("timeout"),
    ],
)
def test_telegram_requires_http_success_ok_true_and_message_id(response):
    post = _Post([response])
    client = TelegramClient("token", "chat", post=post)

    assert client.send("hello") is None


def test_telegram_success_returns_message_id_and_sets_reply_parameter():
    post = _Post([_Response(200, {"ok": True, "result": {"message_id": 42}})])
    client = TelegramClient("token", "chat", post=post)

    assert client.send("fill", reply_to_message_id=7) == 42
    assert post.calls[0][1]["reply_to_message_id"] == 7


def test_telegram_disabled_kill_switch_suppresses_transport(monkeypatch):
    monkeypatch.setenv("LOB_TELEGRAM_DISABLED", "1")
    post = _Post([_Response(200, {"ok": True, "result": {"message_id": 42}})])
    client = TelegramClient("token", "chat", post=post)

    assert client.send("hello") is None
    assert post.calls == []


def test_submission_message_is_sent_once_and_id_is_persisted(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([_Response(200, {"ok": True, "result": {"message_id": 101}})])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))

    ledger.record_telegram_round_parent(order.client_order_id, message_id=77)
    assert reporter.order_submitted(ledger, order, reply_to_message_id=77) == 101
    assert reporter.order_submitted(ledger, order) == 101

    restored = PaperLedger.load(tmp_path / "paper.json").get_order("buy-1")
    assert restored.telegram_submission_message_id == 101
    assert restored.telegram_round_message_id == 77
    assert restored.telegram_submission_attempted is True
    assert len(post.calls) == 1
    assert post.calls[0][1]["reply_to_message_id"] == 77
    assert "BUY OPENED" in post.calls[0][1]["text"]
    assert "shared-session" in post.calls[0][1]["text"]


def test_partial_and_final_fill_reply_once_to_round_parent(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([
        _Response(200, {"ok": True, "result": {"message_id": 101}}),
        _Response(200, {"ok": True, "result": {"message_id": 102}}),
        _Response(200, {"ok": True, "result": {"message_id": 103}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))
    reporter.order_submitted(ledger, order, reply_to_message_id=77)

    partial = ledger.apply_cumulative_fill(
        "buy-1", cumulative_matched=0.4, fill_price=99, reason="trade_cross", now=NOW
    )
    reporter.order_changed(ledger, partial)
    reporter.order_changed(ledger, partial)
    final = ledger.apply_cumulative_fill(
        "buy-1", cumulative_matched=1, fill_price=100, reason="price_touch", now=NOW
    )
    reporter.order_changed(ledger, final)
    reporter.order_changed(ledger, final)

    assert len(post.calls) == 3
    assert post.calls[1][1]["reply_to_message_id"] == 77
    assert post.calls[2][1]["reply_to_message_id"] == 77
    assert "PARTIALLY FILLED" in post.calls[1][1]["text"]
    assert "BUY FILLED" in post.calls[2][1]["text"]
    assert ledger.get_order("buy-1").telegram_last_reported_matched == pytest.approx(1)


def test_buy_fill_reports_total_fee_in_quote_currency(tmp_path):
    """A buy's exchange fee is charged in BASE units (Nobitex convention,
    mirrored by PaperOrder.total_fee). Reporting that number under the quote
    label printed every real buy fee as '0.00 IRT'."""
    ledger = PaperLedger(
        session_id="shared-session",
        symbol="USDTIRT",
        initial_inventory=0,
        initial_cash=2_500_000,
        commission_perc=0.0015,
        valuation_price=1_856_040,
        state_path=tmp_path / "paper.json",
    )
    order = ledger.submit_order(
        side="buy", limit_price=1_856_040, amount=0.6,
        selected_level=1, client_order_id="buy-1", now=NOW,
    )
    post = _Post([
        _Response(200, {"ok": True, "result": {"message_id": 201}}),
        _Response(200, {"ok": True, "result": {"message_id": 202}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))
    reporter.order_submitted(ledger, order, reply_to_message_id=7)
    filled = ledger.apply_cumulative_fill(
        "buy-1", cumulative_matched=0.6, fill_price=1_856_040,
        reason="level1_fast_fill", now=NOW,
    )
    reporter.order_changed(ledger, filled)

    # Fee is 0.0009 USDT == 1,670.44 IRT. The base figure must never be the
    # one printed next to the quote symbol.
    assert order.total_fee == pytest.approx(0.0009)
    assert order.total_fee_quote == pytest.approx(1670.436)
    assert "Total fee: 1,670.44 IRT" in post.calls[1][1]["text"]
    assert "Total fee: 0.00 IRT" not in post.calls[1][1]["text"]


def test_cancellation_replies_once_and_preserves_matched_amount(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([
        _Response(200, {"ok": True, "result": {"message_id": 101}}),
        _Response(200, {"ok": True, "result": {"message_id": 102}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))
    reporter.order_submitted(ledger, order, reply_to_message_id=77)
    ledger.apply_cumulative_fill(
        "buy-1", cumulative_matched=0.4, fill_price=100, reason="trade_cross", now=NOW
    )
    event = ledger.cancel_order("buy-1", reason="stale_timeout", now=NOW)

    reporter.order_changed(ledger, event)
    reporter.order_changed(ledger, event)

    assert len(post.calls) == 2
    assert post.calls[1][1]["reply_to_message_id"] == 77
    assert "Matched before cancellation: 0.40000000" in post.calls[1][1]["text"]
    assert "Released cash: 60.00 IRT" in post.calls[1][1]["text"]
    assert "Reason: <code>stale_timeout</code>" in post.calls[1][1]["text"]
    assert ledger.get_order("buy-1").telegram_terminal_reported is True


def test_failure_uses_unthreaded_fallback_and_never_changes_accounting(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([
        _Response(200, {"ok": False}),
        _Response(200, {"ok": True, "result": {"message_id": 202}}),
        _Response(200, {"ok": False}),
        _Response(200, {"ok": True, "result": {"message_id": 203}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))

    reporter.order_submitted(ledger, order)
    reporter.order_submitted(ledger, order)
    event = ledger.apply_cumulative_fill(
        "buy-1", cumulative_matched=1, fill_price=100, reason="price_touch", now=NOW
    )
    before = ledger.snapshot()
    reporter.order_changed(ledger, event)
    reporter.order_changed(ledger, event)
    after = ledger.snapshot()

    assert ledger.get_order("buy-1").telegram_submission_message_id == 202
    assert post.calls[2][1]["reply_to_message_id"] == 202
    assert len(post.calls) == 4
    assert after == before
    assert ledger.get_order("buy-1").telegram_last_reported_matched == pytest.approx(1)
    assert ledger.get_order("buy-1").telegram_pending_event is None


def test_no_quote_alert_and_recovery_include_operational_evidence(tmp_path):
    store = DemoResultStore(tmp_path / "results.sqlite3", ["Session_id", "Crypto", "Round"])
    monitor = NoQuoteMonitor(
        store,
        "shared-session",
        "USDTIRT",
        alert_rounds=1,
        alert_seconds=300,
        cooldown_seconds=1800,
    )
    diagnostics = {
        "commission": 0.001,
        "exp_ret_min": 0.001,
        "required_threshold": 0.002,
        "candidates": [{"bid_price": 100, "ask_price": 100.1, "spread": 0.001}],
    }
    alert = monitor.record_no_quote(1, NOW, "profitability_threshold", diagnostics)
    post = _Post([
        _Response(200, {"ok": True, "result": {"message_id": 301}}),
        _Response(200, {"ok": True, "result": {"message_id": 302}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))

    assert reporter.no_quote_alert(alert) == 301
    monitor.mark_delivered(alert, 301)
    recovery = monitor.record_quote(2, NOW.replace(minute=1))
    assert reporter.no_quote_recovered(recovery) == 302

    alert_text = post.calls[0][1]["text"]
    assert "USDTIRT" in alert_text
    assert "profitability_threshold" in alert_text
    assert "Commission: 0.001" in alert_text
    assert "Minimum expected return: 0.001" in alert_text
    assert "Required threshold: 0.002" in alert_text
    assert "bid=100" in alert_text
    assert "ask=100.1" in alert_text
    assert "spread=0.001" in alert_text
    assert "NO-QUOTE RECOVERED" in post.calls[1][1]["text"]
