"""The admin Orders table must show orders, not round decisions.

engine.py writes one CSV row per ROUND. Rounds that produced no order still
carry a status explaining why ("no_quote", "not_sent_insufficient_cash",
"not_sent_inventory_cap", "not_sent_max_open_orders"). Those rows used to
render in the Orders table with a blank ("nan") order id and zero amounts --
in the observed USDT session, ~1941 of 2085 "orders" were not orders.
See reports/demo-portfolio-jump-investigation.md.
"""

import json
import re
from pathlib import Path

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

from admin_app import _decision_reason, _filter_orders, _is_real_order, create_app
from config import TRADING_PROFILES, demo_csv_path

HTML = {"Accept": "text/html"}

ROWS = [
    # A real submitted+filled buy.
    {"Session_id": "s1", "Round": 1, "Timestamp": "2026-08-01 12:00:00",
     "Quote Status": "quote_generated", "Bid Price": 98.0, "Ask Price": 101.0,
     "Buy Order ID": "usdtirt-buy-1-111", "Buy Status": "filled",
     "Buy Original Amount": 1.0, "Buy Matched Amount": 1.0,
     "Buy Remaining Amount": 0.0, "Buy Limit Price": 98.0,
     "Buy Selected Level": 3, "Buy Fill Reason": "price_touch"},
    # No quote at all -- a decision, not an order.
    {"Session_id": "s1", "Round": 2, "Timestamp": "2026-08-01 12:00:10",
     "Quote Status": "no_quote", "No Quote Reason": "profitability_threshold",
     "Buy Order ID": None, "Buy Status": "no_quote",
     "Buy Original Amount": 0.0, "Buy Matched Amount": 0.0},
    # Quote made, but no cash -- also a decision.
    {"Session_id": "s1", "Round": 3, "Timestamp": "2026-08-01 12:00:20",
     "Quote Status": "quote_generated", "Bid Price": 98.0, "Ask Price": 101.0,
     "Buy Order ID": None, "Buy Status": "not_sent_insufficient_cash",
     "Buy Not Sent Reason": "insufficient_cash",
     "Buy Original Amount": 0.0, "Buy Matched Amount": 0.0},
    # Open-order cap reached (the new Fix B reason).
    {"Session_id": "s1", "Round": 4, "Timestamp": "2026-08-01 12:00:30",
     "Quote Status": "quote_generated", "Bid Price": 98.0, "Ask Price": 101.0,
     "Buy Order ID": None, "Buy Status": "not_sent_max_open_orders",
     "Buy Not Sent Reason": "max_open_orders",
     "Buy Original Amount": 0.0, "Buy Matched Amount": 0.0},
    # A real sell.
    {"Session_id": "s1", "Round": 5, "Timestamp": "2026-08-01 12:00:40",
     "Quote Status": "quote_generated", "Bid Price": 98.0, "Ask Price": 101.0,
     "Sell Order ID": "usdtirt-sell-5-555", "Sell Status": "open",
     "Sell Original Amount": 1.0, "Sell Matched Amount": 0.0,
     "Sell Remaining Amount": 1.0, "Sell Limit Price": 101.0,
     "Sell Selected Level": 2},
]


@pytest.fixture
def app(tmp_path):
    for name in ("control", "state", "results", "backups"):
        (tmp_path / name).mkdir()
    csv_name = Path(demo_csv_path(TRADING_PROFILES["usdt"]["output_csv"])).name
    pd.DataFrame(ROWS).to_csv(tmp_path / "results" / csv_name, index=False)
    (tmp_path / "state" / "stack_session.json").write_text(json.dumps({"session_id": "s1"}))
    return create_app({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256"),
        "SECRET_KEY": "test-secret",
        "CONTROL_DIR": str(tmp_path / "control"),
        "STATE_DIR": str(tmp_path / "state"),
        "RESULTS_DIR": str(tmp_path / "results"),
        "BACKUPS_DIR": str(tmp_path / "backups"),
        "TESTING": True,
    })


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client):
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    client.post("/admin/login", data={"username": "admin", "password": "pw", "csrf_token": token})


