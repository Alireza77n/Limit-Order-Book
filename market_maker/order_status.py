from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import pandas as pd
import requests

from exchange.nobitex_auth import build_full_path, compact_json, signed_headers


class OrderStatusChecker:
    def __init__(
        self,
        api_key: Optional[str],
        secret_key: Optional[str],
        base_url: str = "https://apiv2.nobitex.ir",
        timeout: int = 20,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _credentials_ready(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def get_order_list_status(
        self,
        status: str = "open",
        details: int = 2,
        page_size: int = 100,
        max_pages: int = 1,
    ) -> List[Dict]:
        """Return normalized user orders.

        GET query parameters are included in the Nobitex signature exactly as
        transmitted. A fresh timestamp/signature is generated for each page.
        """
        if not self._credentials_ready():
            raise RuntimeError("order status credentials not configured")

        path = "/market/orders/list"
        params = {
            "status": status,
            "details": details,
            "page": 1,
            "pageSize": page_size,
        }
        out: List[Dict] = []

        for _ in range(max_pages):
            try:
                full_path = build_full_path(path, params)
                headers = signed_headers(
                    self.api_key, self.secret_key, "GET", full_path
                )
                r = requests.get(
                    f"{self.base_url}{full_path}",
                    headers=headers,
                    timeout=self.timeout,
                )
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or data.get("status") != "ok":
                    raise RuntimeError(f"API error: {data}")

                raw_orders = data.get("orders")
                if not isinstance(raw_orders, list):
                    raise RuntimeError("order status response has no orders list")
                for o in raw_orders:
                    oid = o.get("id")
                    st = o.get("status") or o.get("state")
                    c_at = o.get("created_at") or o.get("createdAt")
                    exec_ = o.get("execution") or o.get("executionType")
                    side = o.get("type") or o.get("side")
                    clid = (
                        o.get("clientOrderId")
                        or o.get("client_id")
                        or o.get("clientOrderID")
                    )
                    price = o.get("price")
                    amt = o.get("amount")
                    filled = o.get("matchedAmount") or o.get("filledAmount")
                    remain = o.get("unmatchedAmount") or o.get("remainingAmount")
                    mkt = o.get("market") or o.get("pair")

                    def _f(value):
                        try:
                            return float(value)
                        except Exception:
                            return None

                    pair_norm = None
                    if mkt:
                        market_text = str(mkt).upper().replace("-", "")
                        pair_norm = market_text.replace("RLS", "IRT").replace(
                            "IRR", "IRT"
                        )

                    out.append(
                        {
                            "id": oid,
                            "status": st,
                            "created_at": c_at,
                            "execution": exec_,
                            "type": str(side).lower() if side else None,
                            "clientOrderId": clid,
                            "price": _f(price),
                            "amount": _f(amt),
                            "matchedAmount": _f(filled),
                            "unmatchedAmount": _f(remain),
                            "market": mkt,
                            "pair": pair_norm,
                            "_raw": o,
                        }
                    )

                if len(raw_orders) < page_size:
                    break
                params["page"] += 1
            except Exception as exc:
                raise RuntimeError(f"order status lookup failed: {exc}") from exc
        else:
            raise RuntimeError("order status page limit reached before the final page")

        return out

    def get_order_done_time(self, order_id: int) -> Optional[str]:
        """Return the timestamp of a trade belonging to ``order_id``."""
        if not self._credentials_ready():
            print("Trade history credentials not configured.")
            return None

        path = "/market/trades/list"
        params = {"orderId": order_id}
        try:
            full_path = build_full_path(path, params)
            headers = signed_headers(
                self.api_key, self.secret_key, "GET", full_path
            )
            r = requests.get(
                f"{self.base_url}{full_path}",
                headers=headers,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "ok":
                raise RuntimeError(f"API error: {data}")
            for trade in data.get("trades", []):
                if str(trade.get("orderId")) == str(order_id):
                    return trade.get("timestamp")
            return None
        except Exception as exc:
            print(f"Error fetching trade details: {exc}")
            return None


class OrderCanceller:
    def __init__(
        self,
        api_key: Optional[str],
        secret_key: Optional[str],
        base_url: str = "https://apiv2.nobitex.ir",
        timeout: int = 10,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "TraderBot/LimitOrderBook",
                "Connection": "close",
            }
        )

    def _credentials_ready(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def cancel_all_open_orders(self, max_pages: int = 10) -> None:
        """Fetch all open orders and cancel them one by one.

        The exchange adapter's scoped bulk-cancel method is safer for normal
        bot operation; this legacy helper remains for explicit operator use.
        """
        checker = OrderStatusChecker(
            self.api_key,
            self.secret_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )
        orders = checker.get_order_list_status(
            status="open", max_pages=max_pages
        )
        if not orders:
            print("No open orders to cancel.")
            return

        print(f"Cancelling {len(orders)} open orders...")
        for order in orders:
            order_id = order.get("id")
            if not order_id:
                continue
            try:
                response = self.cancel_order(order_id)
                ok = isinstance(response, dict) and response.get("status") in {
                    "ok",
                    "success",
                }
                print(f"  - {order_id}: {'OK' if ok else response}")
            except Exception as exc:
                print(f"  - {order_id}: error -> {exc}")

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        if not self._credentials_ready():
            print("Order cancellation credentials not configured.")
            return {}

        path = "/market/orders/update-status"
        payload = {"status": "canceled", "order": order_id}
        body = compact_json(payload)
        try:
            headers = signed_headers(
                self.api_key, self.secret_key, "POST", path, body
            )
            r = self.session.post(
                f"{self.base_url}{path}",
                headers=headers,
                data=body,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return {}
            return data
        except Exception as exc:
            print(f"Error cancelling order {order_id}: {exc}")
            return {}

    def cancel_old_orders(
        self,
        max_age_minutes: float,
        df: Optional[pd.DataFrame] = None,
        tracked_orders: Optional[Dict[str, Set[str]]] = None,
    ):
        orders = OrderStatusChecker(
            self.api_key,
            self.secret_key,
            base_url=self.base_url,
            timeout=self.timeout,
        ).get_order_list_status(status="open")
        current_time = datetime.now(timezone.utc)
        cancelled_ids: List[str] = []

        for order in orders:
            try:
                created_text = str(order.get("created_at") or "")
                try:
                    created_at = datetime.fromisoformat(created_text)
                except ValueError:
                    created_at = datetime.strptime(
                        created_text, "%Y-%m-%dT%H:%M:%S.%f%z"
                    )
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_minutes = (current_time - created_at).total_seconds() / 60.0

                if age_minutes > max_age_minutes:
                    print(
                        f"Order {order['id']} older than {max_age_minutes} minutes. Cancelling."
                    )
                    result = self.cancel_order(order["id"])
                    if result.get("status") in {"ok", "success"}:
                        response_order = result.get("order") or {}
                        client_id = response_order.get("clientOrderId") or order.get(
                            "clientOrderId"
                        )
                        if client_id:
                            cancelled_ids.append(str(client_id))
            except (TypeError, ValueError) as exc:
                print(
                    f"Error parsing timestamp for order {order.get('id')}: {exc}"
                )

        if df is not None and cancelled_ids:
            for client_id in cancelled_ids:
                for column in ("Buy Status", "Sell Status"):
                    if column in df.columns:
                        df.loc[df[column] == client_id, column] = "cancelled"

        if tracked_orders is not None and cancelled_ids:
            tracked_orders["buy"] -= set(cancelled_ids)
            tracked_orders["sell"] -= set(cancelled_ids)

        return tracked_orders
