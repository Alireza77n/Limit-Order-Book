"""Unit tests for Algo1Strategy version 2 -- the I/O-free quoting logic.

The example book, settings and expected numbers follow
docs/algorithms/ALGORITHM_1_V2.md so the documentation and the code can be
checked against each other by hand.
"""

import pytest

from config import MMConfig, TRADING_PROFILES
from fill_model import FillModel
from strategy.algo1 import Algo1Strategy, Candidate
from helpers import make_orderbook


# ---------- fixtures ----------

def _cfg(**overrides):
    base = dict(
        depth_max=10, inv_min=0.0, inv_max=1.0,
        commission_perc=0.0025, exp_ret_min=0.0025, pair="BTCUSDT",
    )
    base.update(overrides)
    return MMConfig(**base)


def _usdt_cfg(**overrides):
    cfg = MMConfig(**TRADING_PROFILES["usdt"])
    cfg.commission_perc = 0.0025
    cfg.exp_ret_min = 0.0025
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _usdt_book():
    """The running example of the specification: mid 2,660,000, 10-rial ticks."""
    bids = [[2_659_990, "20000"], [2_659_900, "300"], [2_658_000, "1500"], [2_655_000, "8000"],
            [2_650_000, "40000"], [2_640_000, "10000"], [2_630_000, "5000"], [2_620_000, "5000"],
            [2_610_000, "5000"], [2_600_000, "5000"], [2_590_000, "5000"]]
    asks = [[2_660_010, "5000"], [2_660_100, "400"], [2_662_000, "2000"], [2_665_000, "6000"],
            [2_670_000, "30000"], [2_680_000, "12000"], [2_690_000, "5000"], [2_700_000, "5000"],
            [2_710_000, "5000"], [2_720_000, "5000"], [2_730_000, "5000"]]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2660000"}


class _ConstantFill(FillModel):
    """Fill model returning one probability everywhere (p == 1 makes the
    expected value equal the net return, so selection is by R_net alone)."""

    def __init__(self, cfg, value=1.0):
        super().__init__(cfg)
        self.value = value

    def probability(self, distance, side):
        return self.value


class _TableFill(FillModel):
    def __init__(self, cfg, table):
        super().__init__(cfg)
        self.table = table

    def probability(self, distance, side):
        # nearest bucket by distance
        key = min(self.table, key=lambda d: abs(d - distance))
        return self.table[key]


# ---------- helpers kept from version 1 ----------

@pytest.mark.parametrize("pair,expected", [
    ("BTCIRT", 10.0),
    ("BTCUSDT", 0.01),
    ("ETHRLS", 0.01),
])
def test_tick_size(pair, expected):
    assert Algo1Strategy(MMConfig(pair=pair)).tick_size() == expected


def test_depth_cap_normal_and_malformed():
    strategy = Algo1Strategy(MMConfig(depth_max=5))
    assert strategy.depth_cap(make_orderbook(n=11)) == 5
    assert strategy.depth_cap(make_orderbook(n=3)) == 2
    assert strategy.depth_cap({"bids": [], "asks": []}) == 0
    assert strategy.depth_cap({}) == 0


def test_gross_bar_is_about_two_fees_plus_the_target():
    strategy = Algo1Strategy(_cfg())
    assert strategy.gross_bar() == pytest.approx(1.0025 / 0.9975 ** 2 - 1)   # 0.753 %
    assert strategy.gross_bar() == pytest.approx(0.00753, abs=2e-5)


def test_spread_ok_still_judges_a_level_pair():
    strategy = Algo1Strategy(_cfg(commission_perc=0.05, exp_ret_min=0.05))
    ob = make_orderbook()
    assert strategy.spread_ok(ob, 8, 8) is True
    assert strategy.spread_ok(ob, 7, 7) is False
    assert strategy.spread_ok(ob, 100, 100) is False


# ---------- inventory position and centre ----------