def test_orders_view_shows_only_the_running_session(app, client, tmp_path):
    """The CSV keeps every session and round numbers restart at 1 in each, so
    an unfiltered table listed several different 'Round 1' rows and contradicted
    the Dashboard, which does filter. Only the running session may appear."""
    csv_name = Path(demo_csv_path(TRADING_PROFILES["usdt"]["output_csv"])).name
    stale = [
        # Same round numbers as the live rows, from an older session, with the
        # opposite outcome -- this is exactly what made the two views disagree.
        {"Session_id": "s0", "Round": 1, "Timestamp": "2026-07-31 12:00:00",
         "Quote Status": "quote_generated", "Bid Price": 90.0, "Ask Price": 95.0,
         "Buy Order ID": "usdtirt-buy-1-000", "Buy Status": "cancelled",
         "Buy Original Amount": 1.0, "Buy Matched Amount": 0.0},
        {"Session_id": "s0", "Round": 5, "Timestamp": "2026-07-31 12:00:40",
         "Quote Status": "quote_generated", "Bid Price": 90.0, "Ask Price": 95.0,
         "Sell Order ID": "usdtirt-sell-5-000", "Sell Status": "cancelled",
         "Sell Original Amount": 1.0, "Sell Matched Amount": 0.0},
    ]
    pd.DataFrame(stale + ROWS).to_csv(tmp_path / "results" / csv_name, index=False)
    _login(client)

    body = client.get("/admin/orders?pair=usdt&view=decisions").get_json()["orders"]

    assert {row["Session_id"] for row in body} == {"s1"}
    ids = {row.get("Buy Order ID") for row in body} | {row.get("Sell Order ID") for row in body}
    assert "usdtirt-buy-1-000" not in ids
    assert "usdtirt-sell-5-000" not in ids
    # Each round appears once, so a round number identifies one row again.
    rounds = [row["Round"] for row in body]
    assert len(rounds) == len(set(rounds))


def test_a_round_that_bought_and_sold_shows_both_orders(app, client, tmp_path):
    """engine.py writes one CSV row per ROUND carrying both a buy and a sell.
    The table rendered one row per round and picked the buy whenever it existed,
    so completed sell fills were invisible in Admin while the Dashboard showed
    them. Both orders must appear."""
    csv_name = Path(demo_csv_path(TRADING_PROFILES["usdt"]["output_csv"])).name
    both = [{
        "Session_id": "s1", "Round": 8, "Timestamp": "2026-08-01 12:01:00",
        "Quote Status": "quote_generated", "Bid Price": 98.0, "Ask Price": 101.0,
        "Buy Order ID": "usdtirt-buy-8-888", "Buy Status": "filled",
        "Buy Original Amount": 1.0, "Buy Matched Amount": 1.0,
        "Sell Order ID": "usdtirt-sell-8-999", "Sell Status": "filled",
        "Sell Original Amount": 1.0, "Sell Matched Amount": 1.0,
    }]
    pd.DataFrame(both).to_csv(tmp_path / "results" / csv_name, index=False)
    _login(client)

    html = client.get("/admin/orders?pair=usdt", headers=HTML).get_data(as_text=True)

    assert "usdtirt-buy-8-888" in html
    assert "usdtirt-sell-8-999" in html


