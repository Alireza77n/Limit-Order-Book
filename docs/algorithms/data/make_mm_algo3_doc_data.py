"""Every number quoted in docs/algorithms/MM_algo3.md, computed from recorded data and the
strategy's own code (strategy/algo4.py), so the document can be regenerated and checked.

Inputs (this folder):
  mids_usdtirt_20261015_17.csv.gz   best bid / best ask of every USDT demo round, 10 s apart
  mids_btcirt_20261012_17.csv.gz    the same for BTC, 60 s apart
  fill_table_usdtirt_20261017.json  the fill tables of 17 October 2026 (horizon 600 s)
  fill_table_btcirt_20261017.json
Output: mm_algo3_doc_numbers.json.

Run from the repository root with market_maker on the path:
  PYTHONPATH=market_maker python docs/algorithms/data/make_mm_algo3_doc_data.py
"""
from __future__ import annotations

import gzip
import json
import math
import random
import statistics
from pathlib import Path

import pandas as pd

from config import MMConfig, TRADING_PROFILES
from fill_model import FillTable
from strategy.algo4 import MIN_POINTS, T_STAT_MAX, Algo4Strategy, ou_fit

HERE = Path(__file__).resolve().parent
WINDOW, SAMPLE, HORIZON = 1800.0, 10.0, 180.0
HALFLIFE = (60.0, 900.0)
TICK = 10.0
DEMO = dict(commission_perc=0.0008, exp_ret_min=0.0002, skew_perc=0.002, algo4_kappa=400.0,   # kappa replaced below by the fill-table fit
            algo4_risk_perc=0.0, algo4_lean_clip=0.005, algo4_window_seconds=WINDOW,
            algo4_sample_seconds=SAMPLE, algo4_horizon_seconds=HORIZON,
            algo4_halflife_min_seconds=HALFLIFE[0], algo4_halflife_max_seconds=HALFLIFE[1],
            hysteresis_ticks=0, requote_band_perc=0.0, fill_table_enabled=False)


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


# ---------------------------------------------------------------- 1. simulated paths: the admissibility rule
def random_walks(t_stat_max, seeds=200, points=180, sigma=300.0):
    admitted = 0
    for seed in range(seeds):
        rng = random.Random(seed)
        price, samples = 2_660_000.0, []
        for k in range(points):
            price += rng.gauss(0.0, sigma)
            samples.append((k * SAMPLE, price))
        admitted += ou_fit(samples, halflife_min=HALFLIFE[0], halflife_max=86400.0, t_stat_max=t_stat_max).ok
    return admitted / seeds


def ou_paths(half_life, seeds=200, points=180, sigma=300.0):
    admitted, half_lives, mus = 0, [], []
    b = math.exp(-math.log(2.0) / half_life * SAMPLE)
    for seed in range(seeds):
        rng = random.Random(seed)
        price, samples = 2_660_000.0, []
        for k in range(points):
            price = 2_660_000.0 + (price - 2_660_000.0) * b + rng.gauss(0.0, sigma)
            samples.append((k * SAMPLE, price))
        estimate = ou_fit(samples, halflife_min=HALFLIFE[0], halflife_max=HALFLIFE[1])
        admitted += estimate.ok
        if estimate.ok:
            half_lives.append(estimate.half_life)
            mus.append(estimate.mu - 2_660_000.0)
    return {"half_life": half_life, "admitted": admitted / seeds,
            "median_half_life_hat": statistics.median(half_lives) if half_lives else None,
            "median_abs_mu_error": statistics.median(abs(m) for m in mus) if mus else None}


simulation = {
    "points": 180, "sample_seconds": SAMPLE, "sigma_eps": 300.0, "seeds": 200,
    "random_walk_admitted_t2": random_walks(-2.0),
    "random_walk_admitted_t286": random_walks(T_STAT_MAX),
    "t_stat_max": T_STAT_MAX,
    "ou": [ou_paths(h) for h in (60.0, 120.0, 300.0)],
}


# ---------------------------------------------------------------- 2. the recorded demo mids
def load_series(name):
    with gzip.open(HERE / name, "rt", encoding="utf-8") as stream:
        df = pd.read_csv(stream)
    df["t"] = pd.to_datetime(df["timestamp"]).astype("datetime64[ns]").astype("int64") / 1e9
    df["mid"] = (df["best_bid"] + df["best_ask"]) / 2.0
    return df.reset_index(drop=True)


