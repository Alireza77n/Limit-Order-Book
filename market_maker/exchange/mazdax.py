"""Mazdax implementation of ``ExchangeClient`` (https://api-docs.mazdax.tech).

Public market data needs no key (``/api/v1/spot/public``); trading uses an API key and an HMAC-SHA256
signature over the query string (``/hapi/v1/spot``, see ``exchange/mazdax_auth.py``).

The engine was written against Nobitex, so every method returns Nobitex-shaped values:

* ``get_orderbook`` returns ``{"bids": [[price, amount], ...], "asks": [...], "lastTradePrice": ...}``
  with the best price first on each side, plus ``marketTrades``: the public trades printed since this
  client's previous call (the demo's trade-fill rule reads them);
* order rows carry ``id``, ``clientOrderId``, ``status`` (``Active`` / ``Done`` / ``Canceled``),
  ``type`` (``buy`` / ``sell``), ``price``, ``amount``, ``matchedAmount``, ``unmatchedAmount``,
  ``created_at`` (ISO 8601), ``pair`` (the Mazdax symbol itself) and ``_raw`` with a cumulative ``fee``
  in the unit the engine expects (base coin for a buy, quote currency for a sell).

Mazdax finds a finished order by its numeric id only, never by ``clientOrderId``, so the client keeps
the id of every order it sent and asks ``GET /order`` for those that left the open list.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests

from exchange.base import ExchangeClient
from exchange.mazdax_auth import signed_request
from safety import DemoSafetyError

DEFAULT_BASE_URL = "https://api.mazdax.ir"
PUBLIC_PATH = "/api/v1/spot/public"
PRIVATE_PATH = "/hapi/v1/spot"
OPEN_STATUSES = {"CREATED", "NEW", "OPEN", "PARTIALLY_FILLED"}
CANCELLED_STATUSES = {"CANCELED", "CANCELLED", "PRE_CANCELED", "EXPIRED", "REJECTED"}
RIAL_CODES = {"rls", "irr", "irt"}
# A finished order's fee can lag its fill by a moment; keep asking for this long before caching it.
FINAL_RECHECK_SECONDS = 90.0


def _number(value: float) -> str:
    """A decimal string without exponent or trailing zeros: 88339.0 -> "88339", 0.5 -> "0.5"."""
    number = float(value)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def asset_code(currency: str) -> str:
    """The Mazdax asset of one of our currency codes: every rial code is IRR."""
    code = str(currency or "").lower()
    return "IRR" if code in RIAL_CODES else code.upper()


class MazdaxExchangeClient(ExchangeClient):
    def __init__(
        self,
        api_key: Optional[str],
        secret_key: Optional[str],
        url_send_order: Optional[str] = None,
        url_wallet_balance: Optional[str] = None,
        algorithm_id: str = "default",
        trading_enabled: bool = True,
        base_url: Optional[str] = None,
        post_only: bool = False,
        depth: int = 50,
        session: Optional[requests.Session] = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.algorithm_id = algorithm_id
        self.trading_enabled = trading_enabled
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.post_only = bool(post_only)
        self.depth = int(depth)
        self.http = session or requests.Session()
        # url_send_order / url_wallet_balance are accepted for the shared constructor shape; None
        # (demo mode) switches the private calls off exactly as it does for Nobitex.
        self._private_enabled = bool(url_send_order or url_wallet_balance)
        self._clock_offset_ms = 0
        self._clock_checked_at = 0.0
        self._last_trade_time: Dict[str, int] = {}
        # clientOrderId -> exchange id and symbol of the orders this client sent or saw open
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._final_rows: Dict[str, Dict[str, Any]] = {}
        self._final_seen_at: Dict[str, float] = {}

    # ------------------------------------------------------------------ plumbing
    def _require_trading_enabled(self) -> None:
        if not self.trading_enabled:
            raise DemoSafetyError("Demo Mode blocks real Mazdax order and cancellation methods")

    def _credentials_ready(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _public(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5) -> Any:
        r = self.http.get(f"{self.base_url}{PUBLIC_PATH}{path}", params=params, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Mazdax {path} HTTP {r.status_code}")
        return r.json()

    def _timestamp_ms(self) -> int:
        """Local time corrected by the server clock (Mazdax allows 5 seconds of difference)."""
        if time.monotonic() - self._clock_checked_at > 300 or self._clock_checked_at == 0.0:
            try:
                before = time.time()
                server = int(self._public("/time")["serverTime"])
                after = time.time()
                self._clock_offset_ms = server - int((before + after) / 2 * 1000)
            except Exception:
                pass
            self._clock_checked_at = time.monotonic()
        return int(time.time() * 1000) + self._clock_offset_ms

    def _private(self, method: str, path: str, params=None, timeout: float = 20) -> requests.Response:
        if not self._credentials_ready():
            raise RuntimeError("Mazdax credentials not configured")
        full_path, headers = signed_request(
            self.api_key, self.secret_key, method, f"{PRIVATE_PATH}{path}", params,
            timestamp_ms=self._timestamp_ms(),
        )
        return self.http.request(method, f"{self.base_url}{full_path}", headers=headers, timeout=timeout)

    # ------------------------------------------------------------------ market data
    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        symbol = str(symbol).upper()
        try:
            book = self._public("/books", {"symbol": symbol, "limit": self.depth})
            bids = sorted(([float(p), float(q)] for p, q in book.get("bids") or []), key=lambda r: -r[0])
            asks = sorted(([float(p), float(q)] for p, q in book.get("asks") or []), key=lambda r: r[0])
            if not bids or not asks:
                print("Orderbook payload has an empty side.")
                return None
            trades = self._public("/trades", {"symbol": symbol, "limit": 100})
            trades = sorted(
                (t for t in trades or [] if _float(t.get("price")) and _float(t.get("qty"))),
                key=lambda t: int(t.get("time") or 0),
            )
            if trades:
                last_price = float(trades[-1]["price"])
            else:
                last_price = float(self._public(f"/ticker/{symbol}").get("lastPrice") or 0.0)
            if last_price <= 0:
                last_price = (bids[0][0] + asks[0][0]) / 2.0
            watermark = self._last_trade_time.get(symbol)
            fresh: List[Dict[str, Any]] = []
            if watermark is not None:
                for t in trades:
                    if int(t.get("time") or 0) > watermark:
                        fresh.append({
                            "price": float(t["price"]),
                            "amount": float(t["qty"]),
                            # isBuyerMaker: the buyer rested and a seller took the price
                            "taker": "sell" if t.get("isBuyerMaker") else "buy",
                            "time": int(t["time"]),
                        })
            if trades:
                self._last_trade_time[symbol] = int(trades[-1].get("time") or 0)
            elif watermark is None:
                self._last_trade_time[symbol] = 0
            return {
                "status": "ok",
                "bids": [[_number(p), _number(q)] for p, q in bids],
                "asks": [[_number(p), _number(q)] for p, q in asks],
                "lastTradePrice": _number(last_price),
                "marketTrades": fresh,
            }
        except Exception as exc:
            print(f"Orderbook error: {exc}")
            return None

    # ------------------------------------------------------------------ wallet
    def wallet_balance(self, currency: str) -> Optional[float]:
        if not (self._credentials_ready() and self._private_enabled):
            print("Wallet balance endpoints not configured.")
            return None
        try:
            r = self._private("GET", "/wallet", timeout=10)
            if r.status_code != 200:
                print(f"Wallet balance HTTP {r.status_code}: {r.text}")
                return None
            want = asset_code(currency)
            rows = r.json()
            if not isinstance(rows, list):
                print(f"Wallet balance API error: {rows}")
                return None
            for row in rows:
                if str(row.get("asset") or "").upper() == want:
                    return float(row.get("balance"))
            return 0.0   # an asset never held has no wallet row
        except Exception as exc:
            print(f"Wallet balance error: {exc}")
            return None

    # ------------------------------------------------------------------ orders
    def send_order(
        self,
        order_type: str,
        srcCurrency: str,
        dstCurrency: str,
        amount: float,
        price: float,
        clientOrderId: Optional[str] = None,
    ) -> Optional[str]:
        self._require_trading_enabled()
        if not (self._credentials_ready() and self._private_enabled):
            print("Order send endpoints not configured.")
            return None
        side = str(order_type).lower()
        if side not in ("buy", "sell"):
            return None
        if clientOrderId is None:
            clientOrderId = f"algo-{self.algorithm_id}-{side}-{int(time.time() * 1000)}"
        symbol = f"{asset_code(srcCurrency)}{asset_code(dstCurrency)}"
        params = [
            ("symbol", symbol),
            ("side", side.upper()),
            ("type", "LIMIT"),
            ("price", _number(price)),
            ("qt", _number(amount)),
            ("clientOrderId", clientOrderId),
        ]
        if self.post_only:
            params.append(("postOnly", True))
        try:
            r = self._private("POST", "/order", params)
            if not 200 <= int(r.status_code) < 300:
                print(f"Send order HTTP {r.status_code}: {r.text}")
                return None
            order = r.json()
            if not isinstance(order, dict) or order.get("id") is None:
                return None
            confirmed = order.get("clientOrderId")
            if confirmed is not None and str(confirmed) != str(clientOrderId):
                return None
            self._orders[str(clientOrderId)] = {"id": order["id"], "symbol": symbol, "side": side}
            return str(clientOrderId)
        except Exception as exc:
            print(f"Send order error: {exc}")
            return None

    def _row(self, order: Dict[str, Any], client_id: Optional[str] = None) -> Dict[str, Any]:
        """One Mazdax order as the Nobitex-shaped row the engine reads."""
        raw_status = str(order.get("status") or "").upper()
        amount = _float(order.get("quantity")) or 0.0
        filled = _float(order.get("filled")) or 0.0
        side = str(order.get("side") or "").lower()
        symbol = str(order.get("symbol") or "").upper()
        if raw_status in OPEN_STATUSES:
            status = "Active"
        elif raw_status == "FILLED":
            status = "Done"
        else:
            status = "Canceled"
        price = _float(order.get("price")) or 0.0
        average = _float(order.get("avgPrice")) or price
        fee = _float(order.get("fee")) or 0.0
        fee_asset = str(order.get("feeAsset") or "").upper()
        quote = "IRR" if symbol.endswith("IRR") else ""
        if fee and fee_asset and average > 0:
            # the engine counts a buy's fee in the base coin and a sell's fee in the quote currency
            if side == "buy" and fee_asset == quote:
                fee = fee / average
            elif side == "sell" and fee_asset != quote:
                fee = fee * average
        created = order.get("createdAt")
        created_at = (
            datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc).isoformat()
            if created is not None else None
        )
        raw = dict(order)
        raw["fee"] = fee
        return {
            "id": order.get("id"),
            "status": status,
            "created_at": created_at,
            "execution": str(order.get("type") or "").lower() or None,
            "type": side or None,
            "clientOrderId": client_id or order.get("clientOrderId"),
            "price": price,
            "amount": amount,
            "matchedAmount": filled,
            "unmatchedAmount": 0.0 if status == "Done" else max(0.0, amount - filled),
            "market": symbol,
            "pair": symbol,
            "_raw": raw,
        }

    def _open_rows(self) -> List[Dict[str, Any]]:
        r = self._private("GET", "/order/open")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
        orders = r.json()
        if not isinstance(orders, list):
            raise RuntimeError("open orders response is not a list")
        rows = []
        for order in orders:
            row = self._row(order)
            client_id = str(row.get("clientOrderId") or "")
            if client_id:
                self._orders.setdefault(client_id, {
                    "id": row["id"], "symbol": row["pair"], "side": row["type"],
                })
            rows.append(row)
        return rows

    def _closed_rows(self, open_client_ids: Set[str]) -> List[Dict[str, Any]]:
        """Orders this client knows that are no longer open, one ``GET /order`` each until final."""
        rows = []
        now = time.monotonic()
        for client_id, known in list(self._orders.items()):
            if client_id in open_client_ids:
                continue
            cached = self._final_rows.get(client_id)
            if cached is not None and now - self._final_seen_at.get(client_id, now) > FINAL_RECHECK_SECONDS:
                rows.append(cached)
                continue
            r = self._private("GET", "/order", [("orderId", known["id"])])
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
            row = self._row(r.json(), client_id)
            if row["status"] == "Active":
                continue   # between the two requests it was still open; the next round sees it
            self._final_rows[client_id] = row
            self._final_seen_at.setdefault(client_id, now)
            rows.append(row)
        return rows

    def get_order_list_status(
        self,
        status: str = "open",
        details: int = 2,
        page_size: int = 100,
        max_pages: int = 1,
    ) -> List[Dict]:
        if not self._credentials_ready():
            raise RuntimeError("order status credentials not configured")
        try:
            open_rows = self._open_rows()
            if str(status).lower() == "open":
                return open_rows
            open_ids = {str(row.get("clientOrderId") or "") for row in open_rows}
            return self._closed_rows(open_ids)
        except Exception as exc:
            raise RuntimeError(f"order status lookup failed: {exc}") from exc

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        self._require_trading_enabled()
        try:
            r = self._private("POST", "/cancel", [("order_id", int(order_id))], timeout=10)
            if r.status_code == 200:
                return {"status": "ok", "order": r.json()}
            return {"status": "failed", "code": r.status_code, "message": r.text}
        except Exception as exc:
            return {"status": "failed", "message": str(exc)}

    def _owned_open(self, pair: Optional[str], owned_client_order_ids) -> List[Dict[str, Any]]:
        owned = {str(value) for value in (owned_client_order_ids or set())}
        if not pair or not owned:
            return []
        want = str(pair).upper()
        return [
            row for row in self._open_rows()
            if str(row.get("clientOrderId") or "") in owned and str(row.get("pair") or "").upper() == want
        ]

    def cancel_all_open_orders(
        self,
        max_pages: int = 10,
        *,
        pair: Optional[str] = None,
        owned_client_order_ids=None,
    ) -> Set[str]:
        self._require_trading_enabled()
        cancelled: Set[str] = set()
        for row in self._owned_open(pair, owned_client_order_ids):
            if row.get("id") is None:
                continue
            if self.cancel_order(row["id"]).get("status") == "ok":
                cancelled.add(str(row["clientOrderId"]))
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
        current_time = now or datetime.now(timezone.utc)
        cancelled: Set[str] = set()
        for row in self._owned_open(pair, owned_client_order_ids):
            try:
                created_at = datetime.fromisoformat(str(row.get("created_at")))
            except (TypeError, ValueError):
                continue
            if (current_time - created_at).total_seconds() <= max_age_minutes * 60:
                continue
            if row.get("id") is None:
                continue
            if self.cancel_order(row["id"]).get("status") == "ok":
                cancelled.add(str(row["clientOrderId"]))
        if df is not None:
            for client_id in cancelled:
                for column in ("Buy Status", "Sell Status"):
                    if column in df.columns:
                        df.loc[df[column] == client_id, column] = "cancelled"
        if tracked_orders is not None:
            tracked_orders["buy"] -= cancelled
            tracked_orders["sell"] -= cancelled
        return cancelled