@pytest.mark.parametrize("q,expected", [(-5.0, 0.0), (0.0, 0.0), (0.25, 0.25), (1.0, 1.0), (7.0, 1.0)])
def test_inventory_position_is_clamped_to_the_band(q, expected):
    assert Algo1Strategy(_cfg()).inventory_position(q) == expected


def test_inventory_position_of_an_empty_band_is_the_middle():
    assert Algo1Strategy(_cfg(inv_min=5.0, inv_max=5.0)).inventory_position(5.0) == 0.5


def test_quote_centre_moves_against_the_inventory():
    strategy = Algo1Strategy(_cfg(skew_perc=0.002))
    ob = make_orderbook()                         # mid 100.5
    assert strategy.quote_centre(ob, 0.5) == pytest.approx(100.5)
    assert strategy.quote_centre(ob, 1.0) == pytest.approx(100.5 * (1 - 0.002))
    assert strategy.quote_centre(ob, 0.0) == pytest.approx(100.5 * (1 + 0.002))
    assert strategy.quote_centre({"bids": [], "asks": []}, 0.5) is None


# ---------- distance grid and candidates ----------

def test_distance_grid_is_geometric_and_bounded():
    strategy = Algo1Strategy(_cfg(grid_min_perc=0.001, grid_ratio=2.0, grid_max_perc=0.016))
    assert strategy.distance_grid() == pytest.approx([0.001, 0.002, 0.004, 0.008, 0.016])
    assert Algo1Strategy(_cfg(grid_min_perc=0.0)).distance_grid() == []


def test_candidates_anchor_to_the_first_level_beyond_the_target():
    """Specification table: grid 0.1 % lands on row 3 (2,655,000 -> 2,655,010,
    real distance 0.188 %), 0.2 % on row 4, 0.4 % on row 5, deeper points are
    synthetic because the book ends."""
    strategy = Algo1Strategy(_usdt_cfg(grid_min_perc=0.001, grid_ratio=2.0, grid_max_perc=0.04, skew_perc=0.0))
    ob = _usdt_book()
    bids, asks = strategy.build_candidates(ob, 2_660_000.0)
    by_price = {c.price: c for c in bids}
    assert [c.price for c in bids][:3] == [2_655_010.0, 2_650_010.0, 2_640_010.0]
    assert by_price[2_655_010.0].level == 3 and by_price[2_655_010.0].anchored
    assert by_price[2_655_010.0].distance == pytest.approx((2_660_000 - 2_655_010) / 2_660_000)
    assert by_price[2_650_010.0].volume_ahead == pytest.approx(20_000 + 300 + 1_500 + 8_000)
    synthetic = [c for c in bids if not c.anchored]
    assert synthetic and all(c.level is None for c in synthetic)
    assert [c.price for c in asks][:3] == [2_664_990.0, 2_669_990.0, 2_679_990.0]


def test_candidates_never_cross_the_touch_and_are_unique():
    strategy = Algo1Strategy(_cfg(skew_perc=0.05))   # huge skew pushes the bid target above the ask
    ob = make_orderbook()
    centre = strategy.quote_centre(ob, 0.0)      # inventory empty: centre far above the mid
    bids, asks = strategy.build_candidates(ob, centre)
    assert all(c.price <= 101.0 - 0.01 + 1e-9 for c in bids)
    assert all(c.price >= 100.0 + 0.01 - 1e-9 for c in asks)
    assert len({c.price for c in bids}) == len(bids)
    assert len({c.price for c in asks}) == len(asks)
    assert bids == sorted(bids, key=lambda c: c.distance)


# ---------- expected-value selection ----------

def test_pair_selection_matches_the_specification_example():
    strategy = Algo1Strategy(_usdt_cfg(skew_perc=0.0))
    quote = strategy.decide_order(_usdt_book(), 25.0)
    assert quote["quote mode"] == "pair"
    assert quote["bid"] == 2_650_010.0 and quote["ask"] == 2_669_990.0
    assert quote["bid level"] == 4 and quote["ask level"] == 4
    assert quote["net return"] == pytest.approx(0.9975 ** 2 * 2_669_990 / 2_650_010 - 1)
    assert quote["net return"] > strategy.min_required_return()
    assert quote["expected value"] == pytest.approx(
        quote["bid exec prob"] * quote["ask exec prob"] * quote["net return"]
    )


