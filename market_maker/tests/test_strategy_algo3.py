"""Algorithm 3 (strategy/algo3.py): order-book imbalance and microprice
market maker. The worked example and the properties follow
docs/algorithms/MM_algo2.md, so the document and the code can be checked
against each other."""

import math
import random

import pytest

from config import MMConfig, TRADING_PROFILES
from strategy.algo3 import Algo3Strategy
from strategy.base import Strategy

TICK = 10.0


def _cfg(**overrides):
    cfg = MMConfig(**TRADING_PROFILES["usdt"])
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _spec_cfg(**overrides):
    """The settings of the design note's worked example (MM_algo2.md section 10)."""
    values = dict(
        commission_perc=0.0025, exp_ret_min=0.0025, inv_min=0.0, inv_max=50.0,
        algo3_depth_k=3, algo3_decay_rho=1.0, algo3_beta=0.0010, skew_perc=0.0020,
        algo3_half_spread=0.0038, algo3_min_volume_orders=0.0, algo3_imbalance_clip=0.8,
        hysteresis_ticks=0,
    )
    values.update(overrides)
    return _cfg(**values)


def _spec_book():
    return {
        "bids": [["2658990", "20000"], ["2658980", "8000"], ["2658970", "6000"], ["2658960", "100"]],
        "asks": [["2659010", "5000"], ["2659020", "4000"], ["2659030", "3000"], ["2659040", "100"]],
        "lastTradePrice": "2659000",
    }


def _book(mid=2_660_000, spread_ticks=2, depth=6, bid_volumes=None, ask_volumes=None, step_ticks=3):
    half = spread_ticks * TICK / 2
    best_bid, best_ask = mid - half, mid + half
    bid_volumes = bid_volumes or [100.0] * depth
    ask_volumes = ask_volumes or [100.0] * depth
    return {
        "bids": [[str(best_bid - i * step_ticks * TICK), str(v)] for i, v in enumerate(bid_volumes)],
        "asks": [[str(best_ask + i * step_ticks * TICK), str(v)] for i, v in enumerate(ask_volumes)],
        "lastTradePrice": str(mid),
    }


