"""MazdaxExchangeClient and its signature, pinned against https://api-docs.mazdax.tech:
the HMAC over METHOD, PATH and the query string; the Nobitex-shaped order book with the trades printed
since the previous call; order rows; fee units; and the demo-mode guard. No network."""
import hashlib
import hmac

import pytest

from exchange.mazdax import MazdaxExchangeClient, _number, asset_code
from exchange.mazdax_auth import API_KEY_HEADER, build_query, sign, signed_request
from safety import DemoSafetyError


class _Response:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


class _Session:
    """Answers by path; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, method, url):
        self.calls.append((method, url))
        path = url.split("mazdax.ir", 1)[1].split("?", 1)[0]
        answer = self.routes[(method, path)]
        if isinstance(answer, list) and answer and isinstance(answer[0], _Response):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer if isinstance(answer, _Response) else _Response(answer)

    def get(self, url, params=None, timeout=None):
        return self._answer("GET", url)

    def request(self, method, url, headers=None, timeout=None):
        self.last_headers = headers
        return self._answer(method, url)


PUBLIC = "/api/v1/spot/public"
PRIVATE = "/hapi/v1/spot"
BOOK = {"bids": [[83000, 5], [83101, 117], [82000, 2]], "asks": [[88900, 11500], [88895, 10000]]}


def _trade(price, qty, time, buyer_maker):
    return {"id": str(time), "price": str(price), "qty": str(qty), "time": time, "isBuyerMaker": buyer_maker}


def test_signature_is_hmac_of_method_path_and_query():
    query = "symbol=AHRM1IRR&side=BUY&type=LIMIT&price=365000&qt=1&timestamp=1718200000000"
    expected = hmac.new(b"secret", f"POST\n/hapi/v1/spot/order\n{query}".encode(), hashlib.sha256).hexdigest()
    assert sign("secret", "post", "/hapi/v1/spot/order", query) == expected
    path, headers = signed_request(
        "mzx_key", "secret", "POST", "/hapi/v1/spot/order",
        [("symbol", "AHRM1IRR"), ("side", "BUY"), ("type", "LIMIT"), ("price", "365000"), ("qt", "1")],
        timestamp_ms=1718200000000,
    )
    assert path == f"/hapi/v1/spot/order?{query}&signature={expected}"
    assert headers == {API_KEY_HEADER: "mzx_key"}


def test_query_keeps_order_drops_none_and_spells_booleans():
    assert build_query([("b", 1), ("a", None), ("postOnly", True)]) == "b=1&postOnly=true"
    path, _ = signed_request("k", "s", "GET", "/hapi/v1/spot/wallet", None, timestamp_ms=5)
    assert path.startswith("/hapi/v1/spot/wallet?timestamp=5&signature=")


def test_numbers_and_assets():
    assert _number(88339.0) == "88339"
    assert _number(0.5) == "0.5"
    assert _number(1e-05) == "0.00001"
    assert asset_code("rls") == "IRR" and asset_code("irr") == "IRR" and asset_code("ahrm1") == "AHRM1"


def test_orderbook_is_nobitex_shaped_best_price_first():
    session = _Session({
        ("GET", f"{PUBLIC}/books"): BOOK,
        ("GET", f"{PUBLIC}/trades"): [_trade(88339, 729, 2000, False), _trade(87039, 10, 1000, True)],
    })
    client = MazdaxExchangeClient(None, None, session=session)
    ob = client.get_orderbook("ahrm1irr")
    assert ob["bids"][0] == ["83101", "117"] and ob["bids"][-1] == ["82000", "2"]
    assert ob["asks"][0] == ["88895", "10000"]
    assert ob["lastTradePrice"] == "88339"          # the newest trade, whatever the order of the reply
    assert ob["marketTrades"] == []                 # first call only sets the watermark


def test_orderbook_reports_only_trades_after_the_previous_call():
    routes = {
        ("GET", f"{PUBLIC}/books"): BOOK,
        ("GET", f"{PUBLIC}/trades"): [_trade(87000, 10, 1000, True)],
    }
    client = MazdaxExchangeClient(None, None, session=_Session(routes))
    client.get_orderbook("AHRM1IRR")
    routes[("GET", f"{PUBLIC}/trades")] = [
        _trade(87000, 10, 1000, True), _trade(86000, 40, 3000, True), _trade(88500, 7, 4000, False),
    ]
    trades = client.get_orderbook("AHRM1IRR")["marketTrades"]
    assert trades == [
        {"price": 86000.0, "amount": 40.0, "taker": "sell", "time": 3000},
        {"price": 88500.0, "amount": 7.0, "taker": "buy", "time": 4000},
    ]


def test_orderbook_with_an_empty_side_is_refused():
    session = _Session({("GET", f"{PUBLIC}/books"): {"bids": [], "asks": []}})
    assert MazdaxExchangeClient(None, None, session=session).get_orderbook("USDTIRR") is None


def _live_client(routes):
    routes.setdefault(("GET", f"{PUBLIC}/time"), {"serverTime": 1718200000000})
    session = _Session(routes)
    client = MazdaxExchangeClient(
        "mzx_key", "secret", url_send_order="https://api.mazdax.ir/hapi/v1/spot/order",
        url_wallet_balance="https://api.mazdax.ir/hapi/v1/spot/wallet", session=session,
    )
    return client, session


def test_send_order_signs_the_query_and_returns_our_client_id():
    client, session = _live_client({
        ("POST", f"{PRIVATE}/order"): _Response(
            {"id": 12345, "symbol": "AHRM1IRR", "clientOrderId": "buy-7-1", "status": "NEW"}, 201),
    })
    assert client.send_order("buy", "ahrm1", "irr", 34.0, 88339.0, "buy-7-1") == "buy-7-1"
    method, url = session.calls[-1]
    assert method == "POST"
    query = url.split("?", 1)[1]
    assert query.startswith("symbol=AHRM1IRR&side=BUY&type=LIMIT&price=88339&qt=34&clientOrderId=buy-7-1&timestamp=")
    signed, signature = query.rsplit("&signature=", 1)
    assert signature == sign("secret", "POST", f"{PRIVATE}/order", signed)
    assert session.last_headers == {API_KEY_HEADER: "mzx_key"}


def test_send_order_failure_returns_none():
    client, _ = _live_client({("POST", f"{PRIVATE}/order"): _Response({"code": 146, "error": "x"}, 422)})
    assert client.send_order("sell", "ahrm1", "irr", 34, 88339, "sell-1") is None


def test_demo_mode_blocks_orders_and_cancels():
    client = MazdaxExchangeClient(None, None, trading_enabled=False, session=_Session({}))
    with pytest.raises(DemoSafetyError):
        client.send_order("buy", "ahrm1", "irr", 1, 1)
    with pytest.raises(DemoSafetyError):
        client.cancel_order(1)


def test_open_and_closed_rows_are_what_the_engine_reads():
    open_order = {"id": 1, "symbol": "AHRM1IRR", "side": "BUY", "type": "LIMIT", "price": "88000",
                  "clientOrderId": "buy-1", "quantity": "34", "filled": "4", "status": "PARTIALLY_FILLED",
                  "createdAt": 1718200000000}
    filled = {"id": 2, "symbol": "AHRM1IRR", "side": "SELL", "type": "LIMIT", "price": "89000",
              "avgPrice": "89000", "quantity": "10", "filled": "10", "status": "FILLED",
              "createdAt": 1718200000000, "fee": "0.02", "feeAsset": "AHRM1"}
    client, _ = _live_client({
        ("POST", f"{PRIVATE}/order"): _Response({"id": 2, "clientOrderId": "sell-2"}, 201),
        ("GET", f"{PRIVATE}/order/open"): [open_order],
        ("GET", f"{PRIVATE}/order"): filled,
    })
    client.send_order("sell", "ahrm1", "irr", 10, 89000, "sell-2")
    (row,) = client.get_order_list_status("open")
    assert (row["status"], row["type"], row["clientOrderId"], row["pair"]) == ("Active", "buy", "buy-1", "AHRM1IRR")
    assert (row["amount"], row["matchedAmount"], row["unmatchedAmount"]) == (34.0, 4.0, 30.0)
    assert row["created_at"].startswith("2024-06-12T")
    (done,) = client.get_order_list_status("close")
    assert (done["status"], done["clientOrderId"], done["matchedAmount"], done["unmatchedAmount"]) == (
        "Done", "sell-2", 10.0, 0.0)
    # a sell's fee is counted in the quote currency: 0.02 AHRM1 at 89000
    assert done["_raw"]["fee"] == pytest.approx(1780.0)


def test_buy_fee_paid_in_rial_is_converted_to_coins():
    client, _ = _live_client({})
    row = client._row({"id": 3, "symbol": "AHRM1IRR", "side": "BUY", "price": "88000", "avgPrice": "88000",
                       "quantity": "10", "filled": "10", "status": "FILLED", "fee": "880", "feeAsset": "IRR"})
    assert row["_raw"]["fee"] == pytest.approx(0.01)


def test_cancelled_order_keeps_its_partial_fill():
    client, _ = _live_client({})
    row = client._row({"id": 4, "symbol": "AHRM1IRR", "side": "BUY", "price": "88000", "quantity": "34",
                       "filled": "4", "status": "CANCELED"})
    assert (row["status"], row["matchedAmount"], row["unmatchedAmount"]) == ("Canceled", 4.0, 30.0)


def test_wallet_balance_reads_the_asset_row():
    client, _ = _live_client({("GET", f"{PRIVATE}/wallet"): [
        {"asset": "AHRM1", "balance": "1200", "balance_locked": "100"},
        {"asset": "IRR", "balance": "150000000", "balance_locked": "36500000"},
    ]})
    assert client.wallet_balance("irr") == 150000000.0
    assert client.wallet_balance("ahrm1") == 1200.0
    assert client.wallet_balance("roba1") == 0.0


def test_cancel_old_orders_cancels_only_owned_old_orders_of_the_pair():
    from datetime import datetime, timezone
    old, new = 1718200000000, 1718200170000
    orders = [
        {"id": 1, "symbol": "AHRM1IRR", "side": "BUY", "price": "1", "quantity": "1", "filled": "0",
         "status": "OPEN", "clientOrderId": "buy-1", "createdAt": old},
        {"id": 2, "symbol": "AHRM1IRR", "side": "BUY", "price": "1", "quantity": "1", "filled": "0",
         "status": "OPEN", "clientOrderId": "buy-2", "createdAt": new},
        {"id": 3, "symbol": "ROBA1IRR", "side": "BUY", "price": "1", "quantity": "1", "filled": "0",
         "status": "OPEN", "clientOrderId": "other", "createdAt": old},
    ]
    client, session = _live_client({
        ("GET", f"{PRIVATE}/order/open"): orders,
        ("POST", f"{PRIVATE}/cancel"): {"id": 1, "status": "PRE_CANCELED"},
    })
    tracked = {"buy": {"buy-1", "buy-2"}, "sell": set()}
    cancelled = client.cancel_old_orders(
        3, tracked_orders=tracked, pair="AHRM1IRR", owned_client_order_ids={"buy-1", "buy-2", "other"},
        now=datetime.fromtimestamp(1718200200, tz=timezone.utc),
    )
    assert cancelled == {"buy-1"} and tracked["buy"] == {"buy-2"}
    assert [u for m, u in session.calls if m == "POST"][0].split("?")[1].startswith("order_id=1&timestamp=")
