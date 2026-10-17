"""Every number in the MM_algo2 documentation (docs/algorithms/MM_algo2.md, README.md,
PROJECT_GUIDE.md, docs/guide/MARKET_MAKER_EXPLAINED.*) is produced by this script, which runs
the real code of this branch (strategy/algo3.py, the engine's sizing rule, the config modules)
on the real Nobitex order books stored next to it:

  orderbooks_20261003.jsonl.gz          578 snapshots per pair, 3 Oct 2026 14:31-16:23 UTC, ~10 s apart
  orderbooks_20261017_btcirt.jsonl.gz   60 BTC/IRT books captured from the server, 17 Oct 2026
  orderbooks_20261017_usdtirt.jsonl.gz  60 USDT/IRT books captured from the server, 17 Oct 2026
  demo_settings_20261017.json           the settings both demo workers had applied on 17 Oct 2026
  live_settings_20261017.json           the saved live settings (Live Config pages) on 17 Oct 2026
  btc_demo_mm_algo2_rounds.json         the BTC demo's MM_algo2 rounds: diagnostics + round rows

Output: mm_algo2_doc_numbers.json. Run from anywhere: python docs/algorithms/data/make_mm_algo2_doc_data.py"""
import gzip
import json
import math
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "market_maker"))
try:
    import fcntl  # noqa: F401
except ImportError:  # Windows: the engine's file locks are not needed to compute these numbers
    shim = types.ModuleType("fcntl")
    shim.LOCK_SH, shim.LOCK_EX, shim.LOCK_NB, shim.LOCK_UN = 1, 2, 4, 8
    shim.flock = shim.lockf = lambda *args, **kwargs: None
    sys.modules["fcntl"] = shim

import demo_config  # noqa: E402
import forward_config  # noqa: E402
from config import MMConfig, TRADING_PROFILES  # noqa: E402
from engine import MIN_ORDER_SIZE_FRACTION, SimpleMarketMaker  # noqa: E402
from strategy.algo3 import Algo3Strategy  # noqa: E402

context = json.loads((HERE / "demo_settings_20261017.json").read_text(encoding="utf-8"))
live_saved = json.loads((HERE / "live_settings_20261017.json").read_text(encoding="utf-8"))
out = {"inputs": sorted(p.name for p in HERE.glob("*") if p.suffix in (".gz", ".json")
                        and p.name != "mm_algo2_doc_numbers.json" and not p.name.startswith("dashboard_metrics"))}


def cfg_with(pair, values):
    cfg = MMConfig(**TRADING_PROFILES[pair])
    for key, value in values.items():
        if hasattr(cfg, key) and value is not None and key != "algorithm":
            setattr(cfg, key, value)
    cfg.algorithm = "algo3"
    return cfg


def book_of(snap):
    return {"bids": snap["bids"], "asks": snap["asks"], "lastTradePrice": snap.get("last_trade_price")}


def ts_of(snap):
    return datetime.fromisoformat(snap["t"]).timestamp()