def rolling_fit(df, step_seconds):
    """ou_fit on a 30-minute window ending at every step; the lean over the
    horizon and the realised move of the mid over the same horizon."""
    times, mids = df["t"].to_numpy(), df["mid"].to_numpy()
    rows = []
    start = times[0] + WINDOW
    next_end = start
    i0 = 0
    for i in range(len(times)):
        if times[i] < next_end:
            continue
        next_end = times[i] + step_seconds
        while times[i0] < times[i] - WINDOW:
            i0 += 1
        samples = list(zip(times[i0:i + 1], mids[i0:i + 1]))
        estimate = ou_fit(samples, halflife_min=HALFLIFE[0], halflife_max=HALFLIFE[1])
        j = i
        while j + 1 < len(times) and times[j + 1] <= times[i] + HORIZON:
            j += 1
        if times[j] - times[i] < HORIZON * 0.8:
            continue
        move = mids[j] - mids[i]
        lean = None
        if estimate.mu is not None and estimate.theta is not None:
            lean = (estimate.mu - mids[i]) * (1.0 - math.exp(-estimate.theta * HORIZON))
        rows.append({"t": times[i], "mid": mids[i], "ok": estimate.ok, "reason": estimate.reason,
                     "b": estimate.b, "t_stat": estimate.t_stat, "half_life": estimate.half_life,
                     "sigma_eps": estimate.sigma_eps, "lean": lean, "move": move, "points": estimate.points})
    return rows


def summarise(rows, pair):
    fitted = [r for r in rows if r["b"] is not None]
    admissible = [r for r in rows if r["ok"]]
    reasons = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    out = {
        "pair": pair, "windows": len(rows), "fitted": len(fitted), "admissible": len(admissible),
        "admissible_share": len(admissible) / len(rows) if rows else None,
        "reasons": reasons,
        "median_b": statistics.median(r["b"] for r in fitted) if fitted else None,
        "median_t_stat": statistics.median(r["t_stat"] for r in fitted if r["t_stat"] is not None and math.isfinite(r["t_stat"])) if fitted else None,
        "median_half_life_admissible": statistics.median(r["half_life"] for r in admissible) if admissible else None,
        "median_sigma_eps_rel": statistics.median(r["sigma_eps"] / r["mid"] for r in fitted) if fitted else None,
    }
    if admissible:
        leans = [r["lean"] for r in admissible]
        moves = [r["move"] for r in admissible]
        out["skill_corr_admissible"] = pearson(leans, moves)
        out["sign_agreement_admissible"] = sum(1 for l, m in zip(leans, moves) if l * m > 0) / len(leans)
        out["median_abs_lean_rel_admissible"] = statistics.median(abs(r["lean"]) / r["mid"] for r in admissible)
        out["median_abs_move_rel_admissible"] = statistics.median(abs(r["move"]) / r["mid"] for r in admissible)
    leaning = [r for r in fitted if r["lean"] is not None]
    if leaning:
        out["skill_corr_every_fit"] = pearson([r["lean"] for r in leaning], [r["move"] for r in leaning])
    return out


series = {}
rows_by_pair = {}
for pair, name, step in (("usdt", "mids_usdtirt_20261015_17.csv.gz", 60.0), ("btc", "mids_btcirt_20261012_17.csv.gz", 300.0)):
    df = load_series(name)
    rows = rolling_fit(df, step)
    rows_by_pair[pair] = rows
    series[pair] = summarise(rows, pair)
    series[pair].update({"first": df["timestamp"].iloc[0], "last": df["timestamp"].iloc[-1], "rounds": int(len(df)),
                         "spacing_seconds": float(statistics.median(df["t"].diff().dropna()))})


# ---------------------------------------------------------------- 3. kappa from the fill tables
kappa = {}
for pair in ("usdt", "btc"):
    document = json.loads((HERE / f"fill_table_{pair}irt_20261017.json").read_text(encoding="utf-8"))
    table = FillTable.from_document(document)
    fit = table.fit_intensity(50)
    kappa[pair] = {"horizon_seconds": table.horizon_seconds, "updated_at": document.get("updated_at"),
                   "buckets": len(table.buckets), "fit": fit,
                   "one_over_kappa_perc": (100.0 / fit["kappa"]) if fit else None,
                   "sample": [{"distance": b.distance, "p_bid": b.p_bid, "p_ask": b.p_ask, "n_bid": b.n_bid, "n_ask": b.n_ask}
                              for b in table.buckets[:6]]}


# ---------------------------------------------------------------- 4. a worked example on a real USDT window
def book_of(best_bid, best_ask):
    return {"bids": [[str(best_bid), "100"], [str(best_bid - 1000), "100"]],
            "asks": [[str(best_ask), "100"], [str(best_ask + 1000), "100"]], "lastTradePrice": str((best_bid + best_ask) / 2)}


