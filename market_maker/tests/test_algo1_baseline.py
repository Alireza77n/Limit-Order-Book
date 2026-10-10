"""Engine-side characterization of Algorithm 1 v2.

SimpleMarketMaker delegates every quoting decision to Algo1Strategy; these
tests pin the delegation and the engine-visible contract (what a quote dict
must contain, what the engine does with a missing side) without going
through the network or MySQL. The strategy's own arithmetic is covered in
test_strategy_algo1.py.
"""

import pytest

from config import MMConfig
from helpers import make_bot, make_orderbook


# ---------- delegation of the helpers the engine still calls ----------

@pytest.mark.parametrize("pair,expected", [
    ("BTCIRT", 10.0),
    ("BTCUSDT", 0.01),
    ("ETHRLS", 0.01),
])
def test_tick_size(pair, expected):
    bot = make_bot(MMConfig(pair=pair))
    assert bot._tick_size() == expected


def test_depth_cap_normal():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap(make_orderbook(n=11)) == 5


def test_depth_cap_capped_by_short_book():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap(make_orderbook(n=3)) == 2


def test_depth_cap_malformed_book():
    bot = make_bot(MMConfig(depth_max=5))
    assert bot._depth_cap({"bids": [], "asks": []}) == 0
    assert bot._depth_cap({}) == 0


def test_spread_ok_delegates_to_the_strategy():
    bot = make_bot(MMConfig(commission_perc=0.05, exp_ret_min=0.05, pair="BTCUSDT"))
    ob = make_orderbook(last=100.0)
    assert bot._spread_ok(ob, 10, 10) is True
    assert bot._spread_ok(ob, 0, 0) is False
    assert bot._spread_ok(ob, 100, 100) is False


# ---------- _decide_order: the engine-visible contract ----------

def _cfg_scenario_a():
    return MMConfig(depth_max=10, inv_min=0.0, inv_max=1.0,
                    commission_perc=0.0025, exp_ret_min=0.0025, pair="BTCUSDT")


def test_decide_order_returns_the_strategy_quote():
    bot = make_bot(_cfg_scenario_a())
    ob = make_orderbook()
    result = bot._decide_order(ob, 0.5)
    assert result["quote mode"] == "pair"
    assert result["bid"] == 100.01 and result["ask"] == 100.99
    assert result["bid level"] == 0 and result["ask level"] == 0
    assert result["bid exec prob"] == pytest.approx(result["ask exec prob"])
    assert 0.0 < result["bid exec prob"] <= 1.0


@pytest.mark.parametrize("q,mode,bid,ask", [
    (0.0, "bid_only", 99.01, None),
    (0.05, "bid_only", 99.01, None),
    (0.5, "pair", 100.01, 100.99),
    (0.95, "ask_only", None, 101.99),
    (1.0, "ask_only", None, 101.99),
])
def test_decide_order_across_the_inventory_band(q, mode, bid, ask):
    """Empty inventory quotes bids only, full inventory asks only, the
    middle both -- the engine receives None for the absent side. A lone leg
    must clear the whole gross bar against the centre, so it sits one level
    deeper than the pair's leg."""
    bot = make_bot(_cfg_scenario_a())
    result = bot._decide_order(make_orderbook(), q)
    assert result["quote mode"] == mode
    assert result["bid"] == bid
    assert result["ask"] == ask


def test_decide_order_never_feasible_returns_none():
    """A 30 % fee cannot be covered by any pair or lone leg of this book."""
    bot = make_bot(MMConfig(depth_max=10, inv_min=0.0, inv_max=1.0,
                            commission_perc=0.3, exp_ret_min=None, pair="BTCUSDT"))
    for q in (0.0, 0.5, 1.0):
        assert bot._decide_order(make_orderbook(last=100.0), q) is None


def test_decide_order_single_level_book_returns_none():
    bot = make_bot(MMConfig(depth_max=10))
    ob = {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}
    assert bot._decide_order(ob, 0.0) is None


def test_quote_side_helpers_accept_a_missing_side():
    bot = make_bot(_cfg_scenario_a())
    quote = {"bid": None, "ask": 100.99, "bid level": None, "ask level": 0}
    assert bot._quote_side_price(quote, "bid") is None
    assert bot._quote_side_price(quote, "ask") == 100.99
    assert bot._quote_side_level(quote, "bid level") is None
    assert bot._quote_side_level(quote, "ask level") == 0
    assert bot._quote_side_price(None, "bid") is None


def test_resting_order_guard_blocks_only_prices_within_the_tick_window():
    bot = make_bot(MMConfig(pair="USDTIRT", hysteresis_ticks=5))
    resting = [(2_650_010.0, 1.0), (2_700_000.0, 0.0)]
    assert bot._resting_order_blocks(2_650_030.0, resting) is True     # 2 ticks away
    assert bot._resting_order_blocks(2_650_100.0, resting) is False    # 9 ticks away
    assert bot._resting_order_blocks(2_700_000.0, resting) is False    # nothing remaining
    assert bot._resting_order_blocks(None, resting) is False
    bot.cfg.hysteresis_ticks = 0
    assert bot._resting_order_blocks(2_650_010.0, resting) is False
