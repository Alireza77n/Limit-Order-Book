from typing import Callable, Optional

import requests

from paper import PaperEvent, PaperLedger, PaperOrder


class TelegramClient:
    """Small fail-safe Telegram transport with strict API success parsing."""

    def __init__(
        self,
        token: Optional[str],
        chat_id: Optional[str],
        *,
        post: Optional[Callable] = None,
        timeout: float = 10,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.post = post or requests.post
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, *, reply_to_message_id: Optional[int] = None) -> Optional[int]:
        if not self.token or not self.chat_id:
            return None
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        try:
            response = self.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=self.timeout,
            )
            if not 200 <= int(response.status_code) < 300:
                return None
            document = response.json()
            if not isinstance(document, dict) or document.get("ok") is not True:
                return None
            message_id = document.get("result", {}).get("message_id")
            return int(message_id) if message_id is not None else None
        except (Exception,):
            return None


class PaperTelegramReporter:
    """Publishes each persisted paper-order transition at most once."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    @staticmethod
    def _submission_text(order: PaperOrder) -> str:
        return (
            f"🟢 DEMO {order.side.upper()} ORDER OPENED\n\n"
            f"Symbol: {order.symbol}\n"
            f"Session: {order.session_id}\n"
            f"Round: {order.round_no}\n"
            f"Order ID: {order.client_order_id}\n"
            f"Level: {order.selected_level}\n"
            f"Limit price: {order.limit_price:,.8f}\n"
            f"Original amount: {order.original_amount:.8f}\n"
            "Matched: 0.00000000\n"
            f"Remaining: {order.remaining_amount:.8f}\n"
            "Status: OPEN\n"
            "Fill model: price/trade/queue simulation"
        )

    @staticmethod
    def _event_text(order: PaperOrder, event: PaperEvent) -> str:
        if event.status == "cancelled":
            return (
                "⚪ ORDER CANCELLED\n\n"
                f"Order ID: {order.client_order_id}\n"
                f"Matched before cancellation: {event.cumulative_matched:.8f}\n"
                f"Cancelled remainder: {event.cancelled_remainder:.8f}\n"
                f"Released locked amount: {event.released_locked_amount:.8f}\n"
                f"Reason: {event.reason}\n"
                "Status: CANCELLED"
            )
        if event.status == "partially_filled":
            return (
                "🟡 ORDER PARTIALLY FILLED\n\n"
                f"Order ID: {order.client_order_id}\n"
                f"Fill reason: {event.reason}\n"
                f"New fill: {event.new_fill:.8f}\n"
                f"Cumulative matched: {event.cumulative_matched:.8f}\n"
                f"Remaining: {event.remaining:.8f}\n"
                f"Fill price: {event.fill_price:,.8f}\n"
                f"Fee on this fill: {event.fee_quote:,.8f}\n"
                f"Inventory after fill: {event.inventory_after:.8f}\n"
                f"Cash after fill: {event.cash_after:,.8f}\n"
                "Status: PARTIALLY_FILLED"
            )
        return (
            "✅ ORDER FULLY FILLED\n\n"
            f"Order ID: {order.client_order_id}\n"
            f"Fill reason: {event.reason}\n"
            f"New fill: {event.new_fill:.8f}\n"
            f"Total matched: {event.cumulative_matched:.8f}\n"
            "Remaining: 0.00000000\n"
            f"Average fill price: {order.average_fill_price:,.8f}\n"
            f"Total order fee: {order.total_fee:,.8f}\n"
            f"Inventory after fill: {event.inventory_after:.8f}\n"
            f"Cash after fill: {event.cash_after:,.8f}\n"
            f"Wealth after fill: {event.wealth_after:,.8f}\n"
            "Status: FILLED"
        )

    def order_submitted(self, ledger: PaperLedger, order: PaperOrder) -> Optional[int]:
        if order.telegram_submission_message_id is not None:
            return order.telegram_submission_message_id
        if not self.client.configured:
            return None
        ledger.record_telegram_submission(order.client_order_id)
        message_id = self.client.send(self._submission_text(order))
        if message_id is not None:
            ledger.record_telegram_submission(order.client_order_id, message_id=message_id)
        return message_id

    def order_changed(self, ledger: PaperLedger, event: Optional[PaperEvent]) -> Optional[int]:
        if event is None:
            return None
        order = ledger.get_order(event.order_id)
        if event.status in {"partially_filled", "filled"}:
            if event.cumulative_matched <= order.telegram_last_reported_matched:
                return None
        elif event.status == "cancelled":
            if order.telegram_terminal_reported:
                return None
        else:
            return None
        if not self.client.configured:
            return None
        ledger.record_telegram_pending(event)
        message_id = self.client.send(
            self._event_text(order, event),
            reply_to_message_id=order.telegram_submission_message_id,
        )
        if message_id is not None:
            ledger.record_telegram_event(event)
        return message_id

    def retry_pending(self, ledger: PaperLedger) -> None:
        if not self.client.configured:
            return
        for order in list(ledger.order_history.values()):
            if order.telegram_submission_attempted and order.telegram_submission_message_id is None:
                self.order_submitted(ledger, order)
            if order.telegram_pending_event:
                self.order_changed(ledger, PaperEvent(**order.telegram_pending_event))

    @staticmethod
    def _candidate_lines(diagnostics) -> str:
        candidates = diagnostics.get("candidates") or []
        if not candidates:
            return "Candidates: none"
        lines = []
        for index, candidate in enumerate(candidates, start=1):
            lines.append(
                f"Candidate {index}: "
                f"bid={candidate.get('bid_price')} "
                f"ask={candidate.get('ask_price')} "
                f"spread={candidate.get('spread')}"
            )
        return "\n".join(lines)

    @classmethod
    def _no_quote_alert_text(cls, decision) -> str:
        diagnostics = decision.payload.get("diagnostics") or decision.snapshot.diagnostics or {}
        return (
            "🚨 DEMO NO-QUOTE ALERT\n\n"
            f"Symbol: {decision.snapshot.symbol}\n"
            f"Session: {decision.snapshot.session_id}\n"
            f"Alert type: {decision.kind}\n"
            f"Consecutive rounds: {decision.payload.get('count', decision.snapshot.consecutive_count)}\n"
            f"Episode seconds: {decision.payload.get('episode_seconds', decision.snapshot.episode_seconds)}\n"
            f"Reason: {decision.payload.get('reason', decision.snapshot.reason)}\n"
            f"Commission: {diagnostics.get('commission')}\n"
            f"Minimum expected return: {diagnostics.get('exp_ret_min')}\n"
            f"Required threshold: {diagnostics.get('required_threshold')}\n"
            f"{cls._candidate_lines(diagnostics)}"
        )

    @staticmethod
    def _no_quote_recovery_text(decision) -> str:
        return (
            "✅ DEMO NO-QUOTE RECOVERED\n\n"
            f"Symbol: {decision.snapshot.symbol}\n"
            f"Session: {decision.snapshot.session_id}\n"
            f"Episode rounds: {decision.payload.get('episode_count', 0)}\n"
            f"Episode seconds: {decision.payload.get('episode_seconds', 0)}\n"
            f"Previous reason: {decision.payload.get('reason')}\n"
            "Status: QUOTE GENERATED"
        )

    def no_quote_alert(self, decision) -> Optional[int]:
        if decision is None or not decision.kind or decision.kind == "recovered":
            return None
        if not self.client.configured:
            return None
        return self.client.send(self._no_quote_alert_text(decision))

    def no_quote_recovered(self, decision) -> Optional[int]:
        if decision is None or decision.kind != "recovered":
            return None
        if not self.client.configured:
            return None
        return self.client.send(
            self._no_quote_recovery_text(decision),
            reply_to_message_id=decision.snapshot.last_message_id,
        )
