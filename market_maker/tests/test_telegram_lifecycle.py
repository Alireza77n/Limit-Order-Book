from datetime import datetime, timezone

import pytest

from paper import PaperLedger
from telegram_lifecycle import PaperTelegramReporter, TelegramClient


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


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


def test_submission_message_is_sent_once_and_id_is_persisted(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([_Response(200, {"ok": True, "result": {"message_id": 101}})])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))

    assert reporter.order_submitted(ledger, order) == 101
    assert reporter.order_submitted(ledger, order) == 101

    restored = PaperLedger.load(tmp_path / "paper.json").get_order("buy-1")
    assert restored.telegram_submission_message_id == 101
    assert restored.telegram_submission_attempted is True
    assert len(post.calls) == 1
    assert "DEMO BUY ORDER OPENED" in post.calls[0][1]["text"]
    assert "shared-session" in post.calls[0][1]["text"]


def test_partial_and_final_fill_reply_once_to_original_message(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([
        _Response(200, {"ok": True, "result": {"message_id": 101}}),
        _Response(200, {"ok": True, "result": {"message_id": 102}}),
        _Response(200, {"ok": True, "result": {"message_id": 103}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))
    reporter.order_submitted(ledger, order)

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
    assert post.calls[1][1]["reply_to_message_id"] == 101
    assert post.calls[2][1]["reply_to_message_id"] == 101
    assert "PARTIALLY FILLED" in post.calls[1][1]["text"]
    assert "FULLY FILLED" in post.calls[2][1]["text"]
    assert ledger.get_order("buy-1").telegram_last_reported_matched == pytest.approx(1)


def test_cancellation_replies_once_and_preserves_matched_amount(tmp_path):
    ledger = _ledger(tmp_path)
    order = _order(ledger)
    post = _Post([
        _Response(200, {"ok": True, "result": {"message_id": 101}}),
        _Response(200, {"ok": True, "result": {"message_id": 102}}),
    ])
    reporter = PaperTelegramReporter(TelegramClient("token", "chat", post=post))
    reporter.order_submitted(ledger, order)
    ledger.apply_cumulative_fill(
        "buy-1", cumulative_matched=0.4, fill_price=100, reason="trade_cross", now=NOW
    )
    event = ledger.cancel_order("buy-1", reason="stale_timeout", now=NOW)

    reporter.order_changed(ledger, event)
    reporter.order_changed(ledger, event)

    assert len(post.calls) == 2
    assert post.calls[1][1]["reply_to_message_id"] == 101
    assert "Matched before cancellation: 0.40000000" in post.calls[1][1]["text"]
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
