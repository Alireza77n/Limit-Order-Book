"""Algorithm 4 (strategy/algo4.py, shown as MM_algo3): market making with a
mean-reverting signal. The estimator, the admissibility rule, the quotes and
their properties follow docs/algorithms/MM_algo3.md, so the document and the
code can be checked against each other."""

import math
import random

import pytest

from config import MMConfig, TRADING_PROFILES
from fill_model import FillTable
from strategy.algo4 import MIN_POINTS, Algo4Strategy, ou_fit
from strategy.base import Strategy
from strategy.catalog import algorithm_label

TICK = 10.0


def _cfg(**overrides):
    cfg = MMConfig(**TRADING_PROFILES["usdt"])
    cfg.fill_table_enabled = False        # kappa from the setting unless a test says otherwise
    cfg.commission_perc, cfg.exp_ret_min = 0.0008, 0.0002
    cfg.inv_min, cfg.inv_max = 0.0, 50.0
    cfg.hysteresis_ticks, cfg.requote_band_perc = 0, 0.0
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _book(mid, spread_ticks=2, depth=4):
    half = spread_ticks * TICK / 2
    best_bid, best_ask = mid - half, mid + half
    return {
        "bids": [[str(best_bid - i * 30 * TICK), "100"] for i in range(depth)],
        "asks": [[str(best_ask + i * 30 * TICK), "100"] for i in range(depth)],
        "lastTradePrice": str(mid),
    }


def _ou_path(mu, half_life, sigma_eps, n, dt, seed=1, start=None):
    """A simulated Ornstein-Uhlenbeck path sampled every dt seconds."""
    rng = random.Random(seed)
    b = math.exp(-math.log(2.0) / half_life * dt)
    price = mu if start is None else start
    out = []
    for k in range(n):
        price = mu + (price - mu) * b + rng.gauss(0.0, sigma_eps)
        out.append((k * dt, price))
    return out


def _feed(strategy, clock, samples, inventory=25.0):
    """One decide_order per sample, the mid on the tick; returns the quotes."""
    quotes = []
    for t, price in samples:
        clock["t"] = t
        quotes.append(strategy.decide_order(_book(round(price / TICK) * TICK), inventory))
    return quotes


# ---------- the estimator (specification section 6.1) ----------

def test_ou_fit_recovers_the_parameters_of_a_simulated_path():
    samples = _ou_path(2_660_000.0, 120.0, 300.0, 3000, 10.0)
    estimate = ou_fit(samples, halflife_min=30.0, halflife_max=600.0)
    assert estimate.ok and estimate.reason == "admissible"
    assert estimate.points == 3000 and estimate.dt_mean == pytest.approx(10.0)
    assert abs(estimate.mu - 2_660_000.0) < 500
    assert abs(estimate.half_life - 120.0) / 120.0 < 0.3
    assert abs(estimate.sigma_eps - 300.0) / 300.0 < 0.1
    assert estimate.t_stat < -2.0
    # sigma of the continuous model: sigma_eps^2 = sigma^2 (1 - b^2) / (2 theta)
    b = math.exp(-estimate.theta * 10.0)
    assert estimate.sigma ** 2 * (1 - b * b) / (2 * estimate.theta) == pytest.approx(estimate.sigma_eps ** 2, rel=1e-6)


def _random_walks_admitted(t_stat_max, seeds=60, points=180):
    admitted = 0
    for seed in range(seeds):
        rng = random.Random(seed)
        price, samples = 2_660_000.0, []
        for _ in range(points):
            price += rng.gauss(0.0, 300.0)
            samples.append((len(samples) * 10.0, price))
        estimate = ou_fit(samples, halflife_min=30.0, halflife_max=86400.0, t_stat_max=t_stat_max)
        admitted += estimate.ok
        assert estimate.reason in ("admissible", "unit_root", "negative_autocorrelation", "half_life_long")
    return admitted


