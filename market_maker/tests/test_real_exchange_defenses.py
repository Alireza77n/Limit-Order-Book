from datetime import datetime, timedelta, timezone

import pytest

from exchange.nobitex import NobitexExchangeClient
from order_status import OrderStatusChecker


class _Response:
    def __init__(self, status_code=200, body=None, json_error=None):
        self.status_code = status_code
        self._body = body
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._body

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client():
    return NobitexExchangeClient(
        token="test-token",
        url_send_order="https://mock.invalid/orders",
        url_wallet_balance="https://mock.invalid/wallet",
        algorithm_id="algo1",
    )


@pytest.mark.parametrize(
    "outcome",
    [
        _Response(400, {"status": "ok", "order": {"clientOrderId": "owned-1"}}),
        _Response(500, {"status": "ok", "order": {"clientOrderId": "owned-1"}}),
        _Response(200, {"status": "failed", "order": {"clientOrderId": "owned-1"}}),
        _Response(200, {"status": "ok"}),
        _Response(200, json_error=ValueError("bad json")),
        TimeoutError("timeout"),
    ],
)
def test_rejected_or_unproven_real_submission_is_not_reported_as_sent(monkeypatch, outcome):
    def post(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("exchange.nobitex.requests.post", post)

    result = _client().send_order("buy", "usdt", "rls", 1, 100, "owned-1")

    assert result is None


def test_confirmed_real_submission_returns_matching_client_order_id(monkeypatch):
    response = _Response(200, {
        "status": "ok",
        "order": {"id": 7, "clientOrderId": "owned-1"},
    })
    monkeypatch.setattr("exchange.nobitex.requests.post", lambda *a, **k: response)

    assert _client().send_order("buy", "usdt", "rls", 1, 100, "owned-1") == "owned-1"


def test_status_pagination_reads_beyond_first_hundred(monkeypatch):
    calls = []

    def get(url, *, headers, params, timeout):
        calls.append(dict(params))
        start = (params["page"] - 1) * params["pageSize"]
        count = 100 if params["page"] == 1 else 50
        orders = [
            {
                "id": start + index,
                "status": "open",
                "clientOrderId": f"owned-{start + index}",
                "market": "USDT-RLS",
            }
            for index in range(count)
        ]
        return _Response(200, {"status": "ok", "orders": orders})

    monkeypatch.setattr("order_status.requests.get", get)

    orders = OrderStatusChecker("test-token", base_url="https://mock.invalid").get_order_list_status(
        status="open", page_size=100, max_pages=10
    )

    assert len(orders) == 150
    assert [call["page"] for call in calls] == [1, 2]


class _Checker:
    def __init__(self, orders):
        self.orders = orders
        self.calls = []

    def get_order_list_status(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.orders)


class _Canceller:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"status": "ok", "order": {"id": order_id}}


def _open_order(order_id, client_id, pair, *, created_at=None):
    return {
        "id": order_id,
        "clientOrderId": client_id,
        "pair": pair,
        "status": "open",
        "created_at": created_at or "2026-08-01T00:00:00.000000+0000",
    }


def test_bulk_cancel_is_pair_and_owned_registry_scoped():
    client = _client()
    client._status_checker = _Checker([
        _open_order(1, "owned-1", "USDTIRT"),
        _open_order(2, "owned-2", "BTCIRT"),
        _open_order(3, "foreign-1", "USDTIRT"),
    ])
    client._canceller = _Canceller()

    cancelled = client.cancel_all_open_orders(
        pair="USDTIRT", owned_client_order_ids={"owned-1", "owned-2"}, max_pages=10
    )

    assert cancelled == {"owned-1"}
    assert client._canceller.cancelled == [1]
    assert client._status_checker.calls[0]["max_pages"] == 10


def test_bulk_cancel_fails_closed_without_both_scope_filters():
    client = _client()
    client._status_checker = _Checker([_open_order(1, "owned-1", "USDTIRT")])
    client._canceller = _Canceller()

    assert client.cancel_all_open_orders(pair="USDTIRT", owned_client_order_ids=set()) == set()
    assert client.cancel_all_open_orders(pair=None, owned_client_order_ids={"owned-1"}) == set()
    assert client._canceller.cancelled == []
    assert client._status_checker.calls == []


def test_stale_cancel_prunes_only_owned_active_tracking():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    fresh = now.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    client = _client()
    client._status_checker = _Checker([
        _open_order(1, "owned-old", "USDTIRT", created_at=old),
        _open_order(2, "owned-fresh", "USDTIRT", created_at=fresh),
        _open_order(3, "foreign-old", "USDTIRT", created_at=old),
        _open_order(4, "other-pair", "BTCIRT", created_at=old),
    ])
    client._canceller = _Canceller()
    tracked = {"buy": {"owned-old", "owned-fresh"}, "sell": set()}

    cancelled = client.cancel_old_orders(
        max_age_minutes=30,
        pair="USDTIRT",
        owned_client_order_ids={"owned-old", "owned-fresh"},
        tracked_orders=tracked,
        now=now,
    )

    assert cancelled == {"owned-old"}
    assert client._canceller.cancelled == [1]
    assert tracked == {"buy": {"owned-fresh"}, "sell": set()}
