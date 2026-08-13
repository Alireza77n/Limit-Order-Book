"""Profitability-reference tests for Algorithm 1's reworked spread check.

The old feasibility test compared the raw level gap against the last traded
price:  (ask - bid) / last > commission_perc + exp_ret_min. The last traded
price is a stale snapshot of the order book, so the check could both over-
and under-estimate the current market (a trade printed at 90 against a book
centered at 100.5 inflates the measured spread by ~11%). Under the rework:

* the market's relative quoted spread is measured against the current
  mid-price M_t = (best_bid + best_ask) / 2  --  a market descriptor;
* the economic return on deployed capital uses the SUBMITTED (tick-adjusted)
  buy price as the basis, deducting proportional fees on both legs:
  R_net = (A_q(1-fb)(1-fs) - B_q) / B_q, where fb=fs=commission_perc;
* a candidate pair is accepted only when R_net strictly exceeds
  exp_ret_min (bootstrapped to commission_perc when unset).
"""

import pytest

from config import MMConfig
from helpers import make_orderbook
from strategy.algo1 import Algo1Strategy


def _strategy(**cfg_overrides):
    base = dict(
        depth_max=10, inv_min=0.0, inv_max=1.0,
        commission_perc=0.0025, exp_ret_min=0.0025, pair="BTCUSDT",
    )
    base.update(cfg_overrides)
    return Algo1Strategy(MMConfig(**base))


# ---------- Mid-price: the market descriptor ----------

def test_relative_quoted_spread_uses_mid_price_not_last():
    strategy = _strategy()
    ob = make_orderbook(last=90.0)  # stale trade far from the 100/101 book
    assert strategy.relative_quoted_spread(ob, 99.01, 101.99) == pytest.approx(
        (101.99 - 99.01) / 100.5
    )
    stale_ob = make_orderbook(last=120.0)
    assert strategy.relative_quoted_spread(stale_ob, 99.01, 101.99) == pytest.approx(
        (101.99 - 99.01) / 100.5
    )


def test_mid_price_of_a_book_with_unusable_top_is_none():
    strategy = _strategy()
    assert strategy.mid_price(make_orderbook()) == 100.5
    assert strategy.mid_price({"bids": [], "asks": [], "lastTradePrice": "100"}) is None
    assert strategy.mid_price({}) is None
    crossed = {
        "bids": [["101", "1"], ["100", "1"]],
        "asks": [["100", "1"], ["99", "1"]],
        "lastTradePrice": "100",
    }
    assert strategy.mid_price(crossed) is None
    non_positive = {
        "bids": [["0", "1"], ["-1", "1"]],
        "asks": [["101", "1"], ["102", "1"]],
        "lastTradePrice": "100",
    }
    assert strategy.mid_price(non_positive) is None


# ---------- Returns on the submitted buy price ----------

def test_gross_return_uses_submitted_buy_price_as_capital_basis():
    strategy = _strategy()
    assert strategy.gross_return(99.01, 101.99) == pytest.approx(
        (101.99 - 99.01) / 99.01
    )
    assert strategy.gross_return(0, 101) is None
    assert strategy.gross_return("bad", 101) is None


def test_net_return_formula_with_symmetric_default_fees():
    strategy = _strategy(commission_perc=0.05)
    bid, ask = 99.01, 101.99
    net = strategy.net_return(bid, ask)
    assert net == pytest.approx((ask * (1 - 0.05) ** 2 - bid) / bid)


def test_net_return_respects_asymmetric_fees():
    strategy = _strategy()
    bid, ask = 99.01, 101.99
    net = strategy.net_return(bid, ask, buy_fee=0.01, sell_fee=0.02)
    assert net == pytest.approx((ask * (1 - 0.01) * (1 - 0.02) - bid) / bid)


def test_net_return_with_zero_fees_equals_gross():
    strategy = _strategy(commission_perc=0.0)
    assert strategy.net_return(100.01, 101.99) == pytest.approx(
        strategy.gross_return(100.01, 101.99)
    )


def test_net_return_of_invalid_prices_is_none():
    strategy = _strategy()
    assert strategy.net_return(0, 101) is None
    assert strategy.net_return("bad", 101) is None


# ---------- Minimum required return ----------

def test_min_required_return_bootstraps_when_exp_ret_min_unset():
    strategy = Algo1Strategy(MMConfig(commission_perc=0.003, exp_ret_min=None))
    assert strategy.min_required_return() == 0.003
    diagnostic = strategy.diagnostics(
        make_orderbook(), 0.5, round_no=1, session_id="s"
    )
    assert diagnostic["exp_ret_min"] == 0.003
    assert diagnostic["required_threshold"] == 0.003
    assert diagnostic["min_required_return"] == 0.003


