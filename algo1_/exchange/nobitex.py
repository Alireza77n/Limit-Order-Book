import time
from typing import Any, Dict, List, Optional

import requests

from Order_Statuse import OrderStatusChecker, OrderCanceller
from exchange.base import ExchangeClient


class NobitexExchangeClient(ExchangeClient):
    """Nobitex implementation of ExchangeClient.

    Moved verbatim out of SimpleMarketMaker (_get_orderbook, wallet_balance,
    and the HTTP part of send_order) and composes the existing, untouched
    OrderStatusChecker/OrderCanceller from Order_Statuse.py. Same URLs, same
    requests calls, same error handling -- only the location changed.
    """

    def __init__(self, token: Optional[str], url_send_order: Optional[str],
                 url_wallet_balance: Optional[str], algorithm_id: str = "default"):
        self.token = token
        self.url_send_order = url_send_order
        self.url_wallet_balance = url_wallet_balance
        self.algorithm_id = algorithm_id
        self._status_checker = OrderStatusChecker(token)
        self._canceller = OrderCanceller(token)

    # ---------- Market data ----------
    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"https://apiv2.nobitex.ir/v3/orderbook/{symbol}"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code != 200:
                print(f"Orderbook HTTP {r.status_code}")
                return None
            ob = r.json()
            if not all(k in ob for k in ("bids", "asks", "lastTradePrice")):
                print("Orderbook payload missing keys.")
                return None
            return ob
        except Exception as e:
            print(f"Orderbook error: {e}")
            return None

    # ---------- Wallet ----------
    def wallet_balance(self, currency: str) -> Optional[float]:
        if not (self.token and self.url_wallet_balance):
            print("Wallet balance endpoints not configured.")
            return None
        try:
            headers = {"Authorization": f"Token {self.token}"}
            data = {"currency": currency}
            r = requests.post(self.url_wallet_balance, headers=headers, json=data, timeout=5)
            w = r.json()
            return float(w["balance"])
        except Exception as e:
            print(f"Wallet balance error: {e}")
            return None

    # ---------- Orders ----------
    def send_order(self, order_type: str, srcCurrency: str, dstCurrency: str,
                    amount: float, price: float,
                    clientOrderId: Optional[str] = None) -> Optional[str]:
        if not (self.token and self.url_send_order):
            print("Order send endpoints not configured.")
            return None
        try:
            if clientOrderId is None:
                clientOrderId = f"algo-{self.algorithm_id}-{order_type}-{int(time.time()*1000)}"

            headers = {"Authorization": f"Token {self.token}", "content-type": "application/json"}
            data = {
                "type": order_type,
                "srcCurrency": srcCurrency,
                "dstCurrency": dstCurrency,
                "amount": str(amount),
                "price": price,
                "clientOrderId": clientOrderId
            }
            r = requests.post(self.url_send_order, headers=headers, json=data, timeout=20)
            print(r.text)

            return clientOrderId

        except Exception as e:
            print(f"Send order error: {e}")
            return None

    def get_order_list_status(self, status: str = "open", details: int = 2,
                               page_size: int = 100, max_pages: int = 1) -> List[Dict]:
        return self._status_checker.get_order_list_status(
            status=status, details=details, page_size=page_size, max_pages=max_pages
        )

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        return self._canceller.cancel_order(order_id)

    def cancel_all_open_orders(self, max_pages: int = 10) -> None:
        return self._canceller.cancel_all_open_orders(max_pages=max_pages)

    def cancel_old_orders(self, max_age_minutes: float, df=None, tracked_orders=None):
        return self._canceller.cancel_old_orders(
            max_age_minutes=max_age_minutes, df=df, tracked_orders=tracked_orders
        )
