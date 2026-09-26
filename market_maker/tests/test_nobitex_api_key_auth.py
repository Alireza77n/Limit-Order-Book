import base64
import json
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exchange.nobitex import NobitexExchangeClient
from exchange.nobitex_auth import (
    build_full_path,
    compact_json,
    create_signature,
)
from order_status import OrderCanceller, OrderStatusChecker
from portfolio import Nobitex_portfolio


API_KEY = "unit-test-key"
SECRET_BYTES = b"\x11" * 32
SECRET_KEY = base64.urlsafe_b64encode(SECRET_BYTES).decode("ascii")


class Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_compact_json_and_query_path_are_deterministic():
    assert compact_json({"currency": "rls", "x": 1}) == '{"currency":"rls","x":1}'
    assert build_full_path(
        "/market/orders/list",
        {"status": "open", "page": 1, "pageSize": 100, "unused": None},
    ) == "/market/orders/list?status=open&page=1&pageSize=100"


def test_signature_covers_exact_method_path_and_body():
    timestamp = "1700000000"
    full_path = "/market/orders/list?status=open&page=1"
    body = ""
    ts, signature_b64 = create_signature(
        SECRET_KEY, "get", full_path, body, timestamp=timestamp
    )
    assert ts == timestamp

    signature = base64.urlsafe_b64decode(signature_b64)
    message = f"{timestamp}GET{full_path}{body}".encode()
    private_key = Ed25519PrivateKey.from_private_bytes(SECRET_BYTES)
    private_key.public_key().verify(signature, message)


def _client(trading_enabled=True):
    return NobitexExchangeClient(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        url_send_order="https://mock.invalid/market/orders/add",
        url_wallet_balance="https://mock.invalid/users/wallets/balance",
        algorithm_id="test",
        trading_enabled=trading_enabled,
    )


def test_wallet_balance_signs_and_sends_identical_raw_body(monkeypatch):
    seen = {}

    def fake_headers(api_key, secret_key, method, full_path, body="", **kwargs):
        seen["signed"] = (api_key, secret_key, method, full_path, body)
        return {"X-Test": "ok"}

    def fake_post(url, *, headers, data, timeout):
        seen["sent"] = (url, headers, data, timeout)
        return Response({"status": "ok", "balance": "123.5"})

    monkeypatch.setattr("exchange.nobitex.signed_headers", fake_headers)
    monkeypatch.setattr("exchange.nobitex.requests.post", fake_post)

    assert _client().wallet_balance("rls") == 123.5
    assert seen["signed"][2:] == (
        "POST",
        "/users/wallets/balance",
        '{"currency":"rls"}',
    )
    assert seen["sent"][2] == seen["signed"][4]


def test_send_order_signs_and_sends_identical_raw_body(monkeypatch):
    seen = {}

    def fake_headers(api_key, secret_key, method, full_path, body="", **kwargs):
        seen["signed"] = (method, full_path, body)
        return {"X-Test": "ok"}

    def fake_post(url, *, headers, data, timeout):
        seen["sent_body"] = data
        payload = json.loads(data)
        return Response(
            {
                "status": "ok",
                "order": {"id": 7, "clientOrderId": payload["clientOrderId"]},
            }
        )

    monkeypatch.setattr("exchange.nobitex.signed_headers", fake_headers)
    monkeypatch.setattr("exchange.nobitex.requests.post", fake_post)

    result = _client().send_order(
        "buy", "usdt", "rls", 1.25, 100000, "owned-test-1"
    )
    assert result == "owned-test-1"
    assert seen["signed"][0:2] == ("POST", "/market/orders/add")
    assert seen["signed"][2] == seen["sent_body"]
    assert json.loads(seen["sent_body"])["clientOrderId"] == "owned-test-1"