# ---------- Stale-last invariance ----------

def test_decision_and_metrics_are_identical_for_stale_and_fresh_last_prices():
    strategy = _strategy()
    quote_90 = strategy.decide_order(make_orderbook(last=90.0), 0.5)
    quote_120 = strategy.decide_order(make_orderbook(last=120.0), 0.5)
    assert quote_90 is not None and quote_120 is not None
    for key in (
        "bid", "ask", "bid level", "ask level", "mid price",
        "relative quoted spread", "gross return", "net return",
    ):
        assert quote_90[key] == quote_120[key]
    assert quote_90["lastTradePrice"] == 90.0
    assert quote_120["lastTradePrice"] == 120.0


# ---------- Strict profitability bar ----------

def test_candidate_above_the_bar_passes_and_below_fails():
    strategy = _strategy(commission_perc=0.05, exp_ret_min=0.05)
    ob = make_orderbook(last=100.0)
    assert strategy.spread_ok(ob, 8, 8) is True    # net ~ 6.9% > 5%
    assert strategy.spread_ok(ob, 7, 7) is False   # net ~ 4.8% < 5%


def test_candidate_with_net_return_just_below_the_bar_is_rejected():
    # commission 0 keeps the arithmetic exact: submitted (100.01, 102.99)
    # grosses 2.98% against a 2.99% bar. The raw level gap (3.0% of last)
    # would have cleared the old check; the submitted-price return does not.
    strategy = _strategy(commission_perc=0.0, exp_ret_min=0.0299)
    bids = [[str(101 - i), "1"] for i in range(11)]
    asks = [[str(102 + i), "1"] for i in range(11)]
    ob = {"bids": bids, "asks": asks, "lastTradePrice": "100"}
    assert (103 - 100) / 100 > strategy.min_required_return()
    assert strategy.spread_ok(ob, 1, 1) is False


def test_profitability_is_checked_on_submitted_not_raw_prices():
    """Past validation was eroded by the one-tick inward move (2 eps did not
    even get retested). Now the tick-adjusted prices decide: pair (1,1) with
    raw gap 3% clears the 2.99% bar but its submitted prices (100.01/102.99,
    net 2.98%) do not, so the scan stops at the last feasible pair (2,2)."""
    strategy = _strategy(commission_perc=0.0, exp_ret_min=0.0299)
    bids = [[str(101 - i), "1"] for i in range(11)]
    asks = [[str(102 + i), "1"] for i in range(11)]
    ob = {"bids": bids, "asks": asks, "lastTradePrice": "100"}

    assert strategy.spread_ok(ob, 1, 1) is False
    quote = strategy.decide_order(ob, 0.5)
    assert quote["bid level"] == 2
    assert quote["ask level"] == 2


# ---------- Quote dict carries the new economics ----------

def test_quote_carries_mid_relative_spread_gross_and_net():
    strategy = _strategy()
    quote = strategy.algo1_bid_ask_prices(make_orderbook(), 0.5)
    c = strategy.cfg.commission_perc
    assert quote["mid price"] == 100.5
    assert quote["relative quoted spread"] == pytest.approx(
        (quote["ask"] - quote["bid"]) / 100.5
    )
    assert quote["gross return"] == pytest.approx(
        (quote["ask"] - quote["bid"]) / quote["bid"]
    )
    assert quote["net return"] == pytest.approx(
        (1 - c) ** 2 * (quote["ask"] / quote["bid"]) - 1
    )
    assert quote["buy fee"] == quote["sell fee"] == c
    assert quote["min required return"] == c


def test_decide_order_returns_exactly_the_scan_result():
    strategy = _strategy(commission_perc=0.05, exp_ret_min=0.05)
    ob = make_orderbook(last=100.0)
    assert strategy.decide_order(ob, 0.5) == strategy.algo1_bid_ask_prices(ob, 0.5)


# ---------- Crossed / malformed books fail closed ----------

def test_crossed_book_never_quotes():
    strategy = _strategy()
    crossed = {
        "bids": [["101", "1"], ["100", "1"]],
        "asks": [["100", "1"], ["99", "1"]],
        "lastTradePrice": "100",
    }
    assert strategy.mid_price(crossed) is None
    assert strategy.relative_quoted_spread(crossed, 100.01, 99.99) is None
    assert strategy.spread_ok(crossed, 0, 0) is False
    assert strategy.spread_ok(crossed, 1, 1) is False
    assert strategy.decide_order(crossed, 0.5) is None