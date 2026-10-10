"""Build ``fill_table_<pair>.json`` from the project's own data.

The fill model (fill_model.py) needs, for each relative distance ``delta``
from the quote centre, the probability that a resting order there is filled
within one order lifetime ``tau``. Two estimates are combined:

* **price path** (always available): a bid at ``c_t (1 - delta)`` is filled
  when the best ask comes down to it, so for every sampled moment ``t`` we
  check whether ``min_{t < u <= t + tau} best_ask(u) <= c_t (1 - delta)``;
  asks symmetrically with the best bid rising. The CSVs written by the
  engine carry ``Best Bid`` / ``Best Ask`` per round, which is exactly the
  series needed. Windows that cross a gap in the data (worker stopped,
  HTTP 403) are skipped.
* **own orders** (demo/live rows that carry an order with its limit price
  and outcome): the fraction of orders placed near ``delta`` that filled.

The two are blended per bucket with weight ``w = n_own / (n_own + 100)``
on the own-order estimate; recent days are weighted more (exponential
half-life); the final curve is made non-increasing in distance.

Usage (on the server, from market_maker/):

    python -m tools.build_fill_table --pair usdt \\
        --csv /var/lib/market-maker-forward/results/usdt_track.csv \\
        --csv /var/lib/market-maker-forward/results/usdt_track_demo.csv \\
        --horizon-seconds 600 --out /var/lib/market-maker-forward/results/fill_table_usdt.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

from fill_model import write_fill_table  # noqa: E402

DEFAULT_BUCKETS = (0.0010, 0.0015, 0.0020, 0.0030, 0.0040, 0.0060, 0.0080, 0.0120, 0.0160, 0.0240)
OWN_ORDER_PRIOR_WEIGHT = 100.0


# ---------------------------------------------------------------- loading
def load_price_series(paths: Iterable[str]) -> pd.DataFrame:
    """Timestamp-sorted best bid/ask samples from one or more engine CSVs."""
    frames = []
    for path in paths:
        frame = pd.read_csv(path, low_memory=False)
        if "Timestamp" not in frame.columns or "Best Bid" not in frame.columns or "Best Ask" not in frame.columns:
            continue
        subset = frame[["Timestamp", "Best Bid", "Best Ask"]].copy()
        subset["Timestamp"] = pd.to_datetime(subset["Timestamp"], errors="coerce")
        subset["Best Bid"] = pd.to_numeric(subset["Best Bid"], errors="coerce")
        subset["Best Ask"] = pd.to_numeric(subset["Best Ask"], errors="coerce")
        subset = subset.dropna()
        subset = subset[(subset["Best Bid"] > 0) & (subset["Best Ask"] >= subset["Best Bid"])]
        frames.append(subset)
    if not frames:
        return pd.DataFrame(columns=["Timestamp", "Best Bid", "Best Ask"])
    series = pd.concat(frames, ignore_index=True)
    series = series.drop_duplicates(subset="Timestamp").sort_values("Timestamp").reset_index(drop=True)
    return series


def load_own_orders(paths: Iterable[str]) -> pd.DataFrame:
    """Own resting orders with their distance at placement and outcome, from
    demo/live rows that carry ``<Side> Limit Price`` / ``<Side> Status``."""
    rows = []
    for path in paths:
        frame = pd.read_csv(path, low_memory=False)
        if "Best Bid" not in frame.columns or "Best Ask" not in frame.columns:
            continue
        mid = (pd.to_numeric(frame["Best Bid"], errors="coerce") + pd.to_numeric(frame["Best Ask"], errors="coerce")) / 2.0
        for side, prefix in (("bid", "Buy"), ("ask", "Sell")):
            price_col, status_col, id_col = f"{prefix} Limit Price", f"{prefix} Status", f"{prefix} Order ID"
            if price_col not in frame.columns or status_col not in frame.columns:
                continue
            price = pd.to_numeric(frame[price_col], errors="coerce")
            status = frame[status_col].astype(str).str.lower()
            has_order = frame[id_col].notna() if id_col in frame.columns else price.notna()
            mask = has_order & price.notna() & mid.notna() & (mid > 0)
            if not mask.any():
                continue
            distance = (mid - price) / mid if side == "bid" else (price - mid) / mid
            filled = status.isin({"filled", "partially_filled"})
            terminal = status.isin({"filled", "partially_filled", "cancelled", "canceled", "expired"})
            stamp = pd.to_datetime(frame["Timestamp"], errors="coerce") if "Timestamp" in frame.columns else pd.NaT
            for d, f, t, ts in zip(distance[mask], filled[mask], terminal[mask], stamp[mask] if isinstance(stamp, pd.Series) else [pd.NaT] * int(mask.sum())):
                if not math.isfinite(d) or d <= 0 or not t:
                    continue  # still open: outcome unknown
                rows.append({"side": side, "distance": float(d), "filled": bool(f), "Timestamp": ts})
    if not rows:
        return pd.DataFrame(columns=["side", "distance", "filled", "Timestamp"])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- price path
class _RangeQuery:
    """Sparse table for O(1) range-min / range-max over a numpy array."""

    def __init__(self, values: np.ndarray, op: str):
        self.op = np.minimum if op == "min" else np.maximum
        n = len(values)
        self.levels = [np.asarray(values, dtype=float)]
        k = 1
        while (1 << k) <= n:
            previous = self.levels[-1]
            step = 1 << (k - 1)
            self.levels.append(self.op(previous[:-step], previous[step:]))
            k += 1

    def query(self, lo: int, hi: int) -> float:
        """Aggregate over the half-open index range [lo, hi)."""
        span = hi - lo
        k = span.bit_length() - 1
        level = self.levels[k]
        return float(self.op(level[lo], level[hi - (1 << k)]))


def price_path_counts(
    series: pd.DataFrame, buckets: Sequence[float], horizon_seconds: float,
    *, max_gap_seconds: Optional[float] = None, half_life_days: Optional[float] = None,
    reference_time: Optional[datetime] = None,
) -> Dict[str, Dict[float, Tuple[float, float]]]:
    """Weighted (hits, trials) per bucket and side from the best bid/ask path.

    For sample ``t`` the window is every later sample within ``horizon``;
    the window must reach at least 80% of the horizon and contain no gap
    larger than ``max_gap_seconds`` (default: 3x the median sampling step)
    or the sample is skipped as incomplete."""
    counts = {"bid": {b: (0.0, 0.0) for b in buckets}, "ask": {b: (0.0, 0.0) for b in buckets}}
    n = len(series)
    if n < 3:
        return counts
    times = series["Timestamp"].values.astype("datetime64[ns]").astype("int64") / 1e9
    bids = series["Best Bid"].to_numpy(dtype=float)
    asks = series["Best Ask"].to_numpy(dtype=float)
    mids = (bids + asks) / 2.0
    steps = np.diff(times)
    median_step = float(np.median(steps)) if len(steps) else horizon_seconds
    if max_gap_seconds is None:
        max_gap_seconds = max(3.0 * median_step, 30.0)
    # Largest gap inside [i, j): prefix max of steps lets us check it in O(1).
    gap_query = _RangeQuery(steps, "max") if len(steps) else None
    ask_min = _RangeQuery(asks, "min")
    bid_max = _RangeQuery(bids, "max")
    if reference_time is None:
        reference_time = datetime.fromtimestamp(float(times[-1]), tz=timezone.utc)
    reference_seconds = reference_time.timestamp()
    ends = np.searchsorted(times, times + horizon_seconds, side="right")
    for i in range(n):
        j = int(ends[i])
        if j - i < 2:
            continue
        if times[j - 1] - times[i] < 0.8 * horizon_seconds:
            continue
        if gap_query is not None and gap_query.query(i, j - 1) > max_gap_seconds:
            continue
        weight = 1.0
        if half_life_days and half_life_days > 0:
            age_days = max(0.0, (reference_seconds - times[i]) / 86400.0)
            weight = 0.5 ** (age_days / half_life_days)
        lowest_ask = ask_min.query(i + 1, j)
        highest_bid = bid_max.query(i + 1, j)
        centre = mids[i]
        for b in buckets:
            hit_b, trial_b = counts["bid"][b]
            counts["bid"][b] = (hit_b + weight * (lowest_ask <= centre * (1.0 - b)), trial_b + weight)
            hit_a, trial_a = counts["ask"][b]
            counts["ask"][b] = (hit_a + weight * (highest_bid >= centre * (1.0 + b)), trial_a + weight)
    return counts


def own_order_counts(orders: pd.DataFrame, buckets: Sequence[float]) -> Dict[str, Dict[float, Tuple[float, float]]]:
    """(filled, placed) per bucket and side from our own orders, each order
    assigned to the nearest bucket in log-distance."""
    counts = {"bid": {b: (0.0, 0.0) for b in buckets}, "ask": {b: (0.0, 0.0) for b in buckets}}
    if orders.empty:
        return counts
    log_buckets = np.log(np.asarray(buckets, dtype=float))
    for row in orders.itertuples(index=False):
        if row.distance <= 0:
            continue
        nearest = int(np.argmin(np.abs(log_buckets - math.log(row.distance))))
        bucket = buckets[nearest]
        hit, trial = counts[row.side][bucket]
        counts[row.side][bucket] = (hit + (1.0 if row.filled else 0.0), trial + 1.0)
    return counts


# ---------------------------------------------------------------- blending
def blend(
    path_counts, own_counts, buckets: Sequence[float], *, prior_weight: float = OWN_ORDER_PRIOR_WEIGHT,
    queue_factor: float = 1.0,
) -> List[Dict[str, float]]:
    """Per bucket: p = w * p_own + (1 - w) * p_path * queue_factor, w = n_own / (n_own + prior_weight),
    then made non-increasing in distance (pool-adjacent-violators)."""
    result = []
    for side in ("bid", "ask"):
        probabilities = []
        samples = []
        for b in buckets:
            hit_p, trial_p = path_counts[side][b]
            hit_o, trial_o = own_counts[side][b]
            p_path = (hit_p / trial_p) * queue_factor if trial_p > 0 else None
            p_own = hit_o / trial_o if trial_o > 0 else None
            if p_path is None and p_own is None:
                probabilities.append(None)
                samples.append(0.0)
                continue
            if p_own is None:
                p = p_path
            elif p_path is None:
                p = p_own
            else:
                w = trial_o / (trial_o + prior_weight)
                p = w * p_own + (1.0 - w) * p_path
            probabilities.append(max(0.0, min(1.0, p)))
            samples.append(trial_p + trial_o)
        # isotonic (non-increasing) regression on the defined buckets
        defined = [(i, p, samples[i]) for i, p in enumerate(probabilities) if p is not None]
        if defined:
            blocks = [[i, p, max(w, 1e-9)] for i, p, w in defined]  # index, value, weight
            merged: List[List[float]] = []
            for block in blocks:
                merged.append([block[0], block[0], block[1], block[2]])  # start, end, value, weight
                while len(merged) >= 2 and merged[-2][2] < merged[-1][2]:
                    a, b_ = merged[-2], merged[-1]
                    value = (a[2] * a[3] + b_[2] * b_[3]) / (a[3] + b_[3])
                    merged[-2:] = [[a[0], b_[1], value, a[3] + b_[3]]]
            fitted = {}
            for start, end, value, _ in merged:
                for i, _, _ in defined:
                    if start <= i <= end:
                        fitted[i] = value
            probabilities = [fitted.get(i, p) for i, p in enumerate(probabilities)]
        for i, b in enumerate(buckets):
            entry = next((e for e in result if e["distance"] == b), None)
            if entry is None:
                entry = {"distance": b}
                result.append(entry)
            entry[f"p_{side}"] = probabilities[i] if probabilities[i] is not None else 0.0
            entry[f"n_{side}"] = int(round(samples[i]))
            entry[f"n_{side}_path"] = int(round(path_counts[side][b][1]))
            entry[f"n_{side}_own"] = int(round(own_counts[side][b][1]))
    result.sort(key=lambda e: e["distance"])
    return result


# ---------------------------------------------------------------- CLI
def build(
    pair: str, csv_paths: Sequence[str], horizon_seconds: float, out_path: str, *,
    buckets: Sequence[float] = DEFAULT_BUCKETS, half_life_days: Optional[float] = 3.0,
    window_days: Optional[float] = 14.0, queue_factor: float = 1.0,
) -> Dict:
    series = load_price_series(csv_paths)
    orders = load_own_orders(csv_paths)
    if window_days and not series.empty:
        cutoff = series["Timestamp"].max() - pd.Timedelta(days=window_days)
        series = series[series["Timestamp"] >= cutoff].reset_index(drop=True)
        if not orders.empty and orders["Timestamp"].notna().any():
            orders = orders[(orders["Timestamp"].isna()) | (orders["Timestamp"] >= cutoff)]
    path_counts = price_path_counts(series, buckets, horizon_seconds, half_life_days=half_life_days)
    own_counts = own_order_counts(orders, buckets)
    table = blend(path_counts, own_counts, buckets, queue_factor=queue_factor)
    document = write_fill_table(
        out_path, pair=pair.upper(), horizon_seconds=horizon_seconds,
        method="price_path+own_orders", buckets=table,
        extra={
            "samples": int(len(series)),
            "own_orders": int(len(orders)),
            "window_days": window_days,
            "half_life_days": half_life_days,
            "queue_factor": queue_factor,
            "sources": [os.path.basename(p) for p in csv_paths],
            "first_sample": series["Timestamp"].min().isoformat() if not series.empty else None,
            "last_sample": series["Timestamp"].max().isoformat() if not series.empty else None,
        },
    )
    return document


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the fill-probability table for one pair.")
    parser.add_argument("--pair", required=True, help="e.g. usdt or btc (used in the file name and document)")
    parser.add_argument("--csv", action="append", required=True, help="engine CSV with Best Bid / Best Ask columns; repeatable")
    parser.add_argument("--horizon-seconds", type=float, required=True, help="order lifetime the probabilities refer to")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--window-days", type=float, default=14.0)
    parser.add_argument("--half-life-days", type=float, default=3.0)
    parser.add_argument("--queue-factor", type=float, default=1.0, help="multiplier on the price-path estimate (e.g. 0.86 after calibration)")
    args = parser.parse_args(argv)
    document = build(
        args.pair, args.csv, args.horizon_seconds, args.out,
        half_life_days=args.half_life_days, window_days=args.window_days,
        queue_factor=args.queue_factor,
    )
    print(json.dumps({k: v for k, v in document.items() if k != "buckets"}, indent=2, sort_keys=True))
    print(f"{'distance':>10} {'p_bid':>7} {'p_ask':>7} {'n_bid':>8} {'n_ask':>8}")
    for bucket in document["buckets"]:
        print(f"{bucket['distance']*100:9.2f}% {bucket['p_bid']:7.3f} {bucket['p_ask']:7.3f} {bucket['n_bid']:8d} {bucket['n_ask']:8d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
