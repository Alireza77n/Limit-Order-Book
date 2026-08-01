import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from exchange.base import ExchangeClient
from exchange.nobitex import NobitexExchangeClient
from paper import PaperLedger


class DemoExchangeClient(ExchangeClient):
    is_demo = True

    """Paper-trading exchange: real market data, simulated fills, no real
    orders ever sent.

    get_orderbook/wallet_balance delegate to a real NobitexExchangeClient,
    so quotes are computed against real, live market data -- nothing about
    Algorithm 1's decision-making changes. send_order never calls Nobitex's
    order endpoints at all: it records a simulated order locally and
    returns a clientOrderId exactly like a real send would, so the engine
    (get_status_orders / update_filled_amounts / update_inventory_and_cash)
    works completely unchanged.

    ponytail: fills assume the full requested amount executes instantly at
    the quoted price -- no partial fills, no slippage, no queue/latency
    modeling. Upgrade path: model fill probability/delay from spread_ok's
    rejected pairs or real trade-arrival data before treating demo PnL as
    a return estimate.
    """

    def __init__(self, market_data: NobitexExchangeClient, pair: str,
                 commission_perc: float = 0.0, ledger: Optional[PaperLedger] = None):
        self._market_data = market_data
        self._pair = pair.upper()
        self._commission_perc = commission_perc
        self.ledger = ledger or PaperLedger(
            session_id="standalone-demo",
            symbol=self._pair,
            initial_inventory=0.0,
            initial_cash=0.0,
            commission_perc=commission_perc,
            valuation_price=0.0,
        )

    # ---------- Real market data (read-only, no risk) ----------
    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._market_data.get_orderbook(symbol)

    def wallet_balance(self, currency: str) -> Optional[float]:
        return None

    # ---------- Simulated orders (nothing ever sent to Nobitex) ----------
    def send_order(self, order_type: str, srcCurrency: str, dstCurrency: str,
                    amount: float, price: float,
                    clientOrderId: Optional[str] = None, *, selected_level: int = 0,
                    queue_ahead: float = 0.0, round_no: int = 0,
                    now: Optional[datetime] = None) -> Optional[str]:
        if clientOrderId is None:
            clientOrderId = f"demo-{order_type}-{int(time.time()*1000)}"
        order = self.ledger.submit_order(
            side=order_type,
            limit_price=price,
            amount=amount,
            selected_level=selected_level,
            client_order_id=clientOrderId,
            queue_ahead=queue_ahead,
            round_no=round_no,
            now=now or datetime.now(timezone.utc),
        )
        return clientOrderId if order.status != "rejected" else None

    @staticmethod
    def _as_exchange_order(order) -> Dict[str, Any]:
        return {
            "id": order.order_id,
            "clientOrderId": order.client_order_id,
            "type": order.side,
            "status": order.status,
            "matchedAmount": order.matched_amount,
            "unmatchedAmount": order.remaining_amount,
            "amount": order.original_amount,
            "price": order.limit_price,
            "created_at": order.created_at.strftime("%Y-%m-%dT%H:%M:%S.%f%z"),
            "pair": order.symbol,
            "selectedLevel": order.selected_level,
            "queueAhead": order.queue_ahead,
            "fillReason": order.fill_reason,
            "averageFillPrice": order.average_fill_price,
            "_raw": {"fee": order.total_fee},
        }

    def get_order_list_status(self, status: str = "open", details: int = 2,
                               page_size: int = 100, max_pages: int = 1) -> List[Dict]:
        if status == "open":
            orders = self.ledger.active_orders.values()
        else:
            orders = (
                order for order in self.ledger.order_history.values()
                if order.status in {"filled", "cancelled", "rejected"}
            )
        return [self._as_exchange_order(order) for order in orders]

    def process_market(self, **market):
        return self.ledger.process_market(**market)

    def cancel_order(self, order_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        event = self.ledger.cancel_order(
            str(order_id), reason="manual_cancel", now=now or datetime.now(timezone.utc)
        )
        return {"status": "ok", "order": {"clientOrderId": event.order_id}}

    def cancel_all_open_orders(self, max_pages: int = 10, *, now: Optional[datetime] = None):
        stamp = now or datetime.now(timezone.utc)
        events = []
        for order_id in list(self.ledger.active_orders):
            events.append(self.ledger.cancel_order(order_id, reason="stack_shutdown", now=stamp))
        return events

    def cancel_old_orders(self, max_age_minutes: float, df=None, tracked_orders=None,
                          *, now: Optional[datetime] = None):
        events = self.ledger.cancel_stale(
            max_age_seconds=max_age_minutes * 60,
            now=now or datetime.now(timezone.utc),
        )
        cancelled_ids = {event.order_id for event in events}
        if tracked_orders is not None:
            tracked_orders["buy"] -= cancelled_ids
            tracked_orders["sell"] -= cancelled_ids
        return tracked_orders
