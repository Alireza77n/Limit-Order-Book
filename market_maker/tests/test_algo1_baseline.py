"""Characterization tests for Algorithm 1's quoting logic.

These pin the CURRENT behavior of SimpleMarketMaker's decision-making
methods (as of the pre-refactor baseline commit) so that later structural
refactors can be verified to leave outputs byte-for-byte identical. Expected
values were captured by running the existing code against synthetic order
books -- they are not independently re-derived from the README formulas, so
any existing quirks or edge-case behavior are intentionally preserved here.

Scope: only the pure, I/O-free quoting methods are exercised. `run()`,
`send_order`, `save_to_db`, and anything touching the live Nobitex API or
MySQL are out of scope -- they cannot be safely exercised in a test
environment (see refactor plan / structural report).
"""

import pytest

from config import MMConfig
from helpers import make_bot, make_orderbook


# ---------- _tick_size ----------

@pytest.mark.parametrize("pair,expected", [
    ("BTCIRT", 10.0),
    ("BTCUSDT", 0.01),
    ("ETHRLS", 0.01),
])
def test_tick_size(pair, expected):
    bot = make_bot(MMConfig(pair=pair))
    assert bot._tick_size() == expected


# ---------- _depth_cap ----------

def test_depth_cap_normal():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap(make_orderbook(n=11)) == 5


def test_depth_cap_capped_by_short_book():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap(make_orderbook(n=3)) == 2


def test_depth_cap_malformed_book():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap({"bids": [], "asks": []}) == 0


def test_depth_cap_missing_keys():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap({}) == 0


# ---------- _leg_pairs ----------

def test_leg_pairs_negative_inv_diff():
    bot = make_bot(MMConfig())
    assert bot._leg_pairs(-5, 10) == [(5, 10), (4, 9), (3, 8), (2, 7), (1, 6)]


def test_leg_pairs_zero_inv_diff():
    bot = make_bot(MMConfig())
    assert bot._leg_pairs(0, 10) == [
        (10, 10), (9, 9), (8, 8), (7, 7), (6, 6),
        (5, 5), (4, 4), (3, 3), (2, 2), (1, 1),
    ]


def test_leg_pairs_positive_inv_diff():
    bot = make_bot(MMConfig())
    assert bot._leg_pairs(5, 10) == [(10, 5), (9, 4), (8, 3), (7, 2), (6, 1), (5, 0)]


def test_leg_pairs_zero_depth():
    bot = make_bot(MMConfig())
    assert bot._leg_pairs(0, 0) == []


# ---------- _spread_ok ----------

def test_spread_ok_bad_index_returns_false():
    bot = make_bot(MMConfig(commission_perc=0.05, exp_ret_min=0.05))
    ob = make_orderbook(last=100.0)
    assert bot._spread_ok(ob, 100, 100) is False


def test_spread_ok_true_case():
    bot = make_bot(MMConfig(commission_perc=0.05, exp_ret_min=0.05))
    ob = make_orderbook(last=100.0)
    assert bot._spread_ok(ob, 10, 10) is True


def test_spread_ok_false_case():
    bot = make_bot(MMConfig(commission_perc=0.05, exp_ret_min=0.05))
    ob = make_orderbook(last=100.0)
    assert bot._spread_ok(ob, 0, 0) is False


# ---------- _algo1_bid_ask_prices: full pipeline ----------

def _cfg_scenario_a():
    return MMConfig(depth_max=10, inv_min=0.0, inv_max=1.0,
                     commission_perc=0.0025, exp_ret_min=None, pair="BTCUSDT")