def test_side_filter_shows_only_that_side_of_a_two_sided_round(app, client, tmp_path):
    csv_name = Path(demo_csv_path(TRADING_PROFILES["usdt"]["output_csv"])).name
    both = [{
        "Session_id": "s1", "Round": 8, "Timestamp": "2026-08-01 12:01:00",
        "Quote Status": "quote_generated",
        "Buy Order ID": "usdtirt-buy-8-888", "Buy Status": "filled",
        "Sell Order ID": "usdtirt-sell-8-999", "Sell Status": "filled",
    }]
    pd.DataFrame(both).to_csv(tmp_path / "results" / csv_name, index=False)
    _login(client)

    html = client.get("/admin/orders?pair=usdt&side=sell", headers=HTML).get_data(as_text=True)

    assert "usdtirt-sell-8-999" in html
    assert "usdtirt-buy-8-888" not in html


# ---------- pure helpers ----------

def test_is_real_order_requires_an_order_id():
    assert _is_real_order({"Buy Order ID": "abc"}) is True
    assert _is_real_order({"Sell Order ID": "abc"}) is True
    assert _is_real_order({"Buy Status": "no_quote", "Buy Order ID": None}) is False
    assert _is_real_order({"Buy Status": "not_sent_insufficient_cash"}) is False
    assert _is_real_order({"Buy Order ID": float("nan")}) is False


def test_decision_reason_prefers_the_specific_reason():
    assert _decision_reason({"Buy Not Sent Reason": "max_open_orders"}) == "max_open_orders"
    assert _decision_reason({"No Quote Reason": "profitability_threshold"}) == "profitability_threshold"
    assert _decision_reason({"Buy Status": "no_quote"}) == "no_quote"


def test_filter_orders_excludes_decisions_by_default():
    kept = _filter_orders(ROWS)
    assert [r["Round"] for r in kept] == [1, 5]


def test_filter_orders_includes_decisions_when_requested():
    kept = _filter_orders(ROWS, include_decisions=True)
    assert [r["Round"] for r in kept] == [1, 2, 3, 4, 5]


# ---------- routes ----------

def test_orders_route_returns_only_real_orders(client):
    _login(client)
    body = client.get("/admin/orders?pair=usdt").get_json()
    assert len(body["orders"]) == 2
    assert all(o["is_order"] for o in body["orders"])


def test_orders_route_decisions_view_includes_reasons(client):
    _login(client)
    body = client.get("/admin/orders?pair=usdt&view=decisions").get_json()
    assert len(body["orders"]) == 5
    decisions = [o for o in body["orders"] if not o["is_order"]]
    assert {d["decision_reason"] for d in decisions} == {
        "profitability_threshold", "insufficient_cash", "max_open_orders",
    }


def test_orders_html_never_renders_literal_nan(client):
    _login(client)
    for url in ("/admin/orders?pair=usdt", "/admin/orders?pair=usdt&view=decisions"):
        body = client.get(url, headers=HTML).get_data(as_text=True)
        assert "nan" not in body.lower().replace("nan-", "")  # no bare nan cells
        assert ">—<" in body or "—" in body  # em dash used for absent values


def test_orders_html_marks_decision_rows(client):
    _login(client)
    body = client.get("/admin/orders?pair=usdt&view=decisions", headers=HTML).get_data(as_text=True)
    assert "Decision" in body
    assert "profitability_threshold" in body


def test_side_filter_still_works_on_real_orders(client):
    _login(client)
    buys = client.get("/admin/orders?pair=usdt&side=buy").get_json()["orders"]
    assert [o["Round"] for o in buys] == [1]
    sells = client.get("/admin/orders?pair=usdt&side=sell").get_json()["orders"]
    assert [o["Round"] for o in sells] == [5]


def test_status_filter_still_works(client):
    _login(client)
    filled = client.get("/admin/orders?pair=usdt&status=filled").get_json()["orders"]
    assert [o["Round"] for o in filled] == [1]


def test_legacy_rows_with_order_id_in_status_are_tolerated():
    """Pre-migration BTC rows stored the client order id in Buy Status."""
    legacy = [{"Round": 9, "Buy Order ID": "buy-1-1785566577924",
               "Buy Status": "buy-1-1785566577924"}]
    assert _filter_orders(legacy) == legacy