usdt = load_series("mids_usdtirt_20261015_17.csv.gz")
# the admissible window with the largest lean below the clip, for a telling example
candidates = [r for r in rows_by_pair["usdt"] if r["ok"] and r["lean"] is not None and abs(r["lean"]) / r["mid"] < 0.004]
chosen = max(candidates, key=lambda r: abs(r["lean"]))
end = chosen["t"]
window = usdt[(usdt["t"] > end - WINDOW) & (usdt["t"] <= end)]
clock = {"t": 0.0}
cfg = MMConfig(**TRADING_PROFILES["usdt"])
for key, value in DEMO.items():
    setattr(cfg, key, value)
cfg.inv_min, cfg.inv_max, cfg.order_size, cfg.cancel_after_minutes = 0.0, 300.0, 5.0, 3
# kappa as the strategy would take it from the fresh USDT fill table (section 3 above)
cfg.algo4_kappa = kappa["usdt"]["fit"]["kappa"] if kappa["usdt"]["fit"] else 400.0
examples = {}
for inventory in (150.0, 50.0):
    strategy = Algo4Strategy(cfg, clock=lambda: clock["t"])
    for t, bid, ask in zip(window["t"], window["best_bid"], window["best_ask"]):
        clock["t"] = float(t)
        strategy.decide_order(book_of(float(bid), float(ask)), inventory)
    last = window.iloc[-1]
    clock["t"] = float(last["t"])
    document = strategy.diagnostics(book_of(float(last["best_bid"]), float(last["best_ask"])), inventory, round_no=0, session_id="doc")
    quote = strategy.decide_order(book_of(float(last["best_bid"]), float(last["best_ask"])), inventory)
    examples[f"inventory_{int(inventory)}"] = {
        "inventory": inventory, "position": document["inventory_position"],
        "estimate": document["estimate"], "window_points": document["window"]["points"],
        "mid": document["mid_price"], "best_bid": float(last["best_bid"]), "best_ask": float(last["best_ask"]),
        "lean": document["lean"], "lean_raw": document["lean_raw"], "skew": document["skew"],
        "kappa": document["kappa"], "base_depth": document["base_depth"], "depth_bid": document["depth_bid"], "depth_ask": document["depth_ask"],
        "centre": document["centre"], "raw_bid": document["raw_bid"], "raw_ask": document["raw_ask"],
        "bid": quote["bid"], "ask": quote["ask"], "widening": document["widening"],
        "net_return": quote["net return"], "gross_return": quote["gross return"], "required": quote["min required return"],
        "selection_reason": quote["selection reason"],
    }
    # the same book without the forecast (the Avellaneda-Stoikov reduction)
    plain = Algo4Strategy(cfg, clock=lambda: clock["t"])
    plain_cfg_lean = cfg.algo4_use_alpha
    cfg.algo4_use_alpha = False
    plain_quote = plain.decide_order(book_of(float(last["best_bid"]), float(last["best_ask"])), inventory)
    cfg.algo4_use_alpha = plain_cfg_lean
    examples[f"inventory_{int(inventory)}"]["without_forecast"] = {"bid": plain_quote["bid"], "ask": plain_quote["ask"], "net_return": plain_quote["net return"]}
example = {"timestamp": pd.to_datetime(end, unit="s").strftime("%Y-%m-%d %H:%M:%S"), "settings": dict(DEMO, algo4_kappa=cfg.algo4_kappa),
           "inv_min": 0.0, "inv_max": 300.0, "order_size": 5.0, "cases": examples,
           "realised_move_180s": chosen["move"]}

numbers = {"generated_from": ["mids_usdtirt_20261015_17.csv.gz", "mids_btcirt_20261012_17.csv.gz",
                              "fill_table_usdtirt_20261017.json", "fill_table_btcirt_20261017.json"],
           "rules": {"window_seconds": WINDOW, "sample_seconds": SAMPLE, "horizon_seconds": HORIZON,
                     "halflife_bounds": HALFLIFE, "min_points": MIN_POINTS, "t_stat_max": T_STAT_MAX},
           "simulation": simulation, "series": series, "kappa": kappa, "example": example}
(HERE / "mm_algo3_doc_numbers.json").write_text(json.dumps(numbers, indent=2, default=float), encoding="utf-8")
print(json.dumps({"simulation": simulation, "series": {p: {k: v for k, v in s.items() if k != "reasons"} for p, s in series.items()},
                  "kappa": {p: {"fit": k["fit"], "one_over_kappa_perc": k["one_over_kappa_perc"]} for p, k in kappa.items()},
                  "example": {"timestamp": example["timestamp"], "case150": {k: examples["inventory_150"][k] for k in ("lean", "bid", "ask", "net_return", "selection_reason")}}},
                 indent=1, default=float))