def test_every_returned_pair_clears_the_fee_bar(subtests=None):
    """Property: whatever the book and inventory, a two-sided quote satisfies
    (1-f)^2 A/B - 1 > exp_ret_min on the exact prices returned."""
    import random
    rng = random.Random(7)
    strategy = Algo1Strategy(_usdt_cfg(skew_perc=0.002))
    for _ in range(200):
        mid = rng.uniform(1_000_000, 3_000_000)
        bids, asks, price_b, price_a = [], [], mid, mid
        for _level in range(rng.randint(2, 15)):
            price_b -= 10 * rng.randint(1, 400)
            price_a += 10 * rng.randint(1, 400)
            bids.append([str(int(price_b)), str(rng.uniform(1, 50_000))])
            asks.append([str(int(price_a)), str(rng.uniform(1, 50_000))])
        ob = {"bids": bids, "asks": asks, "lastTradePrice": str(int(mid))}
        quote = strategy.decide_order(ob, rng.uniform(-5, 60))
        if quote is None:
            continue
        if quote["quote mode"] == "pair":
            assert strategy.net_return(quote["bid"], quote["ask"]) > strategy.min_required_return()
            assert quote["bid"] < quote["ask"]
        if quote["bid"] is not None:
            assert quote["bid"] <= float(asks[0][0]) - 10 + 1e-6
        if quote["ask"] is not None:
            assert quote["ask"] >= float(bids[0][0]) + 10 - 1e-6


def test_with_certain_fills_the_widest_profitable_pair_wins():
    cfg = _cfg(ev_min=0.0)
    strategy = Algo1Strategy(cfg, fill_model=_ConstantFill(cfg, 1.0))
    quote = strategy.decide_order(make_orderbook(), 0.5)
    evaluation = strategy.evaluate(make_orderbook(), 0.5)
    best = max((p for p in evaluation.pairs if p.passed), key=lambda p: p.net_return)
    assert quote["bid"] == best.bid.price and quote["ask"] == best.ask.price


def test_fill_probabilities_can_prefer_a_tighter_pair():
    """A far pair earns more per fill but almost never fills; the EV rule
    picks the near pair while a p == 1 model would pick the far one."""
    cfg = _usdt_cfg(skew_perc=0.0, ev_min=0.0)
    table = {0.0019: 0.4, 0.0038: 0.2, 0.0075: 0.001, 0.0113: 0.0005, 0.0150: 0.0001}
    near = Algo1Strategy(cfg, fill_model=_TableFill(cfg, table)).decide_order(_usdt_book(), 25.0)
    far = Algo1Strategy(cfg, fill_model=_ConstantFill(cfg, 1.0)).decide_order(_usdt_book(), 25.0)
    assert near["bid"] > far["bid"] and near["ask"] < far["ask"]


def test_one_non_monotone_level_does_not_hide_the_best_pair():
    """Version 1 stopped at the first failing pair. v2 scores all of them."""
    strategy = Algo1Strategy(_usdt_cfg(skew_perc=0.0))
    ob = _usdt_book()
    ob["asks"][3] = [2_690_000, "6000"]       # row 3 pulled far out: a non-monotone book
    quote = strategy.decide_order(ob, 25.0)
    assert quote is not None and quote["quote mode"] == "pair"
    assert quote["net return"] > strategy.min_required_return()


def test_expected_value_floor_rejects_pairs_that_never_fill():
    cfg = _usdt_cfg(skew_perc=0.0, ev_min=0.5)      # impossible floor
    strategy = Algo1Strategy(cfg)
    assert strategy.decide_order(_usdt_book(), 25.0) is None
    assert strategy.no_quote_reason(_usdt_book(), 25.0) == "expected_value_below_floor"


