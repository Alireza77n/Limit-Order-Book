from html import escape
import os
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
        return not self.disabled and bool(self.token and self.chat_id)

    @property
    def disabled(self) -> bool:
        return os.environ.get("LOB_TELEGRAM_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}

    def send(self, text: str, *, reply_to_message_id: Optional[int] = None) -> Optional[int]:
        if self.disabled or not self.token or not self.chat_id:
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

    def round_summary(self, text: str) -> Optional[int]:
        return self.client.send(text)

    @staticmethod
    def _currencies(symbol: str) -> tuple[str, str]:
        normalized = str(symbol).upper()
        for quote in ("USDT", "IRT", "USD", "EUR", "BTC", "ETH"):
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return normalized[:-len(quote)], quote
        return normalized, "QUOTE"

    @staticmethod
    def _submission_text(order: PaperOrder) -> str:
        base, quote = PaperTelegramReporter._currencies(order.symbol)
        side = order.side.upper()
        icon = "🟢" if order.side == "buy" else "🔴"
        locked_label = "Cash locked" if order.side == "buy" else "Inventory locked"
        locked_value = (
            order.remaining_amount * order.limit_price
            if order.side == "buy" else order.remaining_amount
        )
        locked_unit = quote if order.side == "buy" else base
        return (
            f"{icon} <b>{side} OPENED</b> · DEMO\n"
            f"<b>{escape(order.symbol)}</b> · Round {order.round_no}\n\n"
            f"Session: <code>{escape(order.session_id)}</code>\n"
            f"Order: <code>{escape(order.client_order_id)}</code>\n"
            f"Amount: {order.original_amount:.8f} {base}\n"
            f"Limit: {order.limit_price:,.2f} {quote} · Level "
            f"{'synthetic' if order.selected_level is None else order.selected_level}\n"
            f"Remaining: {order.remaining_amount:.8f} {base}\n"
            f"{locked_label}: {locked_value:,.2f} {locked_unit}\n"
            "Status: <b>OPEN</b>"
        )

    @staticmethod
    def _event_text(order: PaperOrder, event: PaperEvent) -> str:
        base, quote = PaperTelegramReporter._currencies(order.symbol)
        side = order.side.upper()
        if event.status == "cancelled":
            released_kind = event.released_locked_kind or (
                "cash" if order.side == "buy" else "inventory"
            )
            released_unit = event.released_locked_unit or (
                quote if order.side == "buy" else base
            )
            return (
                f"⚪ <b>{side} CANCELLED</b> · DEMO\n"
                f"<b>{escape(order.symbol)}</b>\n\n"
                f"Order: <code>{escape(order.client_order_id)}</code>\n"
                f"Matched before cancellation: {event.cumulative_matched:.8f}\n"
                f"Cancelled remainder: {event.cancelled_remainder:.8f}\n"
                f"Released {released_kind}: "
                f"{event.released_locked_amount:,.2f} {released_unit}\n"
                f"Reason: <code>{escape(event.reason)}</code>\n"
                "Status: <b>CANCELLED</b>"
            )
        if event.status == "partially_filled":
            return (
                f"🟡 <b>{side} PARTIALLY FILLED</b> · DEMO\n"
                f"<b>{escape(order.symbol)}</b>\n\n"
                f"Order: <code>{escape(order.client_order_id)}</code>\n"
                f"New fill: {event.new_fill:.8f} {base}\n"
                f"Total matched: {event.cumulative_matched:.8f} {base}\n"
                f"Remaining: {event.remaining:.8f} {base}\n"
                f"Fill price: {event.fill_price:,.2f} {quote}\n"
                f"Fee: {event.fee_quote:,.2f} {quote}\n"
                f"Reason: <code>{escape(event.reason)}</code>\n"
                "Status: <b>PARTIALLY FILLED</b>"
            )
        return (
            f"✅ <b>{side} FILLED</b> · DEMO\n"
            f"<b>{escape(order.symbol)}</b>\n\n"
            f"Order: <code>{escape(order.client_order_id)}</code>\n"
            f"New fill: {event.new_fill:.8f} {base}\n"
            f"Total matched: {event.cumulative_matched:.8f} {base}\n"
            f"Average fill: {order.average_fill_price:,.2f} {quote}\n"
            f"Total fee: {order.total_fee_quote:,.2f} {quote}\n"
            f"Inventory: {event.inventory_after:.8f} {base}\n"
            f"Cash: {event.cash_after:,.2f} {quote}\n"
            f"Wealth: {event.wealth_after:,.2f} {quote}\n"
            f"Reason: <code>{escape(event.reason)}</code>\n"
            "Status: <b>FILLED</b>"
        )

    def order_submitted(
        self,
        ledger: PaperLedger,
        order: PaperOrder,
        *,
        reply_to_message_id: Optional[int] = None,
    ) -> Optional[int]:
        if reply_to_message_id is not None and order.telegram_round_message_id is None:
            ledger.record_telegram_round_parent(
                order.client_order_id, message_id=reply_to_message_id
            )
        if order.telegram_submission_message_id is not None:
            return order.telegram_submission_message_id
        if not self.client.configured:
            return None
        ledger.record_telegram_submission(order.client_order_id)
        message_id = self.client.send(
            self._submission_text(order),
            reply_to_message_id=order.telegram_round_message_id,
        )
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
            reply_to_message_id=(
                order.telegram_round_message_id
                or order.telegram_submission_message_id
            ),
        )
        if message_id is not None:
            ledger.record_telegram_event(event)
        return message_id

    def retry_pending(self, ledger: PaperLedger) -> None:
        if not self.client.configured:
            return
        for order in list(ledger.order_history.values()):
            if order.telegram_submission_attempted and order.telegram_submission_message_id is None:
                self.order_submitted(
                    ledger,
                    order,
                    reply_to_message_id=order.telegram_round_message_id,
                )
            if order.telegram_pending_event:
                self.order_changed(ledger, PaperEvent(**order.telegram_pending_event))

    @staticmethod
    def _candidate_lines(diagnostics) -> str:
        candidates = diagnostics.get("candidates") or []
        if not candidates:
            return "Candidates: none"
        lines = []
        for index, candidate in enumerate(candidates, start=1):
            line = (
                f"Candidate {index}: "
                f"bid={candidate.get('bid_price')} "
                f"ask={candidate.get('ask_price')} "
                f"spread={candidate.get('spread')}"
            )
            net_return = candidate.get('net_return')
            if net_return is not None:
                line += f" net={net_return}"
            lines.append(line)
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
