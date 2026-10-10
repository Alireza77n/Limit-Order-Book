"""tools/build_fill_table.py on synthetic price paths with known answers."""

import json
import math

import numpy as np
import pandas as pd
import pytest

from tools.build_fill_table import (
    DEFAULT_BUCKETS, blend, build, load_own_orders, load_price_series,
    own_order_counts, price_path_counts,
)


def _series(prices, step_seconds=10, start="2026-10-01 00:00:00", spread=20.0):
    stamps = pd.date_range(start, periods=len(prices), freq=f"{step_seconds}s")
    mids = np.asarray(prices, dtype=float)
    return pd.DataFrame({"Timestamp": stamps, "Best Bid": mids - spread / 2, "Best Ask": mids + spread / 2})


def test_price_path_counts_a_hit_only_when_the_touch_reaches_the_level():
    # Flat at 1,000,000 then a dip of 0.3 % for one sample, then flat again.
    prices = [1_000_000.0] * 30 + [997_000.0] + [1_000_000.0] * 30
    series = _series(prices)
    counts = price_path_counts(series, (0.001, 0.002, 0.005), horizon_seconds=100, half_life_days=None)
    # samples whose 100 s window (10 later samples) contains index 30: i in [20, 29]
    hit, trials = counts["bid"][0.001]
    assert hit == pytest.approx(10)
    assert trials > 20
    assert counts["bid"][0.005][0] == 0            # the dip never reached 0.5 %
    # seen from the dip sample itself the best bid "rose" back by 0.3 %: one ask hit
    assert counts["ask"][0.001][0] == pytest.approx(1)
    assert counts["bid"][0.002][0] == pytest.approx(10)


def test_windows_across_a_gap_are_skipped():
    first = _series([1_000_000.0] * 20)
    second = _series([900_000.0] * 20, start="2026-10-01 06:00:00")   # 6 h hole, then a lower regime
    series = pd.concat([first, second], ignore_index=True)
    counts = price_path_counts(series, (0.001,), horizon_seconds=100, half_life_days=None)
    # the drop across the gap must not count as a fill for the samples before it
    assert counts["bid"][0.001][0] == 0


def test_recent_samples_weigh_more_with_a_half_life():
    prices = [1_000_000.0] * 2000
    series = _series(prices, step_seconds=60)              # 2000 minutes
    unweighted = price_path_counts(series, (0.001,), horizon_seconds=600, half_life_days=None)
    weighted = price_path_counts(series, (0.001,), horizon_seconds=600, half_life_days=0.5)
    assert weighted["bid"][0.001][1] < unweighted["bid"][0.001][1]


def test_own_orders_are_bucketed_by_nearest_log_distance():
    orders = pd.DataFrame([
        {"side": "bid", "distance": 0.0011, "filled": True, "Timestamp": pd.NaT},
        {"side": "bid", "distance": 0.0009, "filled": False, "Timestamp": pd.NaT},
        {"side": "ask", "distance": 0.0031, "filled": True, "Timestamp": pd.NaT},
    ])
    counts = own_order_counts(orders, (0.001, 0.003))
    assert counts["bid"][0.001] == (1.0, 2.0)
    assert counts["ask"][0.003] == (1.0, 1.0)


def test_blend_weights_own_orders_by_sample_size_and_is_monotone():
    buckets = (0.001, 0.002, 0.004)
    path = {"bid": {0.001: (60.0, 100.0), 0.002: (50.0, 100.0), 0.004: (10.0, 100.0)},
            "ask": {0.001: (60.0, 100.0), 0.002: (40.0, 100.0), 0.004: (10.0, 100.0)}}
    own = {"bid": {0.001: (0.0, 0.0), 0.002: (90.0, 100.0), 0.004: (0.0, 0.0)},
           "ask": {b: (0.0, 0.0) for b in buckets}}
    table = blend(path, own, buckets)
    by = {row["distance"]: row for row in table}
    # 0.002 bid: w = 100/200 -> 0.5*0.9 + 0.5*0.5 = 0.7, but monotonicity pools it with 0.001 (0.6)
    assert by[0.001]["p_bid"] >= by[0.002]["p_bid"] >= by[0.004]["p_bid"]
    assert by[0.002]["n_bid"] == 200 and by[0.002]["n_bid_own"] == 100
    assert by[0.001]["p_ask"] == pytest.approx(0.6)
    assert by[0.004]["p_bid"] == pytest.approx(0.1)


def test_load_price_series_merges_sorts_and_drops_bad_rows(tmp_path):
    a = _series([1_000_000.0] * 5)
    b = _series([1_000_000.0] * 5, start="2026-10-01 00:00:20")   # overlaps a by 3 samples
    a.loc[2, "Best Ask"] = -1                                       # bad row
    a.to_csv(tmp_path / "a.csv", index=False)
    b.to_csv(tmp_path / "b.csv", index=False)
    series = load_price_series([str(tmp_path / "a.csv"), str(tmp_path / "b.csv")])
    assert len(series) == 7                         # 0..60 s, duplicates merged, bad row dropped
    assert len(series) == series["Timestamp"].nunique()
    assert series["Timestamp"].is_monotonic_increasing


def test_load_own_orders_reads_demo_rows(tmp_path):
    frame = pd.DataFrame([
        {"Timestamp": "2026-10-01 00:00:00", "Best Bid": 999_990, "Best Ask": 1_000_010,
         "Buy Order ID": "b1", "Buy Limit Price": 997_000, "Buy Status": "filled",
         "Sell Order ID": None, "Sell Limit Price": None, "Sell Status": None},
        {"Timestamp": "2026-10-01 00:00:10", "Best Bid": 999_990, "Best Ask": 1_000_010,
         "Buy Order ID": "b2", "Buy Limit Price": 995_000, "Buy Status": "open",
         "Sell Order ID": "s1", "Sell Limit Price": 1_005_000, "Sell Status": "cancelled"},
    ])
    frame.to_csv(tmp_path / "demo.csv", index=False)
    orders = load_own_orders([str(tmp_path / "demo.csv")])
    assert len(orders) == 2                          # the open order has no outcome yet
    assert set(orders["side"]) == {"bid", "ask"}
    assert orders[orders["side"] == "bid"]["filled"].iloc[0]
    assert orders[orders["side"] == "bid"]["distance"].iloc[0] == pytest.approx(0.003)


def test_build_writes_a_loadable_table(tmp_path):
    rng = np.random.default_rng(1)
    prices = 1_000_000 * np.exp(np.cumsum(rng.normal(0, 0.0005, 3000)))
    series = _series(prices)
    series.to_csv(tmp_path / "usdt_track.csv", index=False)
    out = tmp_path / "fill_table_usdtirt.json"
    document = build("usdt", [str(tmp_path / "usdt_track.csv")], 600, str(out), buckets=DEFAULT_BUCKETS)
    loaded = json.loads(out.read_text())
    assert loaded["pair"] == "USDT" and loaded["horizon_seconds"] == 600
    probabilities = [b["p_bid"] for b in loaded["buckets"]]
    assert probabilities == sorted(probabilities, reverse=True)
    assert all(0.0 <= p <= 1.0 for p in probabilities)
    assert loaded["samples"] == 3000
    from fill_model import FillTable
    assert FillTable.from_document(loaded).lookup(0.001, "bid", 1) is not None
    assert document["buckets"] == loaded["buckets"]
