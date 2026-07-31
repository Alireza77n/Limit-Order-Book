"""Unit tests for Algo1Strategy -- the extracted, I/O-free quoting logic.

Mirrors the scenarios in test_algo1_baseline.py, but exercises the strategy
class directly instead of going through SimpleMarketMaker. This is the
lighter-weight test target going forward; test_algo1_baseline.py stays in
place as the regression net proving the SimpleMarketMaker delegation didn't
change behavior.
"""

import pytest

from Config import MMConfig
from strategy.algo1_strategy import Algo1Strategy
from helpers import make_orderbook


@pytest.mark.parametrize("pair,expected", [
    ("BTCIRT", 10.0),
    ("BTCUSDT", 0.01),
    ("ETHRLS", 0.01),
])
def test_tick_size(pair, expected):
    strategy = Algo1Strategy(MMConfig(pair=pair))
    assert strategy.tick_size() == expected


def test_depth_cap_normal():
    strategy = Algo1Strategy(MMConfig(depth_max=5))
    assert strategy.depth_cap(make_orderbook(n=11)) == 5


def test_depth_cap_malformed_book():
    strategy = Algo1Strategy(MMConfig(depth_max=5))
    assert strategy.depth_cap({"bids": [], "asks": []}) == 0


def test_leg_pairs_positive_inv_diff():
    strategy = Algo1Strategy(MMConfig())
    assert strategy.leg_pairs(5, 10) == [(10, 5), (9, 4), (8, 3), (7, 2), (6, 1), (5, 0)]


def test_spread_ok_bad_index_returns_false():
    strategy = Algo1Strategy(MMConfig(commission_perc=0.05, exp_ret_min=0.05))
    ob = make_orderbook(last=100.0)
    assert strategy.spread_ok(ob, 100, 100) is False


def _cfg_scenario_a():
    return MMConfig(depth_max=10, inv_min=0.0, inv_max=1.0,
                     commission_perc=0.0025, exp_ret_min=0.0025, pair="BTCUSDT")


@pytest.mark.parametrize("q,bid_level,ask_level,bid,ask", [
    (0.0, 1, 6, 99.01, 106.99),
    (0.5, 1, 1, 99.01, 101.99),
    (1.0, 5, 0, 95.01, 100.99),
])
def test_algo1_bid_ask_prices_scenario_always_feasible(q, bid_level, ask_level, bid, ask):
    strategy = Algo1Strategy(_cfg_scenario_a())
    ob = make_orderbook()
    result = strategy.algo1_bid_ask_prices(ob, q)
    assert result["bid level"] == bid_level
    assert result["ask level"] == ask_level
    assert result["bid"] == bid
    assert result["ask"] == ask


def test_algo1_bid_ask_prices_single_level_book_returns_none():
    strategy = Algo1Strategy(MMConfig(depth_max=10))
    ob = {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}
    assert strategy.algo1_bid_ask_prices(ob, 0.0) is None


def test_decide_order_matches_algo1_bid_ask_prices_when_feasible():
    strategy = Algo1Strategy(_cfg_scenario_a())
    ob = make_orderbook()
    result = strategy.decide_order(ob, 0.5)
    assert result["bid level"] == 1
    assert result["ask level"] == 1


def test_decide_order_single_level_book_returns_none():
    strategy = Algo1Strategy(MMConfig(depth_max=10))
    ob = {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}
    assert strategy.decide_order(ob, 0.0) is None