def test_a_random_walk_is_rarely_admissible():
    """A random walk fitted by AR(1) looks mean-reverting more often than
    one would think (the finite-sample bias of b): with the design note's
    rule t(b - 1) < -2 about a third of 180-sample random walks pass, with
    the Dickey-Fuller 5 % critical value -2.86 (the code's rule) about one
    in twenty. The property is statistical: a small minority passes."""
    loose, strict = _random_walks_admitted(-2.0), _random_walks_admitted(-2.86)
    assert strict <= 9 and strict < loose, (strict, loose)


def test_warm_up_and_degenerate_windows():
    assert ou_fit([(k * 10.0, 2_660_000.0 + k) for k in range(5)], halflife_min=1, halflife_max=10).reason == "warming_up"
    flat = [(k * 10.0, 2_660_000.0) for k in range(MIN_POINTS + 5)]
    assert ou_fit(flat, halflife_min=1, halflife_max=10).reason == "degenerate"


def test_half_life_bounds_reject_too_fast_and_too_slow_reversion():
    fast = _ou_path(2_660_000.0, 20.0, 300.0, 600, 10.0, seed=2)
    assert ou_fit(fast, halflife_min=60.0, halflife_max=900.0).reason == "half_life_short"
    slow = _ou_path(2_660_000.0, 120.0, 300.0, 3000, 10.0, seed=4)
    assert ou_fit(slow, halflife_min=10.0, halflife_max=60.0).reason == "half_life_long"


# ---------- the quotes ----------

def test_warm_up_quotes_the_avellaneda_stoikov_pair():
    """The design note returns no quote during the warm-up; here the bot
    quotes without the forecast (the Algorithm-2 reduction) so a restarted
    worker is back in the book at once."""
    strategy = Algo4Strategy(_cfg(), clock=lambda: 0.0)
    quote = strategy.decide_order(_book(2_660_000), 25.0)
    assert quote is not None
    assert quote["selection reason"] == "symmetric_pair" and quote["lean"] == 0.0
    assert quote["forecast admissible"] is False and strategy.estimate().reason == "warming_up"


def test_reduces_to_symmetric_quotes_at_depth_one_over_kappa_plus_the_fee():
    strategy = Algo4Strategy(_cfg(algo4_use_alpha=False, algo4_kappa=400.0, algo4_risk_perc=0.0), clock=lambda: 0.0)
    quote = strategy.decide_order(_book(2_660_000), 25.0)          # x = 0.5: no skew
    depth = 2_660_000 * (1 / 400.0 + 0.0008)                     # 8,778 rial each side
    assert quote["bid"] == pytest.approx(2_660_000 - depth, abs=TICK)
    assert quote["ask"] == pytest.approx(2_660_000 + depth, abs=TICK)
    assert quote["bid depth"] == pytest.approx(quote["ask depth"]) and quote["kappa"] == 400.0
    assert quote["centre"] == pytest.approx(2_660_000.0)


def test_more_inventory_moves_both_quotes_down_and_keeps_the_width():
    cfg = _cfg(algo4_use_alpha=False, skew_perc=0.002)
    low = Algo4Strategy(cfg, clock=lambda: 0.0).decide_order(_book(2_660_000), 5.0)
    high = Algo4Strategy(cfg, clock=lambda: 0.0).decide_order(_book(2_660_000), 45.0)
    assert high["bid"] < low["bid"] and high["ask"] < low["ask"]
    assert high["ask"] - high["bid"] == pytest.approx(low["ask"] - low["bid"], abs=2 * TICK)
    # the skew at the band edges is skew_perc of the price on the centre
    assert (low["centre"] - high["centre"]) / 2_660_000 == pytest.approx(2 * 0.002 * 0.8, rel=1e-6)