@pytest.mark.parametrize("q,expected", [
    (0.0, {"bid": 99.01, "ask": 106.99, "bid level": 1, "ask level": 6,
           "lastTradePrice": 100.0, "bid spread": pytest.approx(1.49),
           "ask spread": pytest.approx(6.49)}),
    (0.05, {"bid": 99.01, "ask": 106.99, "bid level": 1, "ask level": 6,
            "lastTradePrice": 100.0, "bid spread": pytest.approx(1.49),
            "ask spread": pytest.approx(6.49)}),
    (0.5, {"bid": 99.01, "ask": 101.99, "bid level": 1, "ask level": 1,
           "lastTradePrice": 100.0, "bid spread": pytest.approx(1.49),
           "ask spread": pytest.approx(1.49)}),
    (0.95, {"bid": 96.01, "ask": 100.99, "bid level": 4, "ask level": 0,
            "lastTradePrice": 100.0, "bid spread": pytest.approx(4.49),
            "ask spread": pytest.approx(0.49)}),
    (1.0, {"bid": 95.01, "ask": 100.99, "bid level": 5, "ask level": 0,
           "lastTradePrice": 100.0, "bid spread": pytest.approx(5.49),
           "ask spread": pytest.approx(0.49)}),
])
def test_algo1_prices_scenario_always_feasible(q, expected):
    """Loose commission threshold: spread condition holds for every candidate
    pair, so the scan walks all the way to the shallowest generated pair."""
    bot = make_bot(_cfg_scenario_a())
    ob = make_orderbook()
    result = bot._algo1_bid_ask_prices(ob, q)
    for key, value in expected.items():
        assert result[key] == value
    assert result["bid exec prob"] == 1.0
    assert result["ask exec prob"] == 1.0


def _cfg_scenario_b():
    return MMConfig(depth_max=10, inv_min=0.0, inv_max=1.0,
                     commission_perc=0.3, exp_ret_min=None, pair="BTCUSDT")


@pytest.mark.parametrize("q", [0.0, 0.5, 1.0])
def test_algo1_prices_scenario_never_feasible(q):
    """Strict commission threshold: no candidate pair satisfies the spread
    condition, not even the loosest one -- the scan never assigns a level, so
    the helper returns None instead of the old untested (depth_max,
    depth_max) fallback. See reports/batch-fill-definitive-root-cause.md."""
    bot = make_bot(_cfg_scenario_b())
    ob = make_orderbook(last=100.0)
    assert bot._algo1_bid_ask_prices(ob, q) is None


def _cfg_scenario_c():
    return MMConfig(depth_max=10, inv_min=0.0, inv_max=1.0,
                     commission_perc=0.05, exp_ret_min=0.05, pair="BTCUSDT")


@pytest.mark.parametrize("q,bid_level,ask_level,bid,ask", [
    (0.5, 5, 5, 95.01, 105.99),
    (0.0, 3, 8, 97.01, 108.99),
    (1.0, 8, 3, 92.01, 103.99),
])
def test_algo1_prices_scenario_breaks_partway(q, bid_level, ask_level, bid, ask):
    """Mid-range commission threshold: the scan is feasible for the deepest
    pairs and stops partway through -- exercises the `else: break` branch."""
    bot = make_bot(_cfg_scenario_c())
    ob = make_orderbook(last=100.0)
    result = bot._algo1_bid_ask_prices(ob, q)
    assert result["bid level"] == bid_level
    assert result["ask level"] == ask_level
    assert result["bid"] == bid
    assert result["ask"] == ask


def test_algo1_prices_single_level_book_returns_none():
    bot = make_bot(MMConfig(depth_max=10))
    ob = {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}
    assert bot._algo1_bid_ask_prices(ob, 0.0) is None


# ---------- _decide_order ----------

def test_decide_order_matches_algo1_prices_when_feasible():
    bot = make_bot(_cfg_scenario_a())
    ob = make_orderbook()
    result = bot._decide_order(ob, 0.5)
    assert result["bid level"] == 1
    assert result["ask level"] == 1
    assert result["bid"] == 99.01
    assert result["ask"] == 101.99


def test_decide_order_single_level_book_returns_none():
    bot = make_bot(MMConfig(depth_max=10))
    ob = {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}
    assert bot._decide_order(ob, 0.0) is None
