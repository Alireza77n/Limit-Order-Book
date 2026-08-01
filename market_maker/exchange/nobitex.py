import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests

from order_status import OrderStatusChecker, OrderCanceller
from safety import DemoSafetyError
from exchange.base import ExchangeClient


class NobitexExchangeClient(ExchangeClient):
    """Nobitex implementation of ExchangeClient.

    Moved verbatim out of SimpleMarketMaker (_get_orderbook, wallet_balance,
    and the HTTP part of send_order) and composes the existing, untouched
    OrderStatusChecker/OrderCanceller from Order_Statuse.py. Same URLs, same
    requests calls, same error handling -- only the location changed.
    """

    def __init__(self, token: Optional[str], url_send_order: Optional[str],
                 url_wallet_balance: Optional[str], algorithm_id: str = "default",
                 trading_enabled: bool = True):
        self.token = token
        self.url_send_order = url_send_order
        self.url_wallet_balance = url_wallet_balance
        self.algorithm_id = algorithm_id
        self.trading_enabled = trading_enabled
        self._status_checker = OrderStatusChecker(token)
        self._canceller = OrderCanceller(token)

    def _require_trading_enabled(self) -> None:
        if not self.trading_enabled:
            raise DemoSafetyError("Demo Mode blocks real Nobitex order and cancellation methods")

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
        self._require_trading_enabled()
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
            if not 200 <= int(r.status_code) < 300:
                return None
            response = r.json()
            if not isinstance(response, dict) or response.get("status") != "ok":
                return None
            order = response.get("order")
            if not isinstance(order, dict):
                return None
            confirmed_client_id = order.get("clientOrderId")
            if str(confirmed_client_id or "") != str(clientOrderId):
                return None
            return str(confirmed_client_id)

        except Exception as e:
            print(f"Send order error: {e}")
            return None

    def get_order_list_status(self, status: str = "open", details: int = 2,
                               page_size: int = 100, max_pages: int = 1) -> List[Dict]:
        return self._status_checker.get_order_list_status(
            status=status, details=details, page_size=page_size, max_pages=max_pages
        )

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        self._require_trading_enabled()
        return self._canceller.cancel_order(order_id)

    def cancel_all_open_orders(
        self,
        max_pages: int = 10,
        *,
        pair: Optional[str] = None,
        owned_client_order_ids=None,
    ) -> Set[str]:
        self._require_trading_enabled()
        owned = {str(value) for value in (owned_client_order_ids or set())}
        if not pair or not owned:
            return set()
        want_pair = str(pair).upper()
        orders = self._status_checker.get_order_list_status(
            status="open", details=2, page_size=100, max_pages=max_pages
        )
        cancelled = set()
        for order in orders:
            client_id = str(order.get("clientOrderId") or "")
            if client_id not in owned or str(order.get("pair") or "").upper() != want_pair:
                continue
            order_id = order.get("id")
            if order_id is None:
                continue
            response = self._canceller.cancel_order(order_id)
            if isinstance(response, dict) and response.get("status") in {"ok", "success"}:
                cancelled.add(client_id)
        return cancelled

    def cancel_old_orders(
        self,
        max_age_minutes: float,
        df=None,
        tracked_orders=None,
        *,
        pair: Optional[str] = None,
        owned_client_order_ids=None,
        now: Optional[datetime] = None,
        max_pages: int = 10,
    ) -> Set[str]:
        self._require_trading_enabled()
        owned = {str(value) for value in (owned_client_order_ids or set())}
        if not pair or not owned:
            return set()
        want_pair = str(pair).upper()
        current_time = now or datetime.now(timezone.utc)
        orders = self._status_checker.get_order_list_status(
            status="open", details=2, page_size=100, max_pages=max_pages
        )
        cancelled = set()
        for order in orders:
            client_id = str(order.get("clientOrderId") or "")
            if client_id not in owned or str(order.get("pair") or "").upper() != want_pair:
                continue
            try:
                created_text = str(order.get("created_at"))
                try:
                    created_at = datetime.fromisoformat(created_text)
                except ValueError:
                    created_at = datetime.strptime(created_text, "%Y-%m-%dT%H:%M:%S.%f%z")
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if (current_time - created_at).total_seconds() <= max_age_minutes * 60:
                continue
            order_id = order.get("id")
            if order_id is None:
                continue
            response = self._canceller.cancel_order(order_id)
            if isinstance(response, dict) and response.get("status") in {"ok", "success"}:
                cancelled.add(client_id)
        if df is not None:
            for client_id in cancelled:
                for column in ("Buy Status", "Sell Status"):
                    if column in df.columns:
                        df.loc[df[column] == client_id, column] = "cancelled"
        if tracked_orders is not None:
            tracked_orders["buy"] -= cancelled
            tracked_orders["sell"] -= cancelled
        return cancelled