def test_adverse_selection_cost_lowers_the_expected_value():
    plain = Algo1Strategy(_usdt_cfg(skew_perc=0.0)).decide_order(_usdt_book(), 25.0)
    costly = Algo1Strategy(_usdt_cfg(skew_perc=0.0, adverse_selection_perc=0.0005)).decide_order(_usdt_book(), 25.0)
    assert costly["expected value"] < plain["expected value"]


# ---------- one-sided quotes ----------

def test_full_inventory_quotes_the_ask_only():
    strategy = Algo1Strategy(_cfg())
    quote = strategy.decide_order(make_orderbook(), 1.0)
    assert quote["quote mode"] == "ask_only"
    assert quote["bid"] is None and quote["bid level"] is None
    assert quote["ask"] == 101.99 and quote["ask level"] == 1
    assert quote["selection reason"] == "ask_only_inventory_edge"
    # the lone ask must be a full gross bar above the reference price
    assert quote["ask"] > quote["reference price"] * (1 + strategy.gross_bar())
    assert quote["reference price"] == pytest.approx(strategy.quote_centre(make_orderbook(), 1.0))


def test_empty_inventory_quotes_the_bid_only():
    strategy = Algo1Strategy(_cfg())
    quote = strategy.decide_order(make_orderbook(), 0.0)
    assert quote["quote mode"] == "bid_only"
    assert quote["ask"] is None
    assert quote["bid"] == 99.01 and quote["bid level"] == 1
    # the lone bid must be a full gross bar below the reference price
    assert quote["bid"] < quote["reference price"] / (1 + strategy.gross_bar())


def test_lone_leg_falls_back_when_no_pair_clears_the_bar():
    """Spec example: 48 of 50 USDT held, bought at 2,640,000; no pair passes
    with a 2 % floor, but the sell against the cost basis does."""
    cfg = _usdt_cfg(skew_perc=0.0, one_sided_hi=0.99)
    strategy = Algo1Strategy(cfg)
    strategy._avg_cost, strategy._cost_units = 2_640_000.0, 48.0
    cfg.exp_ret_min = 0.02            # no pair can clear this
    cfg.commission_perc = 0.0025
    quote = strategy.decide_order(_usdt_book(), 48.0)
    assert quote is None              # a 2 % lone leg does not exist either
    cfg.exp_ret_min = 0.0025
    quote = strategy.decide_order(_usdt_book(), 48.0)
    assert quote["quote mode"] in {"pair", "ask_only"}


def test_cost_basis_reference_lets_a_cheap_inventory_be_sold_nearer():
    cfg = _usdt_cfg(skew_perc=0.0)
    with_cost = Algo1Strategy(cfg)
    with_cost._avg_cost, with_cost._cost_units = 2_640_000.0, 48.0
    market_only = Algo1Strategy(_usdt_cfg(skew_perc=0.0, cost_basis_reference=False))
    market_only._avg_cost, market_only._cost_units = 2_640_000.0, 48.0
    a = with_cost.decide_order(_usdt_book(), 48.0)
    b = market_only.decide_order(_usdt_book(), 48.0)
    assert a["reference price"] == 2_640_000.0
    assert b["reference price"] == pytest.approx(2_660_000.0)
    assert a["ask"] <= b["ask"]


def test_cost_basis_is_learned_from_inventory_changes():
    strategy = Algo1Strategy(_cfg())
    ob = make_orderbook()
    strategy.decide_order(ob, 0.5)               # quotes bid 100.01
    assert strategy.cost_basis() is None
    strategy.decide_order(ob, 0.6)               # +0.1 arrived: bought at the resting bid
    assert strategy.cost_basis() == pytest.approx(100.01)
    strategy.decide_order(ob, 0.55)              # a sale consumes known units, cost unchanged
    assert strategy.cost_basis() == pytest.approx(100.01)
    strategy.reset_memory()
    assert strategy.cost_basis() is None


# ---------- hysteresis ----------

