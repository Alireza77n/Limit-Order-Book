import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz

from exchange.base import ExchangeClient
from exchange.nobitex import NobitexExchangeClient


class DemoExchangeClient(ExchangeClient):
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

    def __init__(self, market_data: NobitexExchangeClient, pair: str, commission_perc: float = 0.0):
        self._market_data = market_data
        self._pair = pair.upper()
        self._commission_perc = commission_perc
        self._orders: Dict[str, Dict[str, Any]] = {}

    # ---------- Real market data (read-only, no risk) ----------
    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._market_data.get_orderbook(symbol)

    def wallet_balance(self, currency: str) -> Optional[float]:
        return self._market_data.wallet_balance(currency)

    # ---------- Simulated orders (nothing ever sent to Nobitex) ----------
    def send_order(self, order_type: str, srcCurrency: str, dstCurrency: str,
                    amount: float, price: float,
                    clientOrderId: Optional[str] = None) -> Optional[str]:
        if clientOrderId is None:
            clientOrderId = f"demo-{order_type}-{int(time.time()*1000)}"

        # Simulated commission, in the same units the engine expects a real
        # exchange's fee to arrive in: base-asset amount for buys, quote
        # currency (matches send price) for sells.
        fee = amount * self._commission_perc if order_type == "buy" \
            else amount * price * self._commission_perc

        self._orders[clientOrderId] = {
            "id": clientOrderId,
            "clientOrderId": clientOrderId,
            "type": order_type,
            "status": "done",  # simulated: fills instantly and in full
            "matchedAmount": float(amount),
            "unmatchedAmount": 0.0,
            "amount": float(amount),
            "price": float(price),
            "created_at": datetime.now(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.%f%z"),
            "pair": self._pair,
            "_raw": {"fee": fee},
        }
        return clientOrderId

    def get_order_list_status(self, status: str = "open", details: int = 2,
                               page_size: int = 100, max_pages: int = 1) -> List[Dict]:
        if status == "open":
            return []  # nothing ever rests open -- every simulated order fills instantly
        return [o for o in self._orders.values() if o["status"] in ("done", "close")]

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        return {"status": "ok", "order": {"clientOrderId": order_id}}

    def cancel_all_open_orders(self, max_pages: int = 10) -> None:
        return None  # nothing ever open in this model

    def cancel_old_orders(self, max_age_minutes: float, df=None, tracked_orders=None):
        return tracked_orders  # nothing ever open/stale to cancel