def test_the_forecast_leans_the_quotes_toward_the_mean_when_admissible():
    cfg = _cfg(algo4_halflife_min_seconds=30.0, algo4_halflife_max_seconds=600.0, cancel_after_minutes=3)
    clock = {"t": 0.0}
    strategy = Algo4Strategy(cfg, clock=lambda: clock["t"])
    _feed(strategy, clock, _ou_path(2_660_000.0, 60.0, 300.0, 600, 10.0, seed=5))
    estimate = strategy.estimate()
    assert estimate.ok, estimate.reason
    assert abs(estimate.mu - 2_660_000.0) < 1_500 and 30 < estimate.half_life < 600
    clock["t"] += 5.0                                             # inside the sampling interval: no refit
    quote = strategy.decide_order(_book(2_655_000), 25.0)         # 5,000 rial below the mean
    assert quote["selection reason"] == "mean_reversion_pair" and quote["lean"] > 0
    symmetric = Algo4Strategy(_cfg(algo4_use_alpha=False), clock=lambda: 0.0).decide_order(_book(2_655_000), 25.0)
    assert quote["bid"] > symmetric["bid"] and quote["ask"] > symmetric["ask"]     # both shifted up, toward the mean
    assert quote["ask"] - quote["bid"] == pytest.approx(symmetric["ask"] - symmetric["bid"], abs=2 * TICK)   # lean < 1/kappa: the width is untouched
    assert quote["lean"] <= cfg.algo4_lean_clip + 1e-12
    expected = (estimate.mu - 2_655_000.0) * (1 - math.exp(-estimate.theta * 180.0)) / 2_655_000.0
    assert quote["lean"] == pytest.approx(min(expected, cfg.algo4_lean_clip), rel=1e-6)
    # a lean beyond 1/kappa floors the near depth at one tick and widens the pair (the design's floor)
    clock["t"] += 1.0
    far = strategy.decide_order(_book(2_640_000), 25.0)
    assert far["bid depth"] == pytest.approx(TICK / 2_640_000 + 0.0008) and far["ask"] - far["bid"] > quote["ask"] - quote["bid"]


def test_the_lean_is_clipped():
    cfg = _cfg(algo4_halflife_min_seconds=30.0, algo4_halflife_max_seconds=600.0, algo4_lean_clip=0.0002)
    clock = {"t": 0.0}
    strategy = Algo4Strategy(cfg, clock=lambda: clock["t"])
    _feed(strategy, clock, _ou_path(2_660_000.0, 60.0, 300.0, 600, 10.0, seed=5))
    clock["t"] += 5.0                                             # inside the sampling interval: no refit
    quote = strategy.decide_order(_book(2_640_000), 25.0)         # 20,000 rial below the mean
    assert quote["lean"] == pytest.approx(0.0002)
    document = strategy.diagnostics(_book(2_640_000), 25.0, round_no=1, session_id="s")
    assert document["lean_clipped"] is True and document["lean_raw"] > 0.0002 * 2_640_000


def test_the_forecast_can_be_switched_off():
    cfg = _cfg(algo4_use_alpha=False, algo4_halflife_min_seconds=30.0, algo4_halflife_max_seconds=600.0)
    clock = {"t": 0.0}
    strategy = Algo4Strategy(cfg, clock=lambda: clock["t"])
    quotes = _feed(strategy, clock, _ou_path(2_660_000.0, 60.0, 300.0, 400, 10.0, seed=5))
    assert strategy.estimate().ok
    assert all(q["lean"] == 0.0 and q["selection reason"].startswith("symmetric_pair") for q in quotes if q)


def test_the_volatility_guard_stops_quoting():
    cfg = _cfg(algo4_sigma_pause=0.00005)        # 0.005 % of the price; the path's residual is 300 / 2.66 M = 0.011 %
    clock = {"t": 0.0}
    strategy = Algo4Strategy(cfg, clock=lambda: clock["t"])
    quotes = _feed(strategy, clock, _ou_path(2_660_000.0, 60.0, 300.0, 200, 10.0, seed=6))
    assert quotes[-1] is None
    assert strategy.no_quote_reason(_book(2_660_000), 25.0) == "volatility_pause"
    assert quotes[0] is not None                  # nothing to measure during the first rounds