def _random_book(rng):
    mid = rng.randrange(1_000_000, 300_000_000, 10)
    spread_ticks = rng.choice([1, 2, 3, 5, 20, 80, 400])
    depth = rng.randint(1, 8)
    best_bid = mid - (spread_ticks // 2) * TICK
    best_ask = best_bid + spread_ticks * TICK
    bids, asks = [], []
    price = best_bid
    for _ in range(depth):
        bids.append([str(price), str(round(rng.uniform(0.0001, 5000), 4))])
        price -= rng.randint(1, 50) * TICK
    price = best_ask
    for _ in range(depth):
        asks.append([str(price), str(round(rng.uniform(0.0001, 5000), 4))])
        price += rng.randint(1, 50) * TICK
    return {"bids": bids, "asks": asks, "lastTradePrice": str(mid)}


# ---------- the worked example of the specification ----------

def test_spec_worked_example_reproduces_every_number():
    strategy = Algo3Strategy(_spec_cfg())
    quote = strategy.decide_order(_spec_book(), 10.0)

    assert quote["imbalance"] == pytest.approx(22_000 / 46_000)          # 0.478
    assert quote["microprice"] == pytest.approx(2_659_006.0)              # four fifths of the way to the ask
    assert quote["inventory position"] == pytest.approx(0.2)
    assert quote["centre"] == pytest.approx(2_659_006 * (1 + 0.001 * 22 / 46) * 1.0012)
    assert round(quote["centre"]) == 2_663_470
    assert quote["bid"] == 2_653_340.0                                    # c(1 - h) rounded down
    assert quote["ask"] == 2_673_600.0                                    # c(1 + h) rounded up
    assert quote["net return"] == pytest.approx(0.9975 ** 2 * 2_673_600 / 2_653_340 - 1)
    assert round(quote["net return"] * 100, 4) == 0.2604
    assert quote["quote mode"] == "pair" and quote["selection reason"] == "microprice_pair"
    assert quote["bid exec prob"] is None and quote["expected value"] is None


def test_minimum_half_spread_is_g_over_two_plus_g():
    strategy = Algo3Strategy(_spec_cfg())
    gross = 1.0025 / 0.9975 ** 2 - 1
    assert strategy.gross_bar() == pytest.approx(gross)
    assert strategy.minimum_half_spread() == pytest.approx(gross / (2 + gross))
    assert round(strategy.minimum_half_spread() * 100, 4) == 0.3752


def test_half_spread_defaults_to_the_minimum_plus_one_tick():
    strategy = Algo3Strategy(_spec_cfg(algo3_half_spread=0.0))
    document = strategy.diagnostics(_spec_book(), 10.0, round_no=1, session_id="s")
    assert document["h"] == pytest.approx(document["h_min"] + TICK / document["centre"])
    assert document["h"] > document["h_min"]


# ---------- the signals ----------

def test_microprice_is_the_mid_shifted_by_half_spread_times_level0_imbalance():
    rng = random.Random(7)
    for _ in range(200):
        book = _random_book(rng)
        bids = [(float(p), float(v)) for p, v in book["bids"]]
        asks = [(float(p), float(v)) for p, v in book["asks"]]
        (b0, vb0), (a0, va0) = bids[0], asks[0]
        micro = Algo3Strategy.microprice(bids, asks)
        mid, spread = (a0 + b0) / 2, a0 - b0
        level0 = (vb0 - va0) / (vb0 + va0)
        assert micro - mid == pytest.approx(spread / 2 * level0, abs=1e-6)
        assert b0 - 1e-6 <= micro <= a0 + 1e-6


def test_imbalance_weights_deeper_levels_by_rho_to_the_i():
    strategy = Algo3Strategy(_cfg(algo3_depth_k=3, algo3_decay_rho=0.5))
    bids = [(100.0, 10.0), (99.0, 20.0), (98.0, 40.0), (97.0, 1000.0)]
    asks = [(101.0, 30.0), (102.0, 10.0), (103.0, 0.0), (104.0, 1000.0)]
    vb, va, imbalance = strategy.imbalance(bids, asks)
    assert vb == pytest.approx(10 + 0.5 * 20 + 0.25 * 40)
    assert va == pytest.approx(30 + 0.5 * 10)
    assert imbalance == pytest.approx((vb - va) / (vb + va))


def test_imbalance_is_clipped_so_one_large_level_cannot_dominate():
    strategy = Algo3Strategy(_cfg(algo3_imbalance_clip=0.8, algo3_min_volume_orders=0.0, hysteresis_ticks=0))
    book = _book(bid_volumes=[10_000.0] * 6, ask_volumes=[1.0] * 6)
    document = strategy.diagnostics(book, 25.0, round_no=1, session_id="s")
    assert document["imbalance_raw"] > 0.99
    assert document["imbalance"] == pytest.approx(0.8)


def test_a_thin_book_carries_no_signal_and_centres_on_the_mid():
    cfg = _cfg(algo3_min_volume_orders=20.0, order_size=1.0, algo3_beta=0.002, hysteresis_ticks=0,
               inv_min=0.0, inv_max=50.0)
    strategy = Algo3Strategy(cfg)
    book = _book(bid_volumes=[5.0, 1.0, 1.0], ask_volumes=[0.5, 0.5, 0.5], depth=3)
    document = strategy.diagnostics(book, 25.0, round_no=1, session_id="s")
    assert document["thin_book"] is True
    assert document["imbalance"] == 0.0
    assert document["microprice"] == pytest.approx(document["mid_price"])
    assert document["centre"] == pytest.approx(document["mid_price"])   # x = 0.5: no skew either


def test_reduces_to_a_fixed_spread_around_the_mid():
    """beta = 0, k = 1 and a balanced top of book: the fixed-spread market
    maker centred on the mid (MM_algo2.md section 14)."""
    cfg = _cfg(algo3_beta=0.0, algo3_depth_k=1, algo3_min_volume_orders=0.0, inv_min=0.0, inv_max=50.0,
               hysteresis_ticks=0, algo3_half_spread=0.004)
    strategy = Algo3Strategy(cfg)
    book = _book(mid=2_660_000, spread_ticks=2)
    quote = strategy.decide_order(book, 25.0)
    assert quote["centre"] == pytest.approx(2_660_000)
    assert quote["bid"] == math.floor(2_660_000 * 0.996 / TICK) * TICK
    assert quote["ask"] == math.ceil(2_660_000 * 1.004 / TICK) * TICK


# ---------- lean and skew ----------

def test_buying_pressure_moves_both_quotes_up():
    base = dict(algo3_beta=0.002, algo3_min_volume_orders=0.0, hysteresis_ticks=0, inv_min=0.0, inv_max=50.0)
    balanced = Algo3Strategy(_cfg(**base)).decide_order(_book(), 25.0)
    pressured = Algo3Strategy(_cfg(**base)).decide_order(
        _book(bid_volumes=[400.0] * 6, ask_volumes=[100.0] * 6), 25.0)
    assert pressured["imbalance"] > 0
    assert pressured["bid"] > balanced["bid"] and pressured["ask"] > balanced["ask"]


def test_more_inventory_moves_both_quotes_down():
    base = dict(algo3_beta=0.0, algo3_min_volume_orders=0.0, hysteresis_ticks=0, inv_min=0.0, inv_max=50.0)
    low = Algo3Strategy(_cfg(**base)).decide_order(_book(), 10.0)
    high = Algo3Strategy(_cfg(**base)).decide_order(_book(), 40.0)
    assert high["centre"] < low["centre"]
    assert high["bid"] < low["bid"] and high["ask"] < low["ask"]


def test_spread_ratio_does_not_depend_on_the_signal_or_the_inventory():
    """A/B = (1+h)/(1-h) before rounding, whatever I and x are."""
    rng = random.Random(11)
    for _ in range(100):
        cfg = _cfg(algo3_beta=rng.uniform(0, 0.003), skew_perc=rng.uniform(0, 0.004),
                   algo3_min_volume_orders=0.0, inv_min=0.0, inv_max=50.0, hysteresis_ticks=0)
        document = Algo3Strategy(cfg).diagnostics(_random_book(rng), rng.uniform(0, 50), round_no=1, session_id="s")
        if document["raw_bid"] is None:
            continue
        h = document["h"]
        assert document["raw_ask"] / document["raw_bid"] == pytest.approx((1 + h) / (1 - h))


# ---------- the guarantees ----------

@pytest.mark.parametrize("fee,target", [(0.0025, 0.0001), (0.0025, 0.0025), (0.001, 0.001), (0.0008, 0.0001), (0.0, 0.0)])
def test_every_quote_clears_the_fee_bar_never_crosses_and_sits_on_the_tick(fee, target):
    rng = random.Random(int(fee * 1e6) + int(target * 1e7))
    quoted = 0
    for _ in range(400):
        cfg = _cfg(commission_perc=fee, exp_ret_min=target,
                   algo3_depth_k=rng.randint(1, 5), algo3_decay_rho=rng.uniform(0.05, 1.0),
                   algo3_beta=rng.uniform(0, 0.01), skew_perc=rng.uniform(0, 0.02),
                   algo3_half_spread=rng.choice([0.0, rng.uniform(0, 0.02)]),
                   algo3_min_volume_orders=rng.choice([0.0, 20.0]), order_size=rng.uniform(0.001, 5),
                   algo3_imbalance_clip=rng.uniform(0, 1), inv_min=0.0, inv_max=50.0, hysteresis_ticks=0)
        book = _random_book(rng)
        quote = Algo3Strategy(cfg).decide_order(book, rng.uniform(-10, 60))
        if quote is None:
            continue
        quoted += 1
        bid, ask = quote["bid"], quote["ask"]
        best_bid, best_ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        assert (1 - fee) ** 2 * ask / bid - 1 > target
        assert 0 < bid < ask
        assert bid <= best_ask - TICK and ask >= best_bid + TICK
        assert bid % TICK == 0 and ask % TICK == 0
    assert quoted > 0


def test_the_final_check_on_the_sent_prices_rejects_an_unprofitable_quote(monkeypatch):
    """The safety net: it can only trigger when h is below h_min, which the
    strategy never chooses on its own (forced here)."""
    strategy = Algo3Strategy(_spec_cfg(algo3_half_spread=0.0))
    monkeypatch.setattr(strategy, "minimum_half_spread", lambda: 0.0)   # h = one tick: far below the bar
    assert strategy.decide_order(_spec_book(), 10.0) is None
    assert strategy.no_quote_reason(_spec_book(), 10.0) == "net_return_below_bar"


def test_clipping_at_the_touch_only_widens_the_quote():
    """A centre far above the ask clips the bid down to one tick below the
    ask: the ratio A/B grows, so the round trip still clears the bar."""
    strategy = Algo3Strategy(_cfg(skew_perc=0.05, inv_min=0.0, inv_max=50.0, algo3_min_volume_orders=0.0,
                                  hysteresis_ticks=0, commission_perc=0.0025, exp_ret_min=0.0001))
    book = _book(mid=2_660_000, spread_ticks=2)
    document = strategy.diagnostics(book, 0.0, round_no=1, session_id="s")   # x = 0: centre +5 %
    assert document["clipped_bid"] is True
    assert document["selected_bid_price"] == float(book["asks"][0][0]) - TICK
    h = document["h"]
    assert document["selected_ask_price"] / document["selected_bid_price"] > (1 + h) / (1 - h)
    assert document["net_return"] > document["min_required_return"]


@pytest.mark.parametrize("book,reason", [
    ({"bids": [["100", "1"]], "asks": [["90", "1"]], "lastTradePrice": "95"}, "invalid_or_single_level_order_book"),
    ({"bids": [], "asks": [["90", "1"]], "lastTradePrice": "95"}, "invalid_or_single_level_order_book"),
    ({"bids": [["2659990", "5"], ["2659980", "5"]], "asks": [["2660010", "5"], ["2660020", "5"]], "lastTradePrice": "2660000"},
     "book_shallower_than_depth_k"),
])
def test_unusable_books_give_no_quote_with_a_reason(book, reason):
    strategy = Algo3Strategy(_cfg(algo3_depth_k=3))
    assert strategy.decide_order(book, 1.0) is None
    assert strategy.no_quote_reason(book, 1.0) == reason


# ---------- hysteresis ----------

def test_a_pair_that_barely_moved_is_kept_until_it_is_refreshed():
    clock = {"t": 0.0}
    cfg = _cfg(hysteresis_ticks=5, refresh_seconds=300.0, algo3_min_volume_orders=0.0, inv_min=0.0, inv_max=50.0)
    strategy = Algo3Strategy(cfg, clock=lambda: clock["t"])
    first = strategy.decide_order(_book(mid=2_660_000), 25.0)

    clock["t"] = 60.0
    nudged = strategy.decide_order(_book(mid=2_660_020), 25.0)        # moved 2 ticks: kept
    assert (nudged["bid"], nudged["ask"]) == (first["bid"], first["ask"])
    assert nudged["bid kept"] and nudged["selection reason"] == "microprice_pair_kept"

    clock["t"] = 400.0
    refreshed = strategy.decide_order(_book(mid=2_660_020), 25.0)     # older than refresh_seconds
    assert refreshed["bid"] != first["bid"] and not refreshed["bid kept"]

    clock["t"] = 410.0
    moved = strategy.decide_order(_book(mid=2_661_000), 25.0)         # 98 ticks away: re-quoted
    assert moved["ask"] > refreshed["ask"] and not moved["ask kept"]


def test_no_quote_forgets_the_resting_pair():
    strategy = Algo3Strategy(_cfg(hysteresis_ticks=5, algo3_min_volume_orders=0.0))
    strategy.decide_order(_book(), 25.0)
    assert strategy._resting is not None
    strategy.decide_order({"bids": [], "asks": [], "lastTradePrice": "1"}, 25.0)
    assert strategy._resting is None


# ---------- interface ----------

def test_algo3_is_a_strategy_with_its_catalog_key():
    strategy = Algo3Strategy(_cfg())
    assert isinstance(strategy, Strategy)
    assert strategy.key == "algo3"


def test_diagnostics_describe_the_decision_without_touching_the_memory():
    strategy = Algo3Strategy(_spec_cfg(hysteresis_ticks=5))
    document = strategy.diagnostics(_spec_book(), 10.0, round_no=4, session_id="s1")
    assert strategy._resting is None
    assert document["algorithm"] == "algo3" and document["round"] == 4
    assert len(document["levels"]) == 3 and document["levels"][1]["weight"] == 1.0
    assert document["selected_bid_price"] == 2_653_340.0 and document["selected_ask_price"] == 2_673_600.0
    # the keys the engine, Telegram and the admin panel read from every strategy's documents
    assert document["required_threshold"] == pytest.approx(0.0025)
    [pair] = document["candidate_results"]
    assert pair["passed"] is True and pair["submitted_bid_price"] == 2_653_340.0
    assert document["quote_mode"] == "pair" and document["selection_reason"] == "microprice_pair"


def test_engine_facing_quote_keys_are_complete():
    quote = Algo3Strategy(_spec_cfg()).decide_order(_spec_book(), 10.0)
    for key in ("bid", "ask", "bid level", "ask level", "mid price", "centre", "relative quoted spread",
                "gross return", "net return", "min required return", "bid distance", "ask distance",
                "quote mode", "selection reason", "imbalance", "microprice"):
        assert key in quote
    assert isinstance(quote["bid level"], int) and isinstance(quote["ask level"], int)