def load_jsonl(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


DEMO = {pair: cfg_with(pair, context[f"{pair}_applied"]) for pair in ("btc", "usdt")}
LIVE = {pair: cfg_with(pair, live_saved[pair]) for pair in ("btc", "usdt")}
SETTING_KEYS = ("commission_perc", "exp_ret_min", "inv_min", "inv_max", "order_size", "skew_perc",
                "hysteresis_ticks", "refresh_seconds", "time_step", "cancel_after_minutes", "depth_max",
                "algo3_depth_k", "algo3_decay_rho", "algo3_beta", "algo3_half_spread",
                "algo3_min_volume_orders", "algo3_imbalance_clip", "inventory_sizing_enabled",
                "inventory_sizing_strength", "inventory_sizing_min_fraction",
                "buy_locked_cash_limit_enabled", "buy_locked_cash_limit_ratio",
                "sell_locked_inventory_limit_enabled", "sell_locked_inventory_limit_ratio")
out["settings"] = {f"{mode}_{pair}": {k: getattr(cfg, k, None) for k in SETTING_KEYS}
                   for mode, table in (("demo", DEMO), ("live", LIVE)) for pair, cfg in table.items()}
out["settings"]["profile_defaults"] = {pair: {k: TRADING_PROFILES[pair].get(k) for k in SETTING_KEYS}
                                       for pair in ("btc", "usdt")}
out["live_algorithm_saved"] = {pair: live_saved[pair].get("algorithm") for pair in ("btc", "usdt")}
out["demo_newest_rounds"] = {pair: context[f"{pair}_newest_round"] for pair in ("btc", "usdt")}

# ---------------------------------------------------------------- A. fee bar
fee_rows = []
for label, f, r in (("live (both pairs)", 0.0025, 0.0001), ("demo (both pairs)", 0.0008, 0.0001),
                    ("profile defaults", 0.0010, 0.0010), ("design-note example", 0.0025, 0.0025)):
    cfg = MMConfig(**TRADING_PROFILES["usdt"])
    cfg.commission_perc, cfg.exp_ret_min = f, r
    s = Algo3Strategy(cfg)
    h_min = s.minimum_half_spread()
    ratio = (1 + h_min) / (1 - h_min)
    fee_rows.append(dict(label=label, f=f, r_min=r, G=s.gross_bar(), h_min=h_min, ratio=ratio,
                         r_at_h_min=(1 - f) ** 2 * ratio - 1))
out["fee_bar"] = fee_rows


def evaluation_dict(strategy, ob, inventory):
    ev = strategy.evaluate(ob, inventory)
    keys = ("ok", "reason", "mid", "spread", "last", "volume_bid", "volume_ask", "min_volume", "thin_book",
            "imbalance_raw", "imbalance", "imbalance_level0", "imbalance_clip", "microprice", "position",
            "beta", "skew_perc", "lean_factor", "skew_factor", "centre", "h_min", "h_config", "tick_fraction",
            "h", "raw_bid", "raw_ask", "bid", "ask", "clipped_bid", "clipped_ask", "bid_level", "ask_level",
            "gross_return", "net_return", "relative_quoted_spread")
    d = {k: getattr(ev, k) for k in keys}
    d["levels"] = ev.levels
    return d


def sized(cfg, inventory, open_buy=0.0, open_sell=0.0):
    """The engine's own linear sizing rule (SimpleMarketMaker._inventory_sized_order_targets)."""
    fake = SimpleNamespace(cfg=cfg, _clamp=SimpleMarketMaker._clamp)
    return SimpleMarketMaker._inventory_sized_order_targets(fake, inventory, open_buy, open_sell)


# ---------------------------------------------------------------- B. the main example: a USDT book of today
usdt_today = load_jsonl(HERE / "orderbooks_20261017_usdtirt.jsonl.gz")
btc_today = load_jsonl(HERE / "orderbooks_20261017_btcirt.jsonl.gz")


def pick_example(snaps, cfg):
    """Deterministic choice: among books that are not thin, whose imbalance is not clipped and whose
    best spread is at least two ticks, the one whose |raw imbalance| is closest to the median of
    those books (a typical book; the clip is shown on the real demo round instead)."""
    s = Algo3Strategy(cfg)
    rows = []
    for i, snap in enumerate(snaps):
        ev = s.evaluate(book_of(snap), (cfg.inv_min + cfg.inv_max) / 2)
        if ev.ok and not ev.thin_book and abs(ev.imbalance_raw) < s.imbalance_clip():
            rows.append((i, abs(ev.imbalance_raw), ev.spread / s.tick_size()))
    candidates = [r for r in rows if r[2] >= 2] or rows
    target = float(np.median([r[1] for r in candidates]))
    return min(candidates, key=lambda r: (abs(r[1] - target), r[0]))[0]


cfg_u = DEMO["usdt"]
inv_u = float(context["usdt_newest_round"]["Available Inventory"])
idx = pick_example(usdt_today, cfg_u)
snap = usdt_today[idx]
ob = book_of(snap)
strategy = Algo3Strategy(cfg_u)
main = {"t": snap["t"], "index": idx, "n_books": len(usdt_today), "inventory": inv_u,
        "book_top": {"bids": snap["bids"][:8], "asks": snap["asks"][:8]},
        "evaluation": evaluation_dict(strategy, ob, inv_u),
        "quote": strategy.decide_order(ob, inv_u)}
variants = {}
for label, change, inventory in (("beta_0.001", {"algo3_beta": 0.001}, inv_u),
                                 ("x_0.5", {}, (cfg_u.inv_min + cfg_u.inv_max) / 2),
                                 ("x_1", {}, cfg_u.inv_max),
                                 ("x_0", {}, cfg_u.inv_min),
                                 ("k1_rho1", {"algo3_depth_k": 1, "algo3_decay_rho": 1.0}, inv_u),
                                 ("half_spread_0.2pct", {"algo3_half_spread": 0.002}, inv_u)):
    cfg = cfg_with("usdt", {**context["usdt_applied"], **change})
    variants[label] = evaluation_dict(Algo3Strategy(cfg), ob, inventory)
main["variants"] = variants
buy_t, sell_t = sized(cfg_u, inv_u)
main["sizing_no_open_orders"] = {"buy": buy_t, "sell": sell_t, "min_size": MIN_ORDER_SIZE_FRACTION * cfg_u.order_size,
                                 "x": Algo3Strategy(cfg_u).inventory_position(inv_u)}
out["main_example"] = main

# ---------------------------------------------------------------- C. a real BTC demo round
demo = json.loads((HERE / "btc_demo_mm_algo2_rounds.json").read_text(encoding="utf-8"))
paired = [p for p in demo["rounds"] if p["row"]]
out["demo_rounds_available"] = {"n_docs": demo["n_docs"], "n_rows": demo["n_rows"],
                                "first": paired[0]["row"]["Timestamp"] if paired else None,
                                "last": paired[-1]["row"]["Timestamp"] if paired else None}
real = []
for p in paired:
    doc, row = p["diagnostics"], p["row"]
    levels = doc["levels"]
    ob_r = {"bids": [[lv["bid"], lv["bid_volume"]] for lv in levels],
            "asks": [[lv["ask"], lv["ask_volume"]] for lv in levels], "lastTradePrice": doc.get("last_price")}
    re_eval = evaluation_dict(Algo3Strategy(DEMO["btc"]), ob_r, float(doc["inventory"]))
    real.append({
        "round": doc["round"], "session": doc["session_id"], "time": row.get("Timestamp"),
        "doc": {k: doc.get(k) for k in ("inventory", "inventory_position", "mid_price", "spread", "last_price",
                                         "volume_bid", "volume_ask", "min_volume", "thin_book", "imbalance_raw",
                                         "imbalance", "imbalance_level0", "imbalance_clip", "microprice",
                                         "microprice_shift", "beta", "lean_factor", "skew_perc", "skew_factor",
                                         "centre", "centre_shift", "gross_bar", "h_min", "h_config", "tick_fraction",
                                         "h", "raw_bid", "raw_ask", "clipped_bid", "clipped_ask",
                                         "selected_bid_price", "selected_ask_price", "selected_bid_level",
                                         "selected_ask_level", "net_return", "gross_return",
                                         "relative_quoted_spread", "selection_reason", "hysteresis")},
        "levels": levels,
        "row": {k: row.get(k) for k in ("Timestamp", "Round", "Strategy", "Quote Status", "Selection Reason",
                                         "Best Bid", "Best Ask", "Bid Price", "Ask Price", "Bid Level", "Ask Level",
                                         "Centre", "Imbalance", "Microprice", "Net Return",
                                         "inv_current", "cash_current", "Available Inventory", "Locked Inventory",
                                         "Available Cash", "Locked Cash", "Portfolio Wealth",
                                         "Buy Status", "Buy Not Sent Reason", "Buy Original Amount", "Buy Limit Price",
                                         "Buy Matched Amount", "Sell Status", "Sell Not Sent Reason",
                                         "Sell Original Amount", "Sell Limit Price", "Sell Matched Amount")},
        "recomputed": {k: re_eval[k] for k in ("imbalance_raw", "imbalance", "microprice", "centre", "h",
                                               "raw_bid", "raw_ask", "bid", "ask", "net_return", "ok", "reason")},
        "matches": (re_eval["bid"] == doc.get("selected_bid_price") and re_eval["ask"] == doc.get("selected_ask_price")),
    })
out["demo_rounds"] = real

# ---------------------------------------------------------------- D. behaviour on real books
def behaviour(snaps, cfg, label):
    s = Algo3Strategy(cfg, clock=lambda: 0.0)
    eps = s.tick_size()
    inventory = (cfg.inv_min + cfg.inv_max) / 2
    clock = {"now": 0.0}
    s.clock = lambda: clock["now"]
    rec = []
    for snap in snaps:
        ob = book_of(snap)
        clock["now"] = ts_of(snap)
        ev = s.evaluate(ob, inventory)
        q = s.decide_order(ob, inventory)
        if not ev.ok:
            rec.append({"ok": False, "reason": ev.reason})
            continue
        b0, a0 = float(snap["bids"][0][0]), float(snap["asks"][0][0])
        rec.append({
            "ok": True, "thin": ev.thin_book, "I_raw": ev.imbalance_raw, "I": ev.imbalance, "I0": ev.imbalance_level0,
            "spread_ticks": (a0 - b0) / eps, "spread_pct": (a0 - b0) / ev.mid, "micro_off": ev.microprice / ev.mid - 1,
            "micro_off_ticks": (ev.microprice - ev.mid) / eps, "centre_off": ev.centre / ev.mid - 1, "h": ev.h,
            "dist_bid": (ev.mid - ev.bid) / ev.mid, "dist_ask": (ev.ask - ev.mid) / ev.mid,
            "bid_vs_b0_ticks": (ev.bid - b0) / eps, "ask_vs_a0_ticks": (ev.ask - a0) / eps,
            "bid_vs_b0_pct": (ev.bid - b0) / ev.mid, "ask_vs_a0_pct": (ev.ask - a0) / ev.mid,
            "clipped_bid": ev.clipped_bid, "clipped_ask": ev.clipped_ask, "net": ev.net_return,
            "kept": bool(q and q["selection reason"] == "microprice_pair_kept"),
            "top_volume": ev.levels[0]["bid_volume"] + ev.levels[0]["ask_volume"],
        })
    ok = [r for r in rec if r["ok"]]
    def pct(values, p):
        return float(np.percentile(values, p)) if values else None
    arr = lambda key: [r[key] for r in ok]  # noqa: E731
    absI = [abs(v) for v in arr("I_raw")]
    steps = np.diff([ts_of(s_) for s_ in snaps])
    return {
        "label": label, "n": len(rec), "ok": len(ok),
        "no_quote": {r["reason"]: sum(1 for x in rec if not x["ok"] and x["reason"] == r["reason"]) for r in rec if not r["ok"]},
        "first": snaps[0]["t"], "last": snaps[-1]["t"], "median_step_s": float(np.median(steps)) if len(steps) else None,
        "inventory": inventory, "settings": {k: getattr(cfg, k) for k in SETTING_KEYS},
        "thin_share": float(np.mean(arr("thin"))),
        "I_raw_mean": float(np.mean(arr("I_raw"))), "I_raw_median": pct(arr("I_raw"), 50),
        "I_raw_p10": pct(arr("I_raw"), 10), "I_raw_p90": pct(arr("I_raw"), 90),
        "absI_median": pct(absI, 50), "clip_share": float(np.mean([a > cfg.algo3_imbalance_clip for a in absI])),
        "spread_ticks_median": pct(arr("spread_ticks"), 50), "spread_ticks_p90": pct(arr("spread_ticks"), 90),
        "spread_pct_median": pct(arr("spread_pct"), 50), "spread_pct_p90": pct(arr("spread_pct"), 90),
        "micro_off_median_signed": pct(arr("micro_off"), 50), "centre_off_abs_median": pct([abs(v) for v in arr("centre_off")], 50),
        "one_tick_share": float(np.mean([v <= 1.0 + 1e-9 for v in arr("spread_ticks")])),
        "micro_off_abs_median": pct([abs(v) for v in arr("micro_off")], 50),
        "micro_off_abs_p90": pct([abs(v) for v in arr("micro_off")], 90),
        "micro_off_abs_max": max(abs(v) for v in arr("micro_off")),
        "micro_ticks_abs_median": pct([abs(v) for v in arr("micro_off_ticks")], 50),
        "h_median": pct(arr("h"), 50),
        "dist_bid_median": pct(arr("dist_bid"), 50), "dist_ask_median": pct(arr("dist_ask"), 50),
        "bid_improves": float(np.mean([v > 1e-9 for v in arr("bid_vs_b0_ticks")])),
        "bid_joins": float(np.mean([abs(v) <= 1e-9 for v in arr("bid_vs_b0_ticks")])),
        "ask_improves": float(np.mean([v < -1e-9 for v in arr("ask_vs_a0_ticks")])),
        "ask_joins": float(np.mean([abs(v) <= 1e-9 for v in arr("ask_vs_a0_ticks")])),
        "bid_vs_b0_ticks_median": pct(arr("bid_vs_b0_ticks"), 50), "ask_vs_a0_ticks_median": pct(arr("ask_vs_a0_ticks"), 50),
        "clipped_bid_share": float(np.mean(arr("clipped_bid"))), "clipped_ask_share": float(np.mean(arr("clipped_ask"))),
        "kept_share": float(np.mean(arr("kept"))), "net_min": min(arr("net")),
        "top_volume_median": pct(arr("top_volume"), 50),
        "series": [{"t": snaps[i]["t"], **{k: rec[i].get(k) for k in ("I_raw", "I", "micro_off", "centre_off", "dist_bid", "dist_ask",
                                                                       "bid_vs_b0_pct", "ask_vs_a0_pct", "spread_pct")}}
                   for i in range(len(rec)) if rec[i]["ok"]],
    }


oct3 = load_jsonl(HERE / "orderbooks_20261003.jsonl.gz")
by_pair = {"BTCIRT": sorted([s for s in oct3 if s["pair"] == "BTCIRT"], key=ts_of),
           "USDTIRT": sorted([s for s in oct3 if s["pair"] == "USDTIRT"], key=ts_of)}
out["behaviour"] = {
    "usdt_3oct": behaviour(by_pair["USDTIRT"], DEMO["usdt"], "USDT/IRT, 3 Oct 2026"),
    "btc_3oct": behaviour(by_pair["BTCIRT"], DEMO["btc"], "BTC/IRT, 3 Oct 2026"),
    "usdt_17oct": behaviour(usdt_today, DEMO["usdt"], "USDT/IRT, 17 Oct 2026"),
    "btc_17oct": behaviour(btc_today, DEMO["btc"], "BTC/IRT, 17 Oct 2026"),
}

# ---------------------------------------------------------------- E. does the imbalance predict the next move? (beta)
def beta_fit(snaps, cfg, tau, raw=False):
    s = Algo3Strategy(cfg)
    ts = np.array([ts_of(x) for x in snaps])
    mids, imb = [], []
    for snap in snaps:
        ev = s.evaluate(book_of(snap), (cfg.inv_min + cfg.inv_max) / 2)
        mids.append(ev.mid if ev.ok else np.nan)
        imb.append((ev.imbalance_raw if raw else ev.imbalance) if ev.ok else np.nan)
    mids, imb = np.array(mids), np.array(imb)
    xs, ys = [], []
    for i in range(len(snaps)):
        j = int(np.searchsorted(ts, ts[i] + tau))
        if j >= len(snaps) or ts[j] - ts[i] > tau + 30 or np.isnan(mids[i]) or np.isnan(mids[j]) or np.isnan(imb[i]):
            continue
        xs.append(imb[i])
        ys.append(mids[j] / mids[i] - 1)
    x, y = np.array(xs), np.array(ys)
    sxx = float(np.sum(x * x))
    beta = float(np.sum(x * y) / sxx)
    e = y - beta * x
    n = len(x)
    lag = int(math.ceil(tau / float(np.median(np.diff(ts)))))
    u = x * e
    s0 = float(np.sum(u * u))
    for l_ in range(1, lag + 1):
        w = 1 - l_ / (lag + 1)
        s0 += 2 * w * float(np.sum(u[l_:] * u[:-l_]))
    se_hac = math.sqrt(s0) / sxx
    se_iid = math.sqrt(float(np.sum(e * e)) / (n - 1) / sxx)
    points = [[float(a), float(b)] for a, b in zip(x, y)] if tau == 600 else None
    return {"points": points, "tau_s": tau, "n": n, "beta": beta, "se_hac": se_hac, "t_hac": beta / se_hac, "t_iid": beta / se_iid,
            "lag": lag, "independent_windows": float((ts[-1] - ts[0]) / tau),
            "median_abs_I": float(np.median(np.abs(x))), "lean_at_median_abs_I": beta * float(np.median(np.abs(x))),
            "r_std": float(np.std(y)), "I_std": float(np.std(x))}


out["beta"] = {
    pair: {"clipped": [beta_fit(by_pair[pair], DEMO[key], tau) for tau in (60, 300, 600)],
           "raw": [beta_fit(by_pair[pair], DEMO[key], tau, raw=True) for tau in (60, 300, 600)]}
    for pair, key in (("USDTIRT", "usdt"), ("BTCIRT", "btc"))
}

# ---------------------------------------------------------------- F. the live settings on today's books
def live_what_if(pair, snaps, inventory, cash):
    cfg = LIVE[pair]
    s = Algo3Strategy(cfg)
    snap = snaps[-1]
    ob = book_of(snap)
    ev = evaluation_dict(s, ob, inventory)
    buy_t, sell_t = sized(cfg, inventory)
    min_size = MIN_ORDER_SIZE_FRACTION * float(cfg.order_size)
    res = {"t": snap["t"], "inventory": inventory, "cash": cash, "evaluation": ev,
           "buy_target": buy_t, "sell_target": sell_t, "min_size": min_size}
    if ev["ok"]:
        buy = min(buy_t, cash / ev["bid"])
        res["buy_affordable"] = buy
        res["buy_notional"] = buy * ev["bid"]
        res["buy_exposure_ratio"] = SimpleMarketMaker._locked_exposure_ratio(buy * ev["bid"], cash)
        res["band_after_buy"] = inventory + buy
        sell = min(sell_t, inventory - float(cfg.inv_min))
        res["sell_size"] = sell
        res["sell_exposure_ratio"] = SimpleMarketMaker._locked_exposure_ratio(sell, inventory) if inventory > 0 else None
        # Consecutive buys while none fills (each at a price more than hysteresis_ticks from the others):
        # every resting buy counts in the sizing (x_b) and in the exposure guard, until the guard stops it.
        seq, open_buy, locked_cash = [], 0.0, 0.0
        limit = float(cfg.buy_locked_cash_limit_ratio)
        for _ in range(10):
            target, _sell = sized(cfg, inventory, open_buy, 0.0)
            size = min(target, (cash - locked_cash) / ev["bid"])
            ratio = SimpleMarketMaker._locked_exposure_ratio(locked_cash + size * ev["bid"], cash)
            blocked = ("inventory_sizing" if target < min_size else "insufficient_cash" if size < min_size
                       else "inventory_cap" if open_buy + inventory + size > float(cfg.inv_max)
                       else "locked_cash_limit" if ratio > limit else None)
            seq.append({"target": target, "size": size, "notional": size * ev["bid"], "ratio_after": ratio, "blocked": blocked})
            if blocked:
                break
            open_buy += size
            locked_cash += size * ev["bid"]
        res["buy_sequence"] = seq
        res["max_locked_cash"] = locked_cash
    return res


out["live_what_if"] = {
    "btc": live_what_if("btc", btc_today, 0.0, 57_637_520.0),
    "usdt": live_what_if("usdt", usdt_today, 0.9975, 57_637_520.0),
}

# ---------------------------------------------------------------- G. code facts
modes = {}
for name, definition in demo_config.FIELD_DEFINITIONS.items():
    modes.setdefault(definition.mode.value, []).append(name)
out["code_facts"] = {
    "demo_columns": len(SimpleMarketMaker.DEMO_COLUMNS),
    "live_columns": 21 + len(SimpleMarketMaker.QUOTE_EXTRA_COLUMNS),
    "quote_extra_columns": list(SimpleMarketMaker.QUOTE_EXTRA_COLUMNS),
    "demo_fields": len(demo_config.FIELD_DEFINITIONS),
    "demo_field_modes": {k: len(v) for k, v in modes.items()},
    "demo_new_session_fields": modes.get("new_session", []),
    "forward_fields": len(forward_config.FORWARD_FIELDS),
    "algo3_fields": {name: {"type": d.value_type.__name__, "min": d.minimum, "max": d.maximum, "mode": d.mode.value,
                            "help": d.help_text}
                     for name, d in demo_config.FIELD_DEFINITIONS.items() if name.startswith("algo3_") or name == "algorithm"},
    "min_order_size_fraction": MIN_ORDER_SIZE_FRACTION,
}

target = HERE / "mm_algo2_doc_numbers.json"
target.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
print("written", target)