@pytest.mark.parametrize("fee,target", [(0.0008, 0.0002), (0.0025, 0.0001), (0.0025, 0.0025), (0.001, 0.002)])
@pytest.mark.parametrize("kappa", [100.0, 400.0, 2000.0, 100000.0])
def test_every_quote_clears_the_fee_bar_never_crosses_and_sits_on_the_tick(fee, target, kappa):
    rng = random.Random(int(kappa) + int(fee * 1e4))
    strategy = Algo4Strategy(_cfg(commission_perc=fee, exp_ret_min=target, algo4_kappa=kappa, skew_perc=0.002),
                             clock=lambda: 0.0)
    for _ in range(60):
        mid = rng.randrange(1_000_000, 300_000_000, 10)
        spread_ticks = rng.choice([1, 2, 5, 20, 400])
        book = _book(mid, spread_ticks=spread_ticks, depth=rng.randint(1, 6))
        quote = strategy.decide_order(book, rng.uniform(0.0, 50.0))
        assert quote is not None, strategy.no_quote_reason(book, 25.0)
        bid, ask = quote["bid"], quote["ask"]
        best_bid, best_ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        assert bid <= best_ask - TICK + 1e-9 and ask >= best_bid + TICK - 1e-9
        assert abs(bid / TICK - round(bid / TICK)) < 1e-6 and abs(ask / TICK - round(ask / TICK)) < 1e-6
        assert quote["net return"] > target
        assert (1 - fee) ** 2 * ask / bid - 1 > target


def test_a_kappa_too_large_for_the_bar_is_widened_symmetrically():
    strategy = Algo4Strategy(_cfg(algo4_use_alpha=False, algo4_kappa=100000.0), clock=lambda: 0.0)
    quote = strategy.decide_order(_book(2_660_000), 25.0)
    document = strategy.diagnostics(_book(2_660_000), 25.0, round_no=1, session_id="s")
    assert document["widening"] > 0 and quote["net return"] > 0.0002
    assert (2_660_000 - quote["bid"]) == pytest.approx(quote["ask"] - 2_660_000, abs=2 * TICK)


def test_kappa_comes_from_the_fill_table_when_it_can_be_fitted():
    """Buckets generated from P = 1 - exp(-lambda e^{-kappa d} tau) give the
    parameters back; the strategy then quotes at depth 1/kappa_table."""
    kappa_true, lam, tau = 1200.0, 0.002, 180.0
    buckets = []
    for d in (0.0005, 0.001, 0.0015, 0.002, 0.003, 0.004):
        p = 1 - math.exp(-lam * math.exp(-kappa_true * d) * tau)
        buckets.append({"distance": d, "p_bid": p, "p_ask": p, "n_bid": 200, "n_ask": 200})
    table = FillTable.from_document({"pair": "USDT", "horizon_seconds": tau, "buckets": buckets,
                                     "updated_at": "2026-10-22T10:00:00+00:00", "method": "test"})
    fit = table.fit_intensity(50)
    assert fit["kappa"] == pytest.approx(kappa_true, rel=1e-6) and fit["lambda"] == pytest.approx(lam, rel=1e-6)

    class _Model:
        def intensity(self):
            return fit

    strategy = Algo4Strategy(_cfg(algo4_use_alpha=False), clock=lambda: 0.0, fill_model=_Model())
    quote = strategy.decide_order(_book(2_660_000), 25.0)
    assert quote["kappa"] == pytest.approx(kappa_true)
    assert quote["bid depth"] == pytest.approx(1 / kappa_true + 0.0008)
    document = strategy.diagnostics(_book(2_660_000), 25.0, round_no=1, session_id="s")
    assert document["kappa_source"] == "table" and document["fill_lambda"] == pytest.approx(lam)
    assert table.fit_intensity(1000) is None       # not enough observations