def test_order_list_signs_full_path_including_query(monkeypatch):
    signed_paths = []
    requested_urls = []

    def fake_headers(api_key, secret_key, method, full_path, body="", **kwargs):
        signed_paths.append((method, full_path, body))
        return {"X-Test": "ok"}

    def fake_get(url, *, headers, timeout):
        requested_urls.append(url)
        return Response({"status": "ok", "orders": []})

    monkeypatch.setattr("order_status.signed_headers", fake_headers)
    monkeypatch.setattr("order_status.requests.get", fake_get)

    checker = OrderStatusChecker(API_KEY, SECRET_KEY, base_url="https://mock.invalid")
    assert checker.get_order_list_status(status="open", details=2, page_size=100) == []

    assert signed_paths[0][0] == "GET"
    assert signed_paths[0][1].startswith("/market/orders/list?")
    assert "status=open" in signed_paths[0][1]
    assert "page=1" in signed_paths[0][1]
    assert requested_urls[0] == "https://mock.invalid" + signed_paths[0][1]


def test_trade_done_time_signs_trades_path_not_orders_path(monkeypatch):
    signed_paths = []

    def fake_headers(api_key, secret_key, method, full_path, body="", **kwargs):
        signed_paths.append(full_path)
        return {"X-Test": "ok"}

    def fake_get(url, *, headers, timeout):
        return Response(
            {
                "status": "ok",
                "trades": [{"orderId": 42, "timestamp": "2026-01-01T00:00:00Z"}],
            }
        )

    monkeypatch.setattr("order_status.signed_headers", fake_headers)
    monkeypatch.setattr("order_status.requests.get", fake_get)

    checker = OrderStatusChecker(API_KEY, SECRET_KEY, base_url="https://mock.invalid")
    assert checker.get_order_done_time(42) == "2026-01-01T00:00:00Z"
    assert signed_paths == ["/market/trades/list?orderId=42"]


def test_cancel_order_uses_api_key_signature_and_raw_body(monkeypatch):
    seen = {}

    def fake_headers(api_key, secret_key, method, full_path, body="", **kwargs):
        seen["signed"] = (method, full_path, body)
        return {"Nobitex-Key": api_key}

    canceller = OrderCanceller(API_KEY, SECRET_KEY, base_url="https://mock.invalid")

    def fake_post(url, *, headers, data, timeout):
        seen["sent"] = (url, headers, data)
        return Response({"status": "ok", "order": {"id": 99}})

    monkeypatch.setattr("order_status.signed_headers", fake_headers)
    monkeypatch.setattr(canceller.session, "post", fake_post)

    result = canceller.cancel_order(99)
    assert result["status"] == "ok"
    assert seen["signed"] == (
        "POST",
        "/market/orders/update-status",
        '{"status":"canceled","order":99}',
    )
    assert seen["sent"][2] == seen["signed"][2]
    assert "Authorization" not in canceller.session.headers


def test_portfolio_generates_fresh_auth_for_each_private_post(monkeypatch):
    signed = []
    sent = []

    def fake_headers(api_key, secret_key, method, full_path, body="", **kwargs):
        signed.append((method, full_path, body))
        return {"Nobitex-Key": api_key}

    client = Nobitex_portfolio(
        API_KEY, SECRET_KEY, base_url="https://mock.invalid"
    )

    def fake_post(url, *, headers, data):
        sent.append((url, headers, data))
        return Response({"status": "ok", "balance": "10"})

    monkeypatch.setattr("portfolio.signed_headers", fake_headers)
    monkeypatch.setattr(client.session, "post", fake_post)

    assert str(client.get_balance("btc")) == "10"
    assert str(client.get_balance("usdt")) == "10"
    assert len(signed) == 2
    assert signed[0][2] == '{"currency":"btc"}'
    assert signed[1][2] == '{"currency":"usdt"}'
    assert sent[0][2] == signed[0][2]
    assert sent[1][2] == signed[1][2]
    assert "Authorization" not in client.session.headers
    assert "Nobitex-Key" not in client.session.headers


def test_exchange_base_url_is_derived_for_testnet_urls():
    client = NobitexExchangeClient(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        url_send_order="https://testnetapi.nobitex.ir/market/orders/add",
        url_wallet_balance="https://testnetapi.nobitex.ir/users/wallets/balance",
    )
    assert client.base_url == "https://testnetapi.nobitex.ir"
    assert client._status_checker.base_url == client.base_url
    assert client._canceller.base_url == client.base_url