def test_resting_price_is_kept_within_the_tick_window():
    clock = {"t": 0.0}
    strategy = Algo1Strategy(_cfg(hysteresis_ticks=5, refresh_seconds=300.0), clock=lambda: clock["t"])
    first = strategy.decide_order(make_orderbook(), 0.5)
    shifted = {
        "bids": [[str(100.02 - i), "1.0"] for i in range(11)],   # book up 2 ticks
        "asks": [[str(101.02 + i), "1.0"] for i in range(11)],
        "lastTradePrice": "100",
    }
    clock["t"] = 10.0
    second = strategy.decide_order(shifted, 0.5)
    assert second["bid"] == first["bid"] and second["bid kept"] is True
    assert second["ask"] == first["ask"] and second["ask kept"] is True
    clock["t"] = 400.0                                              # older than refresh_seconds
    third = strategy.decide_order(shifted, 0.5)
    assert third["bid kept"] is False and third["bid"] == pytest.approx(100.03)


def test_hysteresis_is_off_with_zero_ticks():
    strategy = Algo1Strategy(_cfg(hysteresis_ticks=0, requote_band_perc=0.0))
    strategy.decide_order(make_orderbook(), 0.5)
    shifted = {
        "bids": [[str(100.01 - i), "1.0"] for i in range(11)],
        "asks": [[str(101.01 + i), "1.0"] for i in range(11)],
        "lastTradePrice": "100",
    }
    quote = strategy.decide_order(shifted, 0.5)
    assert quote["bid kept"] is False and quote["bid"] == pytest.approx(100.02)


# ---------- invalid books and reasons ----------

def test_single_level_and_crossed_books_return_none():
    strategy = Algo1Strategy(_cfg())
    assert strategy.decide_order({"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}, 0.5) is None
    crossed = {"bids": [["101", "1"], ["100", "1"]], "asks": [["100", "1"], ["99", "1"]], "lastTradePrice": "100"}
    assert strategy.decide_order(crossed, 0.5) is None
    assert strategy.no_quote_reason(crossed, 0.5) == "invalid_or_single_level_order_book"


def test_missing_last_trade_price_does_not_crash():
    strategy = Algo1Strategy(_cfg())
    ob = make_orderbook()
    ob["lastTradePrice"] = None
    quote = strategy.decide_order(ob, 0.5)
    assert quote is not None and quote["lastTradePrice"] is None
    del ob["lastTradePrice"]
    assert strategy.decide_order(ob, 0.5) is not None


def test_no_quote_reason_names_the_bar_when_nothing_clears_it():
    strategy = Algo1Strategy(_cfg(commission_perc=0.3, exp_ret_min=0.3))
    assert strategy.decide_order(make_orderbook(), 0.5) is None
    assert strategy.no_quote_reason(make_orderbook(), 0.5) == "no_side_clears_bar"


def test_quote_dict_carries_the_economics_and_the_decision():
    strategy = Algo1Strategy(_cfg())
    quote = strategy.decide_order(make_orderbook(), 0.5)
    for key in ("bid", "ask", "bid level", "ask level", "mid price", "centre", "inventory position",
                "relative quoted spread", "gross return", "net return", "min required return",
                "bid exec prob", "ask exec prob", "expected value", "quote mode", "selection reason",
                "bid distance", "ask distance", "buy fee", "sell fee"):
        assert key in quote
    assert quote["mid price"] == 100.5
    assert quote["relative quoted spread"] == pytest.approx((quote["ask"] - quote["bid"]) / 100.5)
    assert quote["gross return"] == pytest.approx((quote["ask"] - quote["bid"]) / quote["bid"])
    assert quote["net return"] == pytest.approx(0.9975 ** 2 * quote["ask"] / quote["bid"] - 1)
    assert quote["buy fee"] == quote["sell fee"] == 0.0025


def test_decision_is_identical_for_stale_and_fresh_last_prices():
    strategy = Algo1Strategy(_cfg())
    a = strategy.decide_order(make_orderbook(last=90.0), 0.5)
    strategy.reset_memory()
    b = strategy.decide_order(make_orderbook(last=120.0), 0.5)
    for key in ("bid", "ask", "bid level", "ask level", "net return", "expected value"):
        assert a[key] == b[key]