def test_a_pair_that_barely_moved_is_kept_until_it_is_refreshed():
    clock = {"t": 0.0}
    strategy = Algo4Strategy(_cfg(algo4_use_alpha=False, hysteresis_ticks=5, requote_band_perc=0.0003, refresh_seconds=300.0),
                             clock=lambda: clock["t"])
    first = strategy.decide_order(_book(2_660_000), 25.0)
    clock["t"] = 10.0
    second = strategy.decide_order(_book(2_660_300), 25.0)        # 300 rial: inside 0.03 %
    assert second["bid kept"] and (second["bid"], second["ask"]) == (first["bid"], first["ask"])
    clock["t"] = 20.0
    third = strategy.decide_order(_book(2_662_000), 25.0)         # 2,000 rial: outside
    assert not third["bid kept"] and third["bid"] > first["bid"]


def test_diagnostics_describe_the_decision_without_touching_the_memory():
    clock = {"t": 0.0}
    strategy = Algo4Strategy(_cfg(hysteresis_ticks=5), clock=lambda: clock["t"])
    _feed(strategy, clock, _ou_path(2_660_000.0, 60.0, 300.0, 50, 10.0, seed=7))
    points, resting = len(strategy._samples), strategy._resting
    document = strategy.diagnostics(_book(2_660_000), 25.0, round_no=4, session_id="s1")
    strategy.no_quote_reason(_book(2_660_000), 25.0)
    assert len(strategy._samples) == points and strategy._resting == resting
    assert document["algorithm"] == "algo4" and document["algorithm_version"] == 4 and document["round"] == 4
    assert document["window"]["points"] == points and len(document["window"]["samples"]) <= 61
    assert set(document["estimate"]) >= {"ok", "reason", "b", "t_stat", "theta", "mu", "half_life", "sigma_eps"}
    assert document["required_threshold"] == pytest.approx(0.0002)
    [pair] = document["candidate_results"]
    assert pair["passed"] is True and pair["submitted_bid_price"] == document["selected_bid_price"]
    assert document["quote_mode"] == "pair" and document["hysteresis"]["resting_bid"] == resting[0]


@pytest.mark.parametrize("book,reason", [
    ({"bids": [], "asks": [["2660010", "1"]]}, "invalid_or_single_level_order_book"),
    ({"bids": [["2660010", "1"]], "asks": [["2659990", "1"]]}, "invalid_or_single_level_order_book"),
    ({"bids": [["0", "1"]], "asks": [["2660010", "1"]]}, "invalid_or_single_level_order_book"),
])
def test_unusable_books_give_no_quote_with_a_reason(book, reason):
    strategy = Algo4Strategy(_cfg(), clock=lambda: 0.0)
    assert strategy.decide_order(book, 25.0) is None
    assert strategy.no_quote_reason(book, 25.0) == reason


def test_algo4_is_a_strategy_with_its_catalog_key_and_branch_name():
    strategy = Algo4Strategy(_cfg())
    assert isinstance(strategy, Strategy) and strategy.key == "algo4"
    assert algorithm_label("algo4") == "MM_algo3"


def test_engine_facing_quote_keys_are_complete():
    quote = Algo4Strategy(_cfg(), clock=lambda: 0.0).decide_order(_book(2_660_000), 25.0)
    for key in ("bid", "ask", "bid level", "ask level", "mid price", "centre", "relative quoted spread",
                "gross return", "net return", "min required return", "bid distance", "ask distance",
                "quote mode", "selection reason", "imbalance", "microprice", "lean", "mu hat", "half life",
                "forecast admissible", "kappa", "bid depth", "ask depth"):
        assert key in quote
    assert isinstance(quote["bid level"], int) and isinstance(quote["ask level"], int)
