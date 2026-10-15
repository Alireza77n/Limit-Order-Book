"""Market-maker console -- the results dashboard (read-only).

Reads the CSV/SQLite result stores run.py writes (real and demo, BTC and
USDT), the fill-probability tables and the per-round decision diagnostics,
and serves a live-updating Dash app with six tabs:

* Overview      -- quote rate, fills, realised vs computed spread, adverse
                   selection, PnL, inventory; mid-price with our quotes and
                   fills; where the quotes sit; why there was no quote.
* Quote decision-- one round of Algorithm 1 v2: the candidates, the
                   expected-value heat map of every pair, the decision trace,
                   and the last rounds' decisions as a strip.
* Fill model    -- the measured fill-probability table vs the formula,
                   calibration (predicted vs realised), adverse selection,
                   table health.
* Order book    -- selected levels and quoted spreads over time.
* Validation    -- the demo-to-real gate, equity/drawdown, trading and
                   inventory statistics.
* Rounds        -- every round as a table.

Never touches Nobitex or MySQL. Pure data-handling functions are kept
separate from the Dash wiring so they can be unit-tested without a server
(tests/test_dashboard.py, tests/test_dashboard_v2.py).
"""

import fcntl
import json
import math
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, ctx, dash_table, dcc, html, no_update
from dash.dependencies import Input, Output, State

from config import DEMO_STATE_DIR, RESULTS_DIR, TRADING_PROFILES, demo_csv_path
from demo_config import (
    CONFIG_STATUS_LABELS,
    OverlayLoadError,
    OverlayValidationError,
    config_status,
    editable_schema,
    load_desired_config,
    read_runtime_snapshot,
)
from result_store import DemoResultStore
from session import SessionError, load_stack_session
from validation.validation_engine import create_validation_engine
from validation.reports import generate_validation_report, format_metric_card

# Every track this project can produce, explicitly labeled real vs demo so
# they're never confused with each other in the UI. Sourced directly from
# config.TRADING_PROFILES -- the same dict run.py builds MMConfig from --
# so the dashboard always agrees with run.py about where results live.
FORWARD_RESULTS_DIR = os.environ.get("LOB_FORWARD_RESULTS_DIR", RESULTS_DIR)
TRACKS = {
    "btc_real": {
        "label": "BTC — Live Forward Test",
        "path": os.path.join(FORWARD_RESULTS_DIR, "btc_track.csv"),
        "status": "real",
    },
    "btc_demo": {"label": "BTC — Demo (paper trading)",
                 "path": demo_csv_path(TRADING_PROFILES["btc"]["output_csv"]), "status": "demo"},
    "usdt_real": {
        "label": "USDT — Live Forward Test",
        "path": os.path.join(FORWARD_RESULTS_DIR, "usdt_track.csv"),
        "status": "real",
    },
    "usdt_demo": {"label": "USDT — Demo (paper trading)",
                  "path": demo_csv_path(TRADING_PROFILES["usdt"]["output_csv"]), "status": "demo"},
}

# Production can deliberately expose only the isolated live-forward result
# streams.  Keeping this as an environment switch preserves the paper-mode
# dashboard for local development while ensuring the public dashboard never
# mixes simulated rows with a real-account view.
if os.environ.get("LOB_LIVE_ONLY_DASHBOARD", "0") == "1":
    TRACKS = {key: value for key, value in TRACKS.items() if value["status"] == "real"}

QUOTE_EXTRA_COLUMNS = [
    "Quote Mode", "Selection Reason", "Centre", "Bid Distance", "Ask Distance",
    "Bid Fill Prob", "Ask Fill Prob", "Expected Value", "Reference Price",
]

ACTIVITY_COLUMNS = [
    "Timestamp", "Session_id", "Round", "Quote Status", "No Quote Reason",
    "No Quote Consecutive Count", "No Quote Episode Seconds",
    "No Quote Alert State", "No Quote Candidate Diagnostics",
    "Bid Level", "Ask Level", "Bid Price", "Ask Price",
    "Mid Price", "Relative Quoted Spread", "Gross Return", "Net Return",
    "Min Required Return",
    "Buy Order ID", "Buy Status", "Buy Not Sent Reason",
    "Buy Original Amount", "Buy Matched Amount", "Buy Remaining Amount",
    "Buy Fill Reason", "Buy Locked Cash", "Buy Telegram Message ID",
    "Sell Order ID", "Sell Status", "Sell Not Sent Reason",
    "Sell Original Amount", "Sell Matched Amount", "Sell Remaining Amount",
    "Sell Fill Reason", "Sell Locked Inventory", "Sell Telegram Message ID",
    *QUOTE_EXTRA_COLUMNS,
]

# Buckets (relative distance) used for calibration tables; match the fill-table builder.
CALIBRATION_BUCKETS = (0.001, 0.0015, 0.002, 0.003, 0.004, 0.006, 0.008, 0.012, 0.016, 0.024)

TAB_VALUES = ("overview", "decision", "fillmodel", "orderbook", "validation", "trading", "attribution")


def _url_params(search) -> Dict[str, str]:
    """Query-string parameters of a deep link, first value each:
    ?tab=decision&track=usdt_demo&session=<id>."""
    if not search:
        return {}
    return {key: values[0] for key, values in parse_qs(str(search).lstrip("?")).items() if values}


# Rounds offered in the Quote decision picker. Each diagnostics document holds
# every scored pair (~50 KB), so this bounds what one refresh reads.
DECISION_ROUNDS = 120

# The demo-to-real gate (docs/algorithms/ALGORITHM_1_V2.md section 7).
GATE_THRESHOLDS = {
    "quote_rate": 0.50, "fills_per_hour": 1.0, "round_trips": 30,
    "spread_gap": -0.0005, "adverse_60s": -0.001, "fill_forecast_error": 0.30,
    "band_edge_share": 0.10, "net_pnl": 0.0, "drawdown": 0.01,
}

# ---------- Design tokens ----------
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
BID_COLOR, ASK_COLOR, FILL_COLOR = "#2a78d6", "#eb6834", "#0f6e6e"
SURFACE = "#ffffff"
PAGE = "#f3f5f4"
BAR = "#12333a"
TEXT_PRIMARY = "#1d2326"
TEXT_SECONDARY = "#44535a"
MUTED = "#8a9aa0"
GRIDLINE = "#e4e8e6"
BASELINE = "#c9d1ce"
STATUS_CRITICAL = "#c0392b"  # real money -- draws the eye
STATUS_GOOD = "#0f6e6e"      # demo/paper -- safe
STATUS_WARN = "#8a4b00"
ACCENT_SOFT = "#e3f0ee"
FONT_FAMILY = "system-ui, -apple-system, 'Segoe UI', sans-serif"
MONO = "Consolas, 'JetBrains Mono', monospace"


# ---------- Pure data handling (unit-tested, no Dash involved) ----------

# Newest rounds a track view holds. Every row is decoded from JSON into
# Python objects, and a full 5,700-row store cost the dashboard container
# about 210 MB against its 192 MB limit, so the view is bounded; the status
# line says when it is.
MAX_ROWS = max(100, int(os.environ.get("LOB_DASHBOARD_MAX_ROWS", "2000") or 2000))

_TRACK_CACHE: Dict[str, Any] = {}
_TRACK_CACHE_LOCK = threading.Lock()
_TRACK_LOAD_LOCKS: Dict[str, threading.Lock] = {}


def _track_signature(path: Path) -> tuple:
    """What must change for a track to be re-read: the SQLite file, its
    write-ahead log (appends land there first) and the CSV fallback."""
    parts = []
    for candidate in (path.with_suffix(".sqlite3"), path.with_suffix(".sqlite3-wal"), path):
        try:
            stat = candidate.stat()
            parts.append((candidate.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append((candidate.name, None, None))
    return tuple(parts)


def _session_filter(session_id):
    """None for the comparison view, else the session to load."""
    return None if session_id in (None, "all") else session_id


def load_track(path: str, session_id=None) -> Optional[pd.DataFrame]:
    """Read a track's result store (SQLite first, CSV fallback): the newest
    MAX_ROWS rounds of every session, or of one session when ``session_id``
    is given. Returns None if the store doesn't exist yet, is unreadable, or
    has no rows -- callers show a "no data yet" state. Reads are cached
    until one of the files changes, so the callbacks of one refresh tick
    share a single load."""
    key = (path, None if session_id is None else str(session_id))
    signature = _track_signature(Path(path))
    with _TRACK_CACHE_LOCK:
        cached = _TRACK_CACHE.get(key)
        load_lock = _TRACK_LOAD_LOCKS.setdefault(key, threading.Lock())
    if cached is not None and cached[0] == signature:
        frame = cached[1]
        return None if frame is None else frame.copy()
    # One decode at a time per track: the callbacks of a refresh tick fire
    # together, and concurrent decodes of the same rows were enough to push
    # the container over its memory limit.
    with load_lock:
        with _TRACK_CACHE_LOCK:
            cached = _TRACK_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            frame = cached[1]
            return None if frame is None else frame.copy()
        frame = _read_track(path, session_id=session_id)
        with _TRACK_CACHE_LOCK:
            _TRACK_CACHE[key] = (signature, frame)
    return None if frame is None else frame.copy()


def available_sessions(path: str) -> Optional[List]:
    """Every session of a track, newest first, however old (from the
    store's summary); falls back to the loaded frame for CSV-only tracks.
    None when the track has no data."""
    store_path = Path(path).with_suffix(".sqlite3")
    if store_path.exists():
        try:
            summary = DemoResultStore.open_existing(store_path).session_summary()
        except Exception:
            summary = None
        if summary:
            return [row["session_id"] for row in summary][::-1]
    df = load_track(path)
    if df is None:
        return None
    return session_ids(df)


def _read_track(path: str, max_rows: Optional[int] = None, session_id=None) -> Optional[pd.DataFrame]:
    """The newest ``max_rows`` (default MAX_ROWS) rounds of a track, of one
    session when ``session_id`` is given."""
    limit = MAX_ROWS if max_rows is None else max(1, int(max_rows))
    p = Path(path)
    store_path = p.with_suffix(".sqlite3")
    df = None
    if store_path.exists():
        try:
            df = DemoResultStore.open_existing(store_path).load_view(
                limit=limit, session_id=None if session_id is None else str(session_id),
            )
        except Exception:
            df = None
    if df is None or df.empty:
        if not p.exists():
            return None
        try:
            df = pd.read_csv(path)
            if session_id is not None and "Session_id" in df.columns:
                df = df[df["Session_id"].astype(str) == str(session_id)]
            df = df.tail(limit).reset_index(drop=True)
        except Exception:
            return None
    if df.empty:
        return None
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    return df


def default_track() -> str:
    """The track whose result store was written most recently (the running
    workers), so the page opens on live data; the first track when none has
    data yet."""
    newest, newest_mtime = None, None
    for key, track in TRACKS.items():
        path = Path(track["path"])
        for candidate in (path.with_suffix(".sqlite3"), path):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if newest_mtime is None or mtime > newest_mtime:
                newest, newest_mtime = key, mtime
    return newest or next(iter(TRACKS))


def with_derived_spreads(df: pd.DataFrame) -> pd.DataFrame:
    """Add Mid Price / Bid Spread / Ask Spread columns.

    Derived purely from already-logged Best Bid / Best Ask / Bid Price
    Sent / Ask Price Sent (s_b = mid - bid_price, s_a = ask_price - mid).
    No new data collected; engine.py's CSV schema is untouched.
    """
    df = df.copy()
    df["Mid Price"] = (_num(df["Best Bid"]) + _num(df["Best Ask"])) / 2.0
    bid_column = "Bid Price" if "Bid Price" in df.columns else "Bid Price Sent"
    ask_column = "Ask Price" if "Ask Price" in df.columns else "Ask Price Sent"
    df["Bid Spread"] = df["Mid Price"] - _num(df[bid_column])
    df["Ask Spread"] = _num(df[ask_column]) - df["Mid Price"]
    return df


def activity_records(df: pd.DataFrame, limit: int = 200) -> List[dict]:
    """Return dashboard rows from the same explicit lifecycle fields as CSV.

    Older Real-mode CSVs are tolerated by filling unavailable explicit fields;
    the original status values remain visible until Real reporting is migrated.
    """
    activity = df.copy()
    legacy_aliases = {
        "Bid Price": "Bid Price Sent",
        "Ask Price": "Ask Price Sent",
        "Buy Original Amount": "Buy Size",
        "Sell Original Amount": "Sell Size",
    }
    for target, source in legacy_aliases.items():
        if target not in activity.columns and source in activity.columns:
            activity[target] = activity[source]
    for column in ACTIVITY_COLUMNS:
        if column not in activity.columns:
            activity[column] = None
    return (
        activity[ACTIVITY_COLUMNS]
        .sort_values("Timestamp", ascending=False)
        .head(limit)
        .to_dict("records")
    )


def no_quote_summary(df: pd.DataFrame) -> str:
    """Explain gaps in the price/spread charts.

    A round where Algorithm 1 found no profitable quote logs no Bid/Ask
    Price, so the line charts render a gap. Returns "" when the columns
    aren't present (older Real-mode CSVs) or nothing was skipped.
    """
    if "Quote Status" not in df.columns:
        return ""
    skipped = df[df["Quote Status"] == "no_quote"]
    if skipped.empty:
        return ""
    part = f" — {len(skipped)} of {len(df)} rounds produced no quote"
    if "No Quote Reason" in skipped.columns:
        reasons = skipped["No Quote Reason"].dropna().value_counts()
        if not reasons.empty:
            detail = ", ".join(f"{reason}: {count}" for reason, count in reasons.items())
            part += f" ({detail})"
    return part + ". Chart gaps are these rounds, not missing data."


def session_ids(df: pd.DataFrame) -> List:
    """Distinct Session_id values, most recent (largest) first."""
    return sorted(df["Session_id"].dropna().unique().tolist(), reverse=True)


def filter_session(df: pd.DataFrame, session_id) -> pd.DataFrame:
    """session_id of None/"all" returns every session (comparison view)."""
    if session_id in (None, "all"):
        return df
    return df[df["Session_id"] == session_id]


def _num(series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """A numeric column, or an all-NaN series when it does not exist."""
    if name in df.columns:
        return _num(df[name])
    return pd.Series([float("nan")] * len(df), index=df.index, dtype=float)


def _pair_of(track_key: str) -> str:
    return track_key.split("_", 1)[0]


def _symbol_of(track_key: str) -> str:
    return str(TRADING_PROFILES[_pair_of(track_key)]["pair"]).lower()


def _results_dir_of(track_key: str) -> str:
    return FORWARD_RESULTS_DIR if TRACKS.get(track_key, {}).get("status") == "real" else RESULTS_DIR


def track_settings(track_key: str) -> Dict[str, Any]:
    """The strategy settings in force for a track: the profile, overlaid with
    the Demo desired config (demo tracks) or the live forward config (real
    tracks) when those can be read."""
    pair = _pair_of(track_key)
    profile = TRADING_PROFILES[pair]
    keys = (
        "commission_perc", "exp_ret_min", "inv_min", "inv_max", "cancel_after_minutes",
        "fill_kappa", "fill_lambda_tau", "one_sided_lo", "one_sided_hi", "ev_min",
        "order_size", "grid_min_perc", "grid_ratio", "grid_max_perc", "skew_perc",
    )
    settings = {key: profile.get(key) for key in keys}
    overlay: Dict[str, Any] = {}
    try:
        if TRACKS.get(track_key, {}).get("status") == "demo":
            overlay = load_desired_config(pair, Path(DEMO_STATE_DIR), TRADING_PROFILES)
        elif os.environ.get("LOB_FORWARD_CONFIG_DIR"):
            from forward_config import desired_config
            overlay = desired_config(pair, TRADING_PROFILES)
    except Exception:
        overlay = {}
    for key in keys:
        if key in overlay and overlay[key] is not None:
            settings[key] = overlay[key]
    fee = float(settings.get("commission_perc") or 0.0)
    target = settings.get("exp_ret_min")
    settings["exp_ret_min"] = fee if target is None else float(target)
    settings["gross_bar"] = (1.0 + settings["exp_ret_min"]) / ((1.0 - fee) ** 2) - 1.0 if fee < 1 else float("inf")
    return settings


def quote_mode_counts(df: pd.DataFrame) -> Dict[str, int]:
    """Rounds by decision: two-sided pair, bid only, ask only, none."""
    counts = {"pair": 0, "bid_only": 0, "ask_only": 0, "none": 0, "total": int(len(df))}
    if df.empty:
        return counts
    if "Quote Mode" in df.columns and df["Quote Mode"].notna().any():
        modes = df["Quote Mode"].fillna("none").astype(str)
    else:
        bid = _col(df, "Bid Price" if "Bid Price" in df.columns else "Bid Price Sent").notna()
        ask = _col(df, "Ask Price" if "Ask Price" in df.columns else "Ask Price Sent").notna()
        modes = pd.Series("none", index=df.index)
        modes[bid & ask] = "pair"
        modes[bid & ~ask] = "bid_only"
        modes[~bid & ask] = "ask_only"
    for mode, count in modes.value_counts().items():
        counts[mode if mode in counts else "none"] += int(count)
    return counts


def no_quote_reason_counts(df: pd.DataFrame) -> Dict[str, int]:
    if "No Quote Reason" not in df.columns:
        return {}
    reasons = df["No Quote Reason"].dropna().astype(str)
    return {reason: int(count) for reason, count in reasons.value_counts().items()}


def fill_events(df: pd.DataFrame) -> pd.DataFrame:
    """One row per filled order (buy or sell): placement time, side, price,
    matched amount and the fill probability predicted at placement. Demo
    rows carry the explicit lifecycle fields; legacy live rows, whose status
    cell holds the matched amount once filled, are tolerated."""
    df = df.reset_index(drop=True)
    rows = []
    for side, prefix in (("buy", "Buy"), ("sell", "Sell")):
        status_col = f"{prefix} Status"
        if status_col not in df.columns:
            continue
        status = df[status_col].astype(str).str.lower()
        legacy = pd.to_numeric(df[status_col], errors="coerce")
        matched = _col(df, f"{prefix} Matched Amount")
        matched = matched.where(matched.notna(), legacy)
        filled = status.isin({"filled", "partially_filled"}) | (legacy > 0)
        price_col = f"{prefix} Limit Price"
        if price_col not in df.columns or _col(df, price_col).isna().all():
            price_col = "Bid Price" if side == "buy" else "Ask Price"
            if price_col not in df.columns:
                price_col = "Bid Price Sent" if side == "buy" else "Ask Price Sent"
        prices = _col(df, price_col)
        probability = _col(df, "Bid Fill Prob" if side == "buy" else "Ask Fill Prob")
        rounds = df["Round"] if "Round" in df.columns else pd.Series([None] * len(df), index=df.index)
        mask = filled & (matched > 0) & prices.notna()
        for index in df.index[mask]:
            rows.append({
                "Timestamp": df.at[index, "Timestamp"],
                "Round": rounds.at[index],
                "side": side,
                "price": float(prices.at[index]),
                "amount": float(matched.at[index]),
                "fill_prob": float(probability.at[index]),
            })
    frame = pd.DataFrame(rows, columns=["Timestamp", "Round", "side", "price", "amount", "fill_prob"])
    return frame.sort_values("Timestamp").reset_index(drop=True)


def span_hours(df: pd.DataFrame) -> float:
    stamps = pd.to_datetime(df["Timestamp"], errors="coerce").dropna()
    if len(stamps) < 2:
        return 0.0
    return max(0.0, (stamps.max() - stamps.min()).total_seconds() / 3600.0)


def fills_per_hour(df: pd.DataFrame) -> Optional[float]:
    hours = span_hours(df)
    if hours <= 0:
        return None
    return len(fill_events(df)) / hours


def round_trips(df: pd.DataFrame, fee: float) -> List[Dict[str, float]]:
    """FIFO-match buy fills against later sell fills. Each trip: buy price,
    sell price, amount, net return (1-f)^2 * sell/buy - 1."""
    events = fill_events(df)
    buys: List[List[float]] = []     # [price, remaining amount]
    trips = []
    for event in events.itertuples(index=False):
        if event.side == "buy":
            buys.append([event.price, event.amount])
            continue
        remaining = event.amount
        while remaining > 1e-12 and buys:
            price, available = buys[0]
            take = min(available, remaining)
            trips.append({
                "buy": price, "sell": event.price, "amount": take,
                "net": (1.0 - fee) ** 2 * event.price / price - 1.0 if price > 0 else float("nan"),
            })
            buys[0][1] -= take
            remaining -= take
            if buys[0][1] <= 1e-12:
                buys.pop(0)
    return trips


def realised_vs_computed_spread(df: pd.DataFrame, fee: float) -> Dict[str, Optional[float]]:
    """Realised: amount-weighted mean net return of completed round trips.
    Computed: mean of the strategy's net return at placement on two-sided rounds."""
    trips = round_trips(df, fee)
    realised = None
    if trips:
        weights = sum(t["amount"] for t in trips)
        realised = sum(t["net"] * t["amount"] for t in trips) / weights if weights > 0 else None
    computed = None
    net = _col(df, "Net Return")
    if "Quote Mode" in df.columns and df["Quote Mode"].notna().any():
        net = net[df["Quote Mode"] == "pair"]
    net = net.dropna()
    if not net.empty:
        computed = float(net.mean())
    return {"realised": realised, "computed": computed, "trips": len(trips)}


def adverse_selection(df: pd.DataFrame, horizons: Sequence[int] = (60, 300)) -> Dict[int, Dict[str, Optional[float]]]:
    """Mean signed mid-price move after our fills: for a buy (mid_h - P)/P,
    for a sell (P - mid_h)/P -- negative means the market moved against the
    position we were given. Measured from the row the order was placed on."""
    result: Dict[int, Dict[str, Optional[float]]] = {}
    events = fill_events(df)
    mid = _col(df, "Mid Price")
    if mid.isna().all():
        mid = (_col(df, "Best Bid") + _col(df, "Best Ask")) / 2.0
    mids = pd.DataFrame({
        "Timestamp": pd.to_datetime(df["Timestamp"], errors="coerce"), "Mid Price": mid,
    }).dropna().sort_values("Timestamp").reset_index(drop=True)
    stamps = mids["Timestamp"]
    values = mids["Mid Price"].to_numpy(dtype=float)
    for horizon in horizons:
        moves = []
        for event in events.itertuples(index=False):
            if pd.isna(event.Timestamp) or event.price <= 0 or stamps.empty:
                continue
            target = pd.Timestamp(event.Timestamp) + pd.Timedelta(seconds=int(horizon))
            position = int(stamps.searchsorted(target, side="left"))
            if position >= len(stamps):
                continue
            mid_h = values[position]
            move = (mid_h - event.price) / event.price if event.side == "buy" else (event.price - mid_h) / event.price
            moves.append(move)
        result[int(horizon)] = {"mean": (sum(moves) / len(moves)) if moves else None, "n": len(moves)}
    return result


def quote_distances(df: pd.DataFrame) -> pd.DataFrame:
    """Distance of each quoted bid/ask from the mid, as a fraction of price."""
    mid = _col(df, "Mid Price")
    if mid.isna().all():
        mid = (_col(df, "Best Bid") + _col(df, "Best Ask")) / 2.0
    bid_price = _col(df, "Bid Price" if "Bid Price" in df.columns else "Bid Price Sent")
    ask_price = _col(df, "Ask Price" if "Ask Price" in df.columns else "Ask Price Sent")
    bid = _col(df, "Bid Distance")
    ask = _col(df, "Ask Distance")
    bid = bid.where(bid.notna(), (mid - bid_price) / mid)
    ask = ask.where(ask.notna(), (ask_price - mid) / mid)
    rows = []
    for value in bid.dropna():
        rows.append({"side": "bid", "distance": float(value)})
    for value in ask.dropna():
        rows.append({"side": "ask", "distance": float(value)})
    return pd.DataFrame(rows, columns=["side", "distance"])


def inventory_position(df: pd.DataFrame, inv_min: float, inv_max: float) -> pd.Series:
    band = float(inv_max) - float(inv_min)
    inventory = _col(df, "inv_current")
    if band <= 0:
        return pd.Series(0.5, index=df.index)
    return ((inventory - float(inv_min)) / band).clip(0.0, 1.0)


def fill_table_path(track_key: str) -> str:
    return os.path.join(_results_dir_of(track_key), f"fill_table_{_symbol_of(track_key)}.json")


def load_fill_table(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("buckets"), list):
        return None
    return document


def diagnostics_path(track_key: str) -> str:
    return os.path.join(_results_dir_of(track_key), f"{_symbol_of(track_key)}_algo1_diagnostics.jsonl")


def load_diagnostics(path: str, limit: int = 200) -> List[Dict[str, Any]]:
    """The newest ``limit`` documents of the strategy's JSONL, read from the
    end of the file so a large file costs nothing extra."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    chunk = 65536
    data = b""
    with open(path, "rb") as stream:
        position = size
        while position > 0 and data.count(b"\n") <= limit + 1:
            step = min(chunk, position)
            position -= step
            stream.seek(position)
            data = stream.read(step) + data
    lines = data.split(b"\n")
    if position > 0 and lines:
        lines = lines[1:]           # the first piece may be a partial line
    documents = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            documents.append(json.loads(line))
        except ValueError:
            continue
    return documents[-limit:]


def formula_probability(distance: float, kappa: float, lambda_tau: float) -> float:
    return max(0.0, min(1.0, 1.0 - math.exp(-max(0.0, lambda_tau) * math.exp(-max(0.0, kappa) * max(0.0, distance)))))


def calibration_rows(df: pd.DataFrame, buckets: Sequence[float] = CALIBRATION_BUCKETS) -> List[Dict[str, Any]]:
    """Predicted fill probability at placement vs the realised fill rate of
    our own orders, per side and distance bucket (terminal orders only)."""
    rows = []
    edges = list(buckets)
    for side, prefix, dist_col, prob_col in (("bid", "Buy", "Bid Distance", "Bid Fill Prob"),
                                             ("ask", "Sell", "Ask Distance", "Ask Fill Prob")):
        if f"{prefix} Status" not in df.columns or f"{prefix} Order ID" not in df.columns:
            continue
        has_order = df[f"{prefix} Order ID"].notna()
        status = df[f"{prefix} Status"].astype(str).str.lower()
        terminal = status.isin({"filled", "partially_filled", "cancelled", "canceled", "expired"})
        distance = _col(df, dist_col)
        if distance.isna().all():
            mid = _col(df, "Mid Price")
            if mid.isna().all():
                mid = (_col(df, "Best Bid") + _col(df, "Best Ask")) / 2.0
            price = _col(df, f"{prefix} Limit Price")
            distance = (mid - price) / mid if side == "bid" else (price - mid) / mid
        probability = _col(df, prob_col)
        filled = status.isin({"filled", "partially_filled"})
        mask = has_order & terminal & distance.notna()
        if not mask.any():
            continue
        for i, edge in enumerate(edges):
            lower = edges[i - 1] if i > 0 else 0.0
            in_bucket = mask & (distance > lower) & (distance <= edge)
            n = int(in_bucket.sum())
            if n == 0:
                continue
            rows.append({
                "side": side, "distance": edge, "n": n,
                "realised": float(filled[in_bucket].mean()),
                "predicted": float(probability[in_bucket].mean()) if probability[in_bucket].notna().any() else None,
            })
    return rows


def fills_by_day(df: pd.DataFrame) -> pd.DataFrame:
    """Per calendar day: sum of predicted fill probabilities of the orders we
    placed (expected fills) vs the orders that actually filled."""
    rows = []
    for prefix, prob_col in (("Buy", "Bid Fill Prob"), ("Sell", "Ask Fill Prob")):
        if f"{prefix} Order ID" not in df.columns or f"{prefix} Status" not in df.columns:
            continue
        placed = df[df[f"{prefix} Order ID"].notna()]
        if placed.empty:
            continue
        days = pd.to_datetime(placed["Timestamp"], errors="coerce").dt.date
        filled = placed[f"{prefix} Status"].astype(str).str.lower().isin({"filled", "partially_filled"})
        predicted = _col(placed, prob_col).fillna(0.0)
        frame = pd.DataFrame({"day": days, "predicted": predicted.to_numpy(), "realised": filled.astype(int).to_numpy()})
        rows.append(frame)
    if not rows:
        return pd.DataFrame(columns=["day", "predicted", "realised"])
    merged = pd.concat(rows).dropna(subset=["day"])
    return merged.groupby("day", as_index=False)[["predicted", "realised"]].sum().sort_values("day")


def drawdown_fraction(df: pd.DataFrame) -> Optional[float]:
    wealth = _col(df, "Portfolio Wealth").dropna()
    if wealth.empty:
        return None
    peak = wealth.cummax()
    draw = ((wealth - peak) / peak).min()
    return float(abs(draw)) if pd.notna(draw) else None


def gate_rows(df: pd.DataFrame, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The demo-to-real gate of ALGORITHM_1_V2.md section 7, one row per
    metric with its value, threshold and pass/fail (None = not measurable yet)."""
    fee = float(settings.get("commission_perc") or 0.0)
    modes = quote_mode_counts(df)
    total = modes["total"] or 1
    quote_rate = (modes["pair"] + modes["bid_only"] + modes["ask_only"]) / total
    fph = fills_per_hour(df)
    spread = realised_vs_computed_spread(df, fee)
    adverse = adverse_selection(df, (60,)).get(60, {"mean": None})
    x = inventory_position(df, settings.get("inv_min") or 0.0, settings.get("inv_max") or 1.0).dropna()
    edge_share = float(((x < 0.1) | (x > 0.9)).mean()) if len(x) else None
    wealth = _col(df, "Portfolio Wealth").dropna()
    pnl = float(wealth.iloc[-1] - wealth.iloc[0]) if len(wealth) >= 2 else None
    pnl_fraction = pnl / float(wealth.iloc[0]) if pnl is not None and wealth.iloc[0] else None
    drawdown = drawdown_fraction(df)
    forecast = fills_by_day(df)
    forecast_error = None
    if not forecast.empty and forecast["realised"].sum() > 0:
        forecast_error = float(forecast["predicted"].sum() / forecast["realised"].sum() - 1.0)
    spread_gap = (spread["realised"] - spread["computed"]) if spread["realised"] is not None and spread["computed"] is not None else None
    t = GATE_THRESHOLDS

    def row(metric, value, display, threshold, passed):
        return {"metric": metric, "value": value, "display": display, "threshold": threshold, "passed": passed}

    return [
        row("rounds with a quote", quote_rate, f"{quote_rate:.1%}", f"≥ {t['quote_rate']:.0%}", quote_rate >= t["quote_rate"]),
        row("fills per hour", fph, "—" if fph is None else f"{fph:.2f}", f"≥ {t['fills_per_hour']:.0f}", None if fph is None else fph >= t["fills_per_hour"]),
        row("completed round trips", spread["trips"], str(spread["trips"]), f"≥ {t['round_trips']}", spread["trips"] >= t["round_trips"]),
        row("realised − computed net spread", spread_gap, "—" if spread_gap is None else f"{spread_gap:+.3%}", f"≥ {t['spread_gap']:+.2%}", None if spread_gap is None else spread_gap >= t["spread_gap"]),
        row("adverse selection, 60 s after fills", adverse["mean"], "—" if adverse["mean"] is None else f"{adverse['mean']:+.3%}", f"≥ {t['adverse_60s']:+.2%}", None if adverse["mean"] is None else adverse["mean"] >= t["adverse_60s"]),
        row("fill forecast error (predicted / realised − 1)", forecast_error, "—" if forecast_error is None else f"{forecast_error:+.0%}", f"within ±{t['fill_forecast_error']:.0%}", None if forecast_error is None else abs(forecast_error) <= t["fill_forecast_error"]),
        row("time at band edges (x < 0.1 or > 0.9)", edge_share, "—" if edge_share is None else f"{edge_share:.1%}", f"≤ {t['band_edge_share']:.0%}", None if edge_share is None else edge_share <= t["band_edge_share"]),
        row("net PnL after fees", pnl, "—" if pnl is None else f"{pnl:+,.0f} ({pnl_fraction:+.2%})", "> 0", None if pnl is None else pnl > t["net_pnl"]),
        row("mark-to-market drawdown", drawdown, "—" if drawdown is None else f"{drawdown:.2%}", f"≤ {t['drawdown']:.0%}", None if drawdown is None else drawdown <= t["drawdown"]),
    ]


# ---------- PnL attribution (docs/algorithms/PNL_ATTRIBUTION.md) ----------

ATTRIBUTION_COLUMNS = ["Price PnL", "Trading PnL", "Total PnL", "Fees"]


def pnl_attribution(df: pd.DataFrame) -> pd.DataFrame:
    """Split every round's change in wealth  W_t = cash_t + inv_t * mid_t  into

        price PnL    P_t = inv_{t-1} * (mid_t - mid_{t-1})
                     (the market moved while we were holding inventory)
        trading PnL  T_t = (cash_t - cash_{t-1}) + (inv_t - inv_{t-1}) * mid_t
                     (our fills measured against the mid, net of fees)

    so that  W_t - W_{t-1} = P_t + T_t  holds exactly in every round. The sums
    restart at the first round of each session (that round is the baseline and
    gets 0). Only inv_current, cash_current and a mid are needed: Mid Price when
    the row has one, else the best bid/ask midpoint, so demo and live rows both
    work. Returns one row per usable round, oldest first within each session,
    with the per-round values and their running sums (the "(cum)" columns)."""
    empty = pd.DataFrame(columns=["Session_id", "Timestamp", "Wealth"] + ATTRIBUTION_COLUMNS)
    if df is None or df.empty:
        return empty
    frame = df.copy()
    frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="coerce")
    frame["_mid"] = _col(frame, "Mid Price").fillna((_col(frame, "Best Bid") + _col(frame, "Best Ask")) / 2)
    frame["_inv"] = _col(frame, "inv_current")
    frame["_cash"] = _col(frame, "cash_current")
    frame["_fee"] = _col(frame, "Round Fee").fillna(0.0)
    if "Session_id" not in frame.columns:
        frame["Session_id"] = ""
    frame = frame.dropna(subset=["Timestamp"]).sort_values(["Session_id", "Timestamp"], kind="stable")
    parts = []
    for session, group in frame.groupby("Session_id", sort=False):
        g = group.copy()
        g[["_mid", "_inv", "_cash"]] = g[["_mid", "_inv", "_cash"]].ffill()
        g = g.dropna(subset=["_mid", "_inv", "_cash"])
        if g.empty:
            continue
        price = (g["_inv"].shift(1) * g["_mid"].diff()).fillna(0.0)
        trading = (g["_cash"].diff() + g["_inv"].diff() * g["_mid"]).fillna(0.0)
        fees = g["_fee"].copy()
        fees.iloc[0] = 0.0  # the baseline round attributes nothing
        out = pd.DataFrame({
            "Session_id": session, "Timestamp": g["Timestamp"].values,
            "Mid": g["_mid"].values, "Inventory": g["_inv"].values, "Cash": g["_cash"].values,
            "Wealth": (g["_cash"] + g["_inv"] * g["_mid"]).values,
            "Price PnL": price.values, "Trading PnL": trading.values,
            "Total PnL": (price + trading).values, "Fees": fees.values,
        })
        for column in ATTRIBUTION_COLUMNS:
            out[f"{column} (cum)"] = out[column].cumsum()
        parts.append(out)
    if not parts:
        return empty
    return pd.concat(parts, ignore_index=True)


def attribution_totals(att: pd.DataFrame) -> Dict[str, Any]:
    """Session sums of pnl_attribution() (added across sessions in the
    comparison view), the starting wealth, and the verdict: whichever of
    |trading| and |price| is larger made the result."""
    if att is None or att.empty:
        return {"total": None, "trading": None, "price": None, "fees": None,
                "start_wealth": None, "verdict": "Nothing yet", "sessions": 0}
    total = float(att["Total PnL"].sum())
    trading = float(att["Trading PnL"].sum())
    price = float(att["Price PnL"].sum())
    if abs(total) < 1e-9 and abs(trading) < 1e-9 and abs(price) < 1e-9:
        verdict = "Nothing yet"
    elif abs(trading) >= abs(price):
        verdict = "Mainly trading"
    else:
        verdict = "Mainly price"
    return {
        "total": total, "trading": trading, "price": price,
        "fees": float(att["Fees"].sum()),
        "start_wealth": float(att.groupby("Session_id", sort=False)["Wealth"].first().sum()),
        "verdict": verdict, "sessions": int(att["Session_id"].nunique()),
    }


def _demo_worker_running(pair: str) -> bool:
    """Read the pair worker's lease without mutating Dashboard state."""
    symbol = str(TRADING_PROFILES[pair]["pair"]).lower()
    lock_path = Path(DEMO_STATE_DIR) / f".worker_{symbol}.lock"
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("r") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def load_effective_demo_config(track_key: str) -> Optional[dict]:
    """Resolve desired and current-session runtime config for a Demo track."""
    track = TRACKS.get(track_key)
    pair = track_key.removesuffix("_demo")
    if not track or track["status"] != "demo" or pair not in TRADING_PROFILES:
        return None

    state_dir = Path(DEMO_STATE_DIR)
    desired_error = None
    try:
        desired = load_desired_config(pair, state_dir, TRADING_PROFILES)
    except (OverlayLoadError, OverlayValidationError) as error:
        desired = {}
        desired_error = str(error)
    runtime_error = None
    try:
        snapshot = read_runtime_snapshot(state_dir, pair)
    except OverlayLoadError as error:
        snapshot = None
        runtime_error = str(error)
    try:
        current_session_id = load_stack_session(state_dir / "stack_session.json")
    except SessionError:
        current_session_id = None

    worker_running = _demo_worker_running(pair)
    current_snapshot = snapshot if current_session_id is not None else None
    if desired_error is None:
        status = config_status(
            pair,
            desired,
            current_snapshot,
            worker_running,
            current_session_id=current_session_id,
        )
    else:
        profile_values = {
            field: TRADING_PROFILES[pair][field]
            for field in editable_schema(pair, TRADING_PROFILES)
        }
        snapshot_status = config_status(
            pair,
            profile_values,
            current_snapshot,
            worker_running,
            current_session_id=current_session_id,
        )
        applied = snapshot_status["applied"]
        failed_label = CONFIG_STATUS_LABELS["failed"]
        failed_fields = {
            field: dict(
                row,
                desired=None,
                applied=applied.get(field),
                state="failed",
                label=failed_label,
            )
            for field, row in snapshot_status["fields"].items()
        }
        status = dict(
            snapshot_status,
            state="failed",
            label=failed_label,
            desired_digest=None,
            pending_new_session={},
            error=desired_error,
            fields=failed_fields,
        )
        runtime_error = "; ".join(
            message for message in (desired_error, runtime_error) if message
        )
    return {
        "pair": pair,
        "desired": desired,
        "applied": status["applied"],
        "pending_new_session": status["pending_new_session"],
        "status": status,
        "runtime_error": runtime_error,
    }


# ---------- Chart helpers ----------

def _empty_fig(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font=dict(color=MUTED, size=14))
    return _style(fig, message)


def _style(fig: go.Figure, title: str, height: Optional[int] = None) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(size=14, color=TEXT_PRIMARY),
                   x=0.01, xanchor="left", y=0.97, yanchor="top"),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT_FAMILY, color=TEXT_SECONDARY, size=12),
        hovermode="x unified",
        margin=dict(l=48, r=24, t=78, b=36),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=11)),
    )
    if height:
        fig.update_layout(height=height)
    fig.update_xaxes(gridcolor=GRIDLINE, linecolor=BASELINE, showline=True, zeroline=False)
    fig.update_yaxes(gridcolor=GRIDLINE, linecolor=BASELINE, showline=True, zeroline=False)
    return fig


def _line_chart(df: pd.DataFrame, y_columns: List[str], title: str,
                color_by: Optional[str] = None) -> go.Figure:
    """One measure family per chart (single y-axis). If color_by is set
    (session comparison), each session gets its own trace per y-column;
    otherwise each y-column gets one fixed categorical color."""
    fig = go.Figure()
    if color_by:
        for i, (session, group) in enumerate(df.groupby(color_by)):
            col = y_columns[0]
            fig.add_trace(go.Scatter(
                x=group["Timestamp"], y=group[col], mode="lines+markers",
                name=str(session), line=dict(color=SERIES[i % len(SERIES)], width=2),
                marker=dict(size=5),
            ))
    else:
        for i, col in enumerate(y_columns):
            fig.add_trace(go.Scatter(
                x=df["Timestamp"], y=df[col], mode="lines", name=col,
                line=dict(color=SERIES[i % len(SERIES)], width=2),
            ))
    return _style(fig, title)


def _quotes_chart(df: pd.DataFrame) -> go.Figure:
    """Mid-price with our bid/ask as steps and our fills as dots."""
    fig = go.Figure()
    mid = _col(df, "Mid Price")
    bid = _col(df, "Bid Price" if "Bid Price" in df.columns else "Bid Price Sent")
    ask = _col(df, "Ask Price" if "Ask Price" in df.columns else "Ask Price Sent")
    fig.add_trace(go.Scatter(x=df["Timestamp"], y=mid, mode="lines", name="mid",
                             line=dict(color=TEXT_PRIMARY, width=1.5)))
    fig.add_trace(go.Scatter(x=df["Timestamp"], y=bid, mode="lines", name="our bid",
                             line=dict(color=BID_COLOR, width=1.5, shape="hv"), connectgaps=False))
    fig.add_trace(go.Scatter(x=df["Timestamp"], y=ask, mode="lines", name="our ask",
                             line=dict(color=ASK_COLOR, width=1.5, shape="hv"), connectgaps=False))
    events = fill_events(df)
    if not events.empty:
        fig.add_trace(go.Scatter(
            x=events["Timestamp"], y=events["price"], mode="markers", name="fill",
            marker=dict(color=FILL_COLOR, size=9, line=dict(color="white", width=1)),
            text=[f"{s} {a:g}" for s, a in zip(events["side"], events["amount"])],
        ))
    fig = _style(fig, "Mid-price, our quotes and fills")
    fig.update_yaxes(tickformat=",")
    return fig


def _distance_histogram(df: pd.DataFrame) -> go.Figure:
    distances = quote_distances(df)
    if distances.empty:
        return _empty_fig("No quotes yet")
    fig = go.Figure()
    for side, color in (("bid", BID_COLOR), ("ask", ASK_COLOR)):
        values = distances[distances["side"] == side]["distance"] * 100
        if values.empty:
            continue
        fig.add_trace(go.Histogram(x=values, name=side, marker_color=color, opacity=0.8,
                                   xbins=dict(start=0, end=max(2.5, float(values.max()) * 1.05), size=0.05)))
    fig.update_layout(barmode="group")
    fig = _style(fig, "Where the quotes sit — distance from the mid (%)")
    fig.update_xaxes(title_text="% from mid")
    fig.update_yaxes(title_text="quotes")
    return fig


def _reasons_chart(df: pd.DataFrame) -> go.Figure:
    modes = quote_mode_counts(df)
    reasons = no_quote_reason_counts(df)
    labels, values, colors = [], [], []
    for key, label, color in (("pair", "quoted (pair)", FILL_COLOR), ("bid_only", "quoted (bid only)", BID_COLOR),
                              ("ask_only", "quoted (ask only)", ASK_COLOR)):
        labels.append(label); values.append(modes[key]); colors.append(color)
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1])[:8]:
        labels.append(f"no quote: {reason}"); values.append(count); colors.append(MUTED)
    total = modes["total"] or 1
    fig = go.Figure(go.Bar(x=[v / total * 100 for v in values], y=labels, orientation="h",
                           marker_color=colors, text=[f"{v / total:.1%}" for v in values], textposition="outside"))
    fig = _style(fig, "Decisions — share of rounds")
    fig.update_layout(hovermode="y", margin=dict(l=220))
    fig.update_xaxes(title_text="% of rounds", range=[0, 110])
    fig.update_yaxes(autorange="reversed")
    return fig


def _inventory_sizes_chart(df: pd.DataFrame, settings: Dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["Timestamp"], y=_col(df, "inv_current"), mode="lines", name="inventory",
                             line=dict(color=BAR, width=2, shape="hv"), fill="tozeroy", fillcolor="rgba(18,51,58,0.08)"))
    for prefix, name, color in (("Buy", "buy size", BID_COLOR), ("Sell", "sell size", ASK_COLOR)):
        column = f"{prefix} Original Amount" if f"{prefix} Original Amount" in df.columns else f"{prefix} Size"
        sizes = _col(df, column)
        sizes = sizes.where(sizes > 0)
        fig.add_trace(go.Scatter(x=df["Timestamp"], y=sizes, mode="markers", name=name, yaxis="y2",
                                 marker=dict(color=color, size=6, symbol="diamond")))
    fig = _style(fig, "Inventory inside the band, with the sizes sent")
    inv_min, inv_max = settings.get("inv_min"), settings.get("inv_max")
    if inv_min is not None and inv_max is not None:
        fig.add_hline(y=float(inv_max), line=dict(color=STATUS_CRITICAL, dash="dot", width=1), annotation_text="inv_max")
        fig.add_hline(y=float(inv_min), line=dict(color=STATUS_CRITICAL, dash="dot", width=1), annotation_text="inv_min")
    fig.update_layout(yaxis2=dict(title="order size", overlaying="y", side="right", showgrid=False))
    return fig


def _history_strip(df: pd.DataFrame, rounds: int = 80) -> go.Figure:
    recent = df.tail(rounds)
    colors = {"pair": FILL_COLOR, "bid_only": BID_COLOR, "ask_only": ASK_COLOR, "none": MUTED}
    if "Quote Mode" in recent.columns and recent["Quote Mode"].notna().any():
        modes = recent["Quote Mode"].fillna("none").astype(str)
    else:
        modes = pd.Series(["pair" if s == "quote_generated" else "none" for s in recent.get("Quote Status", pd.Series(["none"] * len(recent)))], index=recent.index)
    labels = recent["Round"].astype(str) if "Round" in recent.columns else recent["Timestamp"].astype(str)
    fig = go.Figure(go.Bar(x=labels, y=[1] * len(recent), marker_color=[colors.get(m, MUTED) for m in modes],
                           hovertext=[f"round {r}: {m}" for r, m in zip(labels, modes)], hoverinfo="text"))
    fig = _style(fig, f"Decision history — last {len(recent)} rounds (teal pair · blue bid only · orange ask only · grey none)", height=170)
    fig.update_layout(bargap=0.15, hovermode="closest", margin=dict(l=24, r=24, t=48, b=24))
    fig.update_yaxes(visible=False)
    fig.update_xaxes(showgrid=False, nticks=12)
    return fig


def _fill_curve_chart(table: Optional[Dict[str, Any]], settings: Dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    distances = [d / 1000 for d in range(1, 26)]
    kappa = float(settings.get("fill_kappa") or 400.0)
    lambda_tau = float(settings.get("fill_lambda_tau") or 1.0)
    fig.add_trace(go.Scatter(x=[d * 100 for d in distances], y=[formula_probability(d, kappa, lambda_tau) for d in distances],
                             mode="lines", name=f"formula (κ={kappa:g}, λτ={lambda_tau:g})",
                             line=dict(color=MUTED, dash="dot", width=1.5)))
    if table:
        buckets = sorted(table["buckets"], key=lambda b: float(b.get("distance", 0)))
        x = [float(b["distance"]) * 100 for b in buckets]
        for side, key, color in (("bid", "p_bid", BID_COLOR), ("ask", "p_ask", ASK_COLOR)):
            samples = [int(b.get(f"n_{side}", 0)) for b in buckets]
            fig.add_trace(go.Scatter(
                x=x, y=[float(b.get(key, 0)) for b in buckets], mode="lines+markers",
                name=f"measured · {side}", line=dict(color=color, width=2),
                marker=dict(size=[6 + min(10, n ** 0.5 / 4) for n in samples]),
                customdata=samples, hovertemplate="%{y:.3f} (n=%{customdata})",
            ))
    fig = _style(fig, "Fill probability by distance — P(fill within one order lifetime); marker size = samples")
    fig.update_xaxes(title_text="% from the quote centre")
    fig.update_yaxes(title_text="probability", range=[0, 1])
    return fig


def _calibration_chart(rows: List[Dict[str, Any]]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="perfect", line=dict(color=MUTED, dash="dash")))
    for side, color in (("bid", BID_COLOR), ("ask", ASK_COLOR)):
        points = [r for r in rows if r["side"] == side and r["predicted"] is not None]
        if not points:
            continue
        fig.add_trace(go.Scatter(x=[p["predicted"] for p in points], y=[p["realised"] for p in points], mode="markers",
                                 name=f"{side} buckets", marker=dict(color=color, size=[6 + min(12, p["n"] ** 0.5) for p in points]),
                                 text=[f"{p['distance']*100:.2f}% · n={p['n']}" for p in points]))
    fig = _style(fig, "Calibration — predicted vs realised fill rate per bucket")
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(title_text="predicted p", range=[0, 1])
    fig.update_yaxes(title_text="realised fill rate", range=[0, 1])
    return fig


def _adverse_chart(adverse: Dict[int, Dict[str, Optional[float]]]) -> go.Figure:
    horizons = sorted(adverse)
    values = [adverse[h]["mean"] for h in horizons]
    if all(v is None for v in values):
        return _empty_fig("No fills yet")
    fig = go.Figure(go.Bar(x=[f"{h} s" for h in horizons], y=[(v or 0) * 100 for v in values],
                           marker_color=[STATUS_CRITICAL if (v or 0) < 0 else STATUS_GOOD for v in values],
                           text=[f"n={adverse[h]['n']}" for h in horizons], textposition="outside"))
    fig = _style(fig, "Adverse selection — mid move after our fills (% of price; negative = against us)")
    fig.update_layout(hovermode="x")
    return fig


def _fills_by_day_chart(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return _empty_fig("No orders yet")
    fig = go.Figure()
    fig.add_trace(go.Bar(x=frame["day"].astype(str), y=frame["predicted"], name="predicted fills", marker_color="#bfe3dd"))
    fig.add_trace(go.Bar(x=frame["day"].astype(str), y=frame["realised"], name="realised fills", marker_color=FILL_COLOR))
    fig.update_layout(barmode="group")
    fig = _style(fig, "Fill-rate forecast quality — predicted vs realised fills per day")
    fig.update_xaxes(type="category")
    return fig


def _position_chart(df: pd.DataFrame, settings: Dict[str, Any]) -> go.Figure:
    x = inventory_position(df, settings.get("inv_min") or 0.0, settings.get("inv_max") or 1.0)
    fig = go.Figure(go.Scatter(x=df["Timestamp"], y=x, mode="lines", name="x", line=dict(color=BAR, width=1.5)))
    for level in (settings.get("one_sided_lo") or 0.1, settings.get("one_sided_hi") or 0.9):
        fig.add_hline(y=float(level), line=dict(color=STATUS_CRITICAL, dash="dot", width=1))
    fig = _style(fig, "Inventory position in the band (one-sided thresholds dotted)")
    fig.update_yaxes(range=[0, 1])
    return fig


def _attribution_chart(att: pd.DataFrame, comparing: bool) -> go.Figure:
    """Running sums of price PnL (orange), trading PnL (blue) and their total
    (black). One set of lines per session; in the comparison view each
    session gets its own dash pattern and the session id in the legend."""
    fig = go.Figure()
    dashes = ("solid", "dash", "dot", "dashdot", "longdash")
    roles = (("Price PnL", ASK_COLOR, 2.0), ("Trading PnL", BID_COLOR, 2.0), ("Total PnL", TEXT_PRIMARY, 2.6))
    for i, (session, group) in enumerate(att.groupby("Session_id", sort=False)):
        suffix = f" — {session}" if comparing else ""
        for column, color, width in roles:
            fig.add_trace(go.Scatter(
                x=group["Timestamp"], y=group[f"{column} (cum)"], mode="lines", name=column + suffix,
                legendgroup=str(session),
                line=dict(color=color, width=width, dash=dashes[i % len(dashes)]),
            ))
    fig.add_hline(y=0, line=dict(color=BASELINE, width=1))
    return _style(fig, "Where the PnL came from — running sums, Total = Price + Trading (IRT)")


def _ev_heatmap(document: Dict[str, Any]) -> go.Figure:
    results = document.get("candidate_results") or []
    if not results:
        return _empty_fig("No pair was scored this round" + (f" ({document.get('selection_reason')})" if document.get("selection_reason") else ""))
    # bids ascending so the highest bid sits at the top, next to the lowest ask
    bids = sorted({float(r["submitted_bid_price"]) for r in results if r.get("submitted_bid_price") is not None})
    asks = sorted({float(r["submitted_ask_price"]) for r in results if r.get("submitted_ask_price") is not None})
    z = [[None] * len(asks) for _ in bids]
    text = [[""] * len(asks) for _ in bids]
    hover = [[""] * len(asks) for _ in bids]
    bar = float(document.get("min_required_return") or 0.0)
    for r in results:
        try:
            i = bids.index(float(r["submitted_bid_price"]))
            j = asks.index(float(r["submitted_ask_price"]))
        except (ValueError, TypeError, KeyError):
            continue
        ev = r.get("expected_value")
        net = r.get("net_return")
        z[i][j] = (ev or 0.0) * 100
        detail = (f"bid {bids[i]:,.0f} / ask {asks[j]:,.0f}<br>p_bid {float(r.get('p_bid') or 0):.3f} · "
                  f"p_ask {float(r.get('p_ask') or 0):.3f}<br>R_net {net * 100:.3f}% · J {ev * 100:.4f}%"
                  if ev is not None and net is not None else "")
        if r.get("passed"):
            hover[i][j] = detail + "<br>passes"
        else:
            reason = "fails the bar" if (net is not None and net <= bar) else "fails the EV floor"
            text[i][j] = "✗"
            hover[i][j] = f"{detail}<br>{reason}"
    fig = go.Figure(go.Heatmap(
        z=z, x=[f"{a:,.0f}" for a in asks], y=[f"{b:,.0f}" for b in bids], text=text, texttemplate="%{text}",
        textfont=dict(size=13, color=STATUS_CRITICAL),
        colorscale=[[0, "#f1f7f5"], [1, "#0f6e6e"]], showscale=True, colorbar=dict(title="J %"),
        customdata=hover, hovertemplate="%{customdata}<extra></extra>", xgap=3, ygap=3,
    ))
    sel_b, sel_a = document.get("selected_bid_price"), document.get("selected_ask_price")
    if sel_b is not None and sel_a is not None:
        fig.add_trace(go.Scatter(
            x=[f"{float(sel_a):,.0f}"], y=[f"{float(sel_b):,.0f}"], mode="markers", name="sent",
            marker=dict(symbol="square-open", size=36, line=dict(color=TEXT_PRIMARY, width=3)),
            hoverinfo="skip", showlegend=False,
        ))
    fig = _style(fig, "Expected value J = p_bid · p_ask · R_net of every pair (✗ rejected · □ sent)", height=420)
    fig.update_layout(hovermode="closest", margin=dict(l=110, b=60))
    fig.update_xaxes(title_text="ask candidates", type="category")
    fig.update_yaxes(title_text="bid candidates", type="category")
    return fig


def _candidate_table(document: Dict[str, Any]) -> html.Div:
    rows = []
    chosen = {("bid", document.get("selected_bid_price")), ("ask", document.get("selected_ask_price"))}
    for side in ("bid", "ask"):
        for c in document.get(f"candidate_{side}s") or []:
            rows.append({
                "side": side,
                "price": f"{float(c['price']):,.0f}",
                "distance": f"{float(c['distance']) * 100:.3f}%",
                "level": "synthetic" if c.get("level") is None else str(c.get("level")),
                "volume ahead": f"{float(c.get('volume_ahead') or 0):,.0f}",
                "p fill": f"{float(c.get('fill_probability') or 0):.3f}",
                "chosen": "◀ sent" if (side, c.get("price")) in chosen else "",
            })
    return dash_table.DataTable(
        columns=[{"name": c, "id": c} for c in ("side", "price", "distance", "level", "volume ahead", "p fill", "chosen")],
        data=rows, page_size=14,
        style_table={"overflowX": "auto"},
        style_header={"backgroundColor": PAGE, "fontWeight": 600, "color": TEXT_PRIMARY, "fontFamily": FONT_FAMILY,
                      "border": f"1px solid {GRIDLINE}", "fontSize": "0.8rem"},
        style_cell={"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.82rem",
                    "border": f"1px solid {GRIDLINE}", "padding": "0.35rem 0.5rem", "textAlign": "left"},
        style_data_conditional=[
            {"if": {"filter_query": '{chosen} contains "sent"'}, "backgroundColor": ACCENT_SOFT, "fontWeight": 600},
            {"if": {"filter_query": '{side} = "bid"', "column_id": "side"}, "color": BID_COLOR},
            {"if": {"filter_query": '{side} = "ask"', "column_id": "side"}, "color": ASK_COLOR},
        ],
    )


def _decision_trace(document: Dict[str, Any], settings: Dict[str, Any]) -> html.Div:
    def line(ok, text):
        mark = "✓" if ok else ("•" if ok is None else "✗")
        color = STATUS_GOOD if ok else (STATUS_WARN if ok is None else STATUS_CRITICAL)
        return html.Div([html.Span(mark, style={"color": color, "fontWeight": 700, "marginRight": "0.5rem"}), html.Span(text)],
                        style={"fontFamily": FONT_FAMILY, "fontSize": "0.88rem", "marginBottom": "0.35rem"})

    model = document.get("fill_model") or {}
    x = document.get("inventory_position")
    centre, mid = document.get("centre"), document.get("mid_price")
    pairs = document.get("candidate_results") or []
    passed = [r for r in pairs if r.get("passed")]
    reason = document.get("selection_reason") or "—"
    mode = document.get("quote_mode") or "none"
    items = [
        line(True, f"round {document.get('round')} · session {document.get('session_id')} · mid {mid:,.0f}" if mid else f"round {document.get('round')}"),
        line(True, f"inventory {float(document.get('inventory') or 0):g} → x = {x:.2f} → centre {centre:,.0f} ({(centre / mid - 1) * 100:+.3f}%)" if (x is not None and centre and mid) else "inventory position unavailable"),
        line(True, f"{len(document.get('candidate_bids') or [])} bid + {len(document.get('candidate_asks') or [])} ask candidates on the grid {', '.join(f'{d*100:.2f}%' for d in (document.get('distance_grid') or [])[:4])}…"),
        line(bool(model.get("table_fresh")) if model else None,
             f"fill model: {'measured table' if model.get('table_fresh') else 'formula'}"
             + (f" (updated {str(model.get('table_updated_at') or '')[:16]}, {model.get('table_buckets')} buckets)" if model.get("table_loaded") else f" (κ={model.get('formula_kappa')}, λτ={model.get('formula_lambda_tau')})")),
        line(bool(passed) if pairs else None, f"{len(passed)} of {len(pairs)} pairs clear the bar (R_net > {float(document.get('min_required_return') or 0) * 100:.2f}%) and the EV floor"),
    ]
    if document.get("cost_basis"):
        items.append(line(True, f"cost basis of held inventory {float(document['cost_basis']):,.0f}"))
    if mode == "pair":
        items.append(line(True, f"pair selected: bid {float(document['selected_bid_price']):,.0f} / ask {float(document['selected_ask_price']):,.0f}, J = {float(document.get('selected_expected_value') or 0) * 100:.4f}%"))
    elif mode in ("bid_only", "ask_only"):
        price = document.get("selected_bid_price") if mode == "bid_only" else document.get("selected_ask_price")
        items.append(line(True, f"{mode.replace('_', ' ')}: {float(price):,.0f} vs reference {float(document.get('reference_price') or 0):,.0f} ({reason})"))
    else:
        items.append(line(False, f"no quote: {reason}"))
    return html.Div(items)


def _status_badge(status: str) -> html.Span:
    if status == "real":
        color, icon, text = STATUS_CRITICAL, "●", "REAL — places real orders"
    else:
        color, icon, text = STATUS_GOOD, "●", "DEMO — simulated, no real orders"
    return html.Span([icon + " ", text], style={
        "color": color, "fontWeight": 600, "fontSize": "0.85rem",
        "border": f"1px solid {color}", "borderRadius": "999px",
        "padding": "0.25rem 0.75rem", "display": "inline-block", "background": SURFACE,
    })


def _card(*children, **style) -> html.Div:
    base = {
        "background": SURFACE, "border": f"1px solid {GRIDLINE}",
        "borderRadius": "10px", "padding": "1rem 1.1rem",
        "boxShadow": "0 1px 2px rgba(11,11,11,0.04)", "marginBottom": "1rem", "minWidth": 0,
    }
    base.update(style)
    return html.Div(children, style=base)


def _h3(text: str, sub: str = "") -> html.H3:
    return html.H3([text, html.Span(f"  {sub}", style={"fontWeight": 400, "color": MUTED, "textTransform": "none", "letterSpacing": 0})] if sub else text,
                   style={"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.8rem", "fontWeight": 600,
                          "textTransform": "uppercase", "letterSpacing": "0.04em", "marginTop": 0, "marginBottom": "0.5rem"})


def _tab_style() -> dict:
    return {
        "padding": "0.6rem 1.1rem", "fontFamily": FONT_FAMILY, "fontSize": "0.9rem", "fontWeight": 500,
        "color": "#c9d8d6", "backgroundColor": BAR, "border": "none", "borderRadius": "6px", "marginRight": "0.25rem",
    }


def _tab_selected_style() -> dict:
    return {
        "padding": "0.6rem 1.1rem", "fontFamily": FONT_FAMILY, "fontSize": "0.9rem", "fontWeight": 600,
        "color": "#ffffff", "backgroundColor": "#1f4d55", "border": "none", "borderRadius": "6px", "marginRight": "0.25rem",
    }


def _metric_card(label: str, value: str, color: str = TEXT_PRIMARY, detail: str = "") -> html.Div:
    """Create a metric card (KPI tile)."""
    children = [
        html.Div(label, style={
            "fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY,
            "fontSize": "0.72rem", "fontWeight": 500,
            "textTransform": "uppercase", "letterSpacing": "0.05em",
            "marginBottom": "0.25rem",
        }),
        html.Div(value, style={
            "fontFamily": FONT_FAMILY, "color": color,
            "fontSize": "1.45rem", "fontWeight": 700, "fontVariantNumeric": "tabular-nums",
        }),
    ]
    if detail:
        children.append(html.Div(detail, style={"fontFamily": FONT_FAMILY, "color": MUTED, "fontSize": "0.75rem", "marginTop": "0.15rem"}))
    return html.Div(children, style={
        "background": SURFACE, "border": f"1px solid {GRIDLINE}",
        "borderRadius": "10px", "padding": "0.8rem 1rem",
        "boxShadow": "0 1px 2px rgba(11,11,11,0.04)",
        "minWidth": "150px", "flex": "1 1 150px",
    })


# ---------- Dash app ----------

app = Dash(
    __name__,
    requests_pathname_prefix=os.environ.get(
        "DASHBOARD_REQUESTS_PATHNAME_PREFIX", "/"
    ),
    routes_pathname_prefix="/",
    suppress_callback_exceptions=True,
)
app.title = "Market-Making Dashboard"

_label_style = {"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY, "fontSize": "0.8rem", "fontWeight": 600}

app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    html.Div([
        html.Div([
            html.Span("Market-Maker ", style={"fontWeight": 700, "fontSize": "1.05rem", "color": "#e8f1f0"}),
            html.Span("Console", style={"fontWeight": 700, "fontSize": "1.05rem", "color": "#6cc3bd"}),
        ], style={"fontFamily": FONT_FAMILY, "marginRight": "1.5rem"}),
        dcc.Tabs(
            id="main-tabs",
            value="overview",
            children=[
                dcc.Tab(label="Overview", value="overview", style=_tab_style(), selected_style=_tab_selected_style()),
                dcc.Tab(label="Quote decision", value="decision", style=_tab_style(), selected_style=_tab_selected_style()),
                dcc.Tab(label="Fill model", value="fillmodel", style=_tab_style(), selected_style=_tab_selected_style()),
                dcc.Tab(label="Order book", value="orderbook", style=_tab_style(), selected_style=_tab_selected_style()),
                dcc.Tab(label="Validation", value="validation", style=_tab_style(), selected_style=_tab_selected_style()),
                dcc.Tab(label="Rounds", value="trading", style=_tab_style(), selected_style=_tab_selected_style()),
                dcc.Tab(label="PnL attribution", value="attribution", style=_tab_style(), selected_style=_tab_selected_style()),
            ],
            style={"flex": "1 1 auto"},
            parent_style={"flex": "1 1 auto"},
            content_style={"display": "none"},
        ),
        html.Div(id="status-badge", style={"marginLeft": "auto"}),
    ], style={"display": "flex", "alignItems": "center", "gap": "0.5rem", "background": BAR,
              "padding": "0.6rem 1.25rem", "borderRadius": "10px", "marginBottom": "1rem", "flexWrap": "wrap"}),

    _card(
        html.Div([
            html.Div([
                html.Label("Track", style=_label_style),
                dcc.Dropdown(
                    id="track",
                    options=[{"label": v["label"], "value": k} for k, v in TRACKS.items()],
                    value=default_track(), clearable=False,
                ),
            ]),
            html.Div([
                html.Label("Session", style=_label_style),
                dcc.Dropdown(id="session", clearable=False),
            ]),
            html.Div([
                html.Label("Risk-free rate % (annual, Validation tab)", style=_label_style),
                dcc.Input(id="risk-free-rate-pct", type="number", value=0.0, step=0.1, style={"width": "100%"}),
            ]),
            html.Div([
                html.Label("Target return % (annual, Sortino MAR)", style=_label_style),
                dcc.Input(id="target-return-pct", type="number", value=0.0, step=0.1, style={"width": "100%"}),
            ]),
        ], style={"display": "grid", "gridTemplateColumns": "1.2fr 1.6fr auto auto", "gap": "1rem", "alignItems": "start"}),
        html.Div(id="status-text", style={"color": MUTED, "fontFamily": FONT_FAMILY, "fontSize": "0.8rem", "marginTop": "0.5rem"}),
    ),

    html.Div(id="tab-content"),

    html.Div(
        _card(
            _h3("Effective Demo configuration"),
            html.Div(id="effective-config-summary", style={
                "color": MUTED, "fontFamily": FONT_FAMILY,
                "fontSize": "0.85rem", "marginBottom": "0.75rem",
            }),
            dash_table.DataTable(
                id="effective-config-table",
                columns=[{"name": field, "id": field} for field in (
                    "Field", "Desired", "Applied", "Mode", "Status",
                )],
                page_size=12,
                style_table={"overflowX": "auto"},
                style_header={
                    "backgroundColor": PAGE, "fontWeight": 600,
                    "color": TEXT_PRIMARY, "fontFamily": FONT_FAMILY,
                    "border": f"1px solid {GRIDLINE}",
                },
                style_cell={
                    "fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY,
                    "fontSize": "0.82rem", "border": f"1px solid {GRIDLINE}",
                    "padding": "0.4rem", "textAlign": "left",
                },
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": PAGE},
                ],
            ),
        ),
        id="effective-config-panel",
    ),

    dcc.Store(id="decision-round-store", storage_type="memory"),
    dcc.Interval(id="tick", interval=20_000, n_intervals=0),  # auto-refresh every 20s
], style={"background": PAGE, "padding": "1rem 1.5rem", "minHeight": "100vh", "fontFamily": FONT_FAMILY})


@app.callback(Output("main-tabs", "value"), Output("track", "value"), Input("url", "search"))
def apply_url(search):
    """Deep links: ?tab=decision&track=usdt_demo (&session=<id>, applied by
    refresh_session_options) open the page on that view."""
    params = _url_params(search)
    tab, track = params.get("tab"), params.get("track")
    return (tab if tab in TAB_VALUES else no_update, track if track in TRACKS else no_update)


@app.callback(Output("session", "options"), Output("session", "value"),
              Input("track", "value"), Input("tick", "n_intervals"),
              State("session", "value"), State("url", "search"))
def refresh_session_options(track_key, _, current_value, search=None):
    """Default the session picker to "All sessions (compare)" on first
    load (or to the session named in the deep link), not the newest single
    session, and keep the operator's choice across auto-refresh ticks
    (State never fires a callback); fall back to "all" only when the
    selected session no longer exists."""
    ids = available_sessions(TRACKS[track_key]["path"])
    if ids is None:
        return [], None
    options = [{"label": "All sessions (compare)", "value": "all"}] + [
        {"label": str(s), "value": s} for s in ids
    ]
    if current_value is None:
        wanted = _url_params(search).get("session")
        if wanted == "all" or wanted in ids:
            return options, wanted
    if current_value == "all" or current_value in ids:
        return options, current_value
    return options, "all"


@app.callback(Output("status-badge", "children"), Input("track", "value"))
def refresh_status_badge(track_key):
    return _status_badge(TRACKS[track_key]["status"])


@app.callback(
    Output("effective-config-panel", "style"),
    Output("effective-config-summary", "children"),
    Output("effective-config-table", "data"),
    Input("track", "value"),
    Input("tick", "n_intervals"),
)
def refresh_effective_config_panel(track_key, _):
    view = load_effective_demo_config(track_key)
    if view is None:
        return {"display": "none"}, "", []

    status = view["status"]
    summary = f"{view['pair'].upper()} Demo configuration — {status['label']}"
    if view["runtime_error"]:
        summary += f" ({view['runtime_error']})"
    rows = [
        {
            "Field": field,
            "Desired": row["desired"],
            "Applied": row["applied"],
            "Mode": row["mode"],
            "Status": row["label"],
        }
        for field, row in status["fields"].items()
    ]
    return {}, summary, rows


@app.callback(
    Output("status-text", "children"),
    Input("track", "value"),
    Input("session", "value"),
    Input("tick", "n_intervals"),
)
def refresh_status_text(track_key, session_id, _):
    track = TRACKS[track_key]
    df = load_track(track["path"], _session_filter(session_id))
    if df is None:
        return f"No data yet for {track['label']} ({track['path']})."
    df = filter_session(df, session_id)
    if df.empty:
        return "No rows for the selected session."
    comparing = session_id in (None, "all")
    last = pd.to_datetime(df["Timestamp"], errors="coerce").max()
    status = f"{len(df)} rounds" + ("" if comparing else f" — session {session_id}")
    if len(df) >= MAX_ROWS:
        status += f" (the newest {MAX_ROWS:,} rounds; older rows stay in the store)"
    if pd.notna(last):
        status += f" — last round {last:%Y-%m-%d %H:%M:%S}"
    return status + no_quote_summary(df)


@app.callback(
    Output("tab-content", "children"),
    Input("main-tabs", "value"),
    Input("track", "value"),
    Input("session", "value"),
    Input("tick", "n_intervals"),
    Input("risk-free-rate-pct", "value"),
    Input("target-return-pct", "value"),
    State("decision-round-store", "data"),
)
def render_tab_content(active_tab, track_key, session_id, _, risk_free_rate_pct, target_return_pct,
                       decision_round=None):
    """Render content for the active tab. On a refresh tick the Rounds tab
    is left alone: rebuilding the table resets the operator's scroll
    position, so refresh_rounds_table swaps its rows in place instead."""
    if active_tab == "trading" and _triggered_by_tick():
        return no_update
    track = TRACKS[track_key]

    df = load_track(track["path"], _session_filter(session_id))
    if df is None:
        return _no_data_message(track["label"], track["path"])

    df = filter_session(df, session_id)
    if df.empty:
        return _no_data_message("selected session", "")

    df = with_derived_spreads(df).sort_values("Timestamp")
    comparing = session_id in (None, "all")
    settings = track_settings(track_key)

    if active_tab == "overview":
        return _render_overview_tab(df, comparing, settings)
    elif active_tab == "decision":
        return _render_decision_tab(df, track_key, settings, selected=decision_round)
    elif active_tab == "fillmodel":
        return _render_fill_model_tab(df, track_key, settings)
    elif active_tab == "orderbook":
        return _render_orderbook_tab(df, comparing)
    elif active_tab == "trading":
        return _render_trading_tab(df, comparing)
    elif active_tab == "attribution":
        return _render_attribution_tab(df, comparing)
    elif active_tab == "validation":
        return _render_validation_tab(
            df, track_key, session_id,
            risk_free_rate=(risk_free_rate_pct or 0.0) / 100.0,
            target_return=(target_return_pct or 0.0) / 100.0,
        )

    return html.Div("Unknown tab")


@app.callback(Output("decision-round-store", "data"), Input("decision-round", "value"))
def remember_decision_round(selected):
    """Keep the operator's round choice across the 20 s refresh ticks, which
    rebuild the whole tab (and therefore the dropdown)."""
    return selected


def _triggered_by_tick() -> bool:
    try:
        return ctx.triggered_id == "tick"
    except Exception:          # called outside a callback (tests)
        return False


@app.callback(
    Output("activity-table", "data"),
    Input("tick", "n_intervals"),
    State("track", "value"),
    State("session", "value"),
    prevent_initial_call=True,
)
def refresh_rounds_table(_, track_key, session_id):
    """Refresh the Rounds table's rows without rebuilding the table."""
    df = load_track(TRACKS[track_key]["path"], _session_filter(session_id))
    if df is None:
        return no_update
    df = filter_session(df, session_id)
    if df.empty:
        return no_update
    return activity_records(with_derived_spreads(df).sort_values("Timestamp"))


@app.callback(
    Output("decision-panels", "children"),
    Input("decision-round", "value"),
    State("track", "value"),
)
def refresh_decision_panels(selected, track_key):
    documents = load_diagnostics(diagnostics_path(track_key), limit=DECISION_ROUNDS)
    document = _pick_document(documents, selected)
    if document is None:
        return _no_data_message("decision diagnostics", diagnostics_path(track_key))
    return _decision_panels(document, track_settings(track_key))


def _round_key(document: Dict[str, Any]) -> str:
    return f"{document.get('session_id')}:{document.get('round')}"


def _pick_document(documents: List[Dict[str, Any]], selected) -> Optional[Dict[str, Any]]:
    """The document the operator selected, else the newest one."""
    if not documents:
        return None
    return next((d for d in documents if _round_key(d) == selected), documents[-1])


def _no_data_message(label: str, path: str) -> html.Div:
    """Return a standardized no-data message."""
    msg = f"No data yet for {label}" + (f" ({path})" if path else ".")
    return html.Div([
        _card(html.P(msg, style={"color": MUTED, "fontFamily": FONT_FAMILY, "textAlign": "center", "padding": "2rem"})),
    ])


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value * 100:.{digits}f} %"


def _render_overview_tab(df: pd.DataFrame, comparing: bool, settings: Dict[str, Any]) -> html.Div:
    """Overview: KPI tiles, quotes with fills, quote distances, decisions, inventory."""
    fee = float(settings.get("commission_perc") or 0.0)
    modes = quote_mode_counts(df)
    total = modes["total"] or 1
    quoted = (modes["pair"] + modes["bid_only"] + modes["ask_only"]) / total
    fph = fills_per_hour(df)
    events = fill_events(df)
    spread = realised_vs_computed_spread(df, fee)
    adverse = adverse_selection(df, (60,)).get(60, {"mean": None, "n": 0})
    wealth = _col(df, "Portfolio Wealth").dropna()
    pnl = float(wealth.iloc[-1] - wealth.iloc[0]) if len(wealth) >= 2 else None
    fees = float(_col(df, "Round Fee").fillna(0).sum()) if "Round Fee" in df.columns else None
    x = inventory_position(df, settings.get("inv_min") or 0.0, settings.get("inv_max") or 1.0)
    inventory = _col(df, "inv_current").dropna()
    x_last = float(x.dropna().iloc[-1]) if x.notna().any() else None

    spread_value = f"{_fmt_pct(spread['realised'])} / {_fmt_pct(spread['computed'])}"
    spread_color = TEXT_PRIMARY
    if spread["realised"] is not None and spread["computed"] is not None:
        spread_color = STATUS_GOOD if spread["realised"] >= spread["computed"] - 0.0005 else STATUS_WARN
    tiles = html.Div([
        _metric_card("Quote rate (rounds)", f"{quoted:.0%}", STATUS_GOOD if quoted >= 0.5 else STATUS_WARN,
                     f"pair {modes['pair'] / total:.0%} · bid only {modes['bid_only'] / total:.0%} · ask only {modes['ask_only'] / total:.0%} · none {modes['none'] / total:.0%}"),
        _metric_card("Fills / hour", "—" if fph is None else f"{fph:.2f}", TEXT_PRIMARY,
                     f"buy {int((events['side'] == 'buy').sum()) if not events.empty else 0} · sell {int((events['side'] == 'sell').sum()) if not events.empty else 0} · {span_hours(df):.1f} h of data"),
        _metric_card("Realised vs computed spread", spread_value, spread_color, f"per round trip, after fees · {spread['trips']} trips"),
        _metric_card("Adverse selection (60 s)", _fmt_pct(adverse["mean"]), STATUS_CRITICAL if (adverse["mean"] or 0) < 0 else STATUS_GOOD,
                     f"mid move after our fills · n = {adverse['n']}"),
        _metric_card("Net PnL after fees", "—" if pnl is None else f"{pnl:+,.0f}", STATUS_GOOD if (pnl or 0) >= 0 else STATUS_CRITICAL,
                     ("—" if pnl is None or not wealth.iloc[0] else f"{pnl / float(wealth.iloc[0]):+.3%} of starting wealth") + (f" · fees {fees:,.0f}" if fees else "")),
        _metric_card("Inventory position", "—" if x_last is None else f"x = {x_last:.2f}", TEXT_PRIMARY,
                     f"{inventory.iloc[-1]:g} held · band {settings.get('inv_min')} – {settings.get('inv_max')}" if not inventory.empty else ""),
    ], style={"display": "flex", "gap": "0.75rem", "flexWrap": "wrap", "marginBottom": "1rem"})

    return html.Div([
        tiles,
        html.Div([
            _card(dcc.Graph(figure=_quotes_chart(df), config={"displaylogo": False})),
            _card(dcc.Graph(figure=_distance_histogram(df), config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "2fr 1fr", "gap": "1rem"}),
        html.Div([
            _card(dcc.Graph(figure=_inventory_sizes_chart(df, settings), config={"displaylogo": False})),
            _card(dcc.Graph(figure=_reasons_chart(df), config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "2fr 1fr", "gap": "1rem"}),
        _card(dcc.Graph(figure=_line_chart(df, ["Portfolio Wealth"], "Portfolio wealth",
                                           color_by="Session_id" if comparing else None), config={"displaylogo": False})),
    ])


def _decision_panels(document: Dict[str, Any], settings: Dict[str, Any]) -> html.Div:
    return html.Div([
        html.Div([
            _card(_h3("Candidates", "highlighted = sent"), _candidate_table(document)),
            _card(dcc.Graph(figure=_ev_heatmap(document), config={"displaylogo": False})),
            _card(_h3("Decision trace", "this round"), _decision_trace(document, settings)),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1.3fr 1fr", "gap": "1rem"}),
    ])


def _render_decision_tab(df: pd.DataFrame, track_key: str, settings: Dict[str, Any], selected=None) -> html.Div:
    documents = load_diagnostics(diagnostics_path(track_key), limit=DECISION_ROUNDS)
    options = [
        {"label": f"round {d.get('round')} · {str(d.get('session_id'))[:24]}", "value": _round_key(d)}
        for d in reversed(documents)
    ]
    document = _pick_document(documents, selected)
    value = _round_key(document) if document is not None else None
    header = _card(html.Div([
        html.Div([html.Label("Round", style=_label_style),
                  dcc.Dropdown(id="decision-round", options=options, value=value, clearable=False)],
                 style={"minWidth": "320px"}),
        html.Div(
            f"fee {float(settings.get('commission_perc') or 0) * 100:.2f}% · required net {float(settings.get('exp_ret_min') or 0) * 100:.2f}% · "
            f"gross bar {float(settings.get('gross_bar') or 0) * 100:.3f}% · lifetime {settings.get('cancel_after_minutes')} min · "
            f"EV floor {float(settings.get('ev_min') or 0) * 100:.4f}%",
            style={"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY, "fontSize": "0.85rem", "alignSelf": "end"}),
    ], style={"display": "flex", "gap": "1.5rem", "alignItems": "end", "flexWrap": "wrap"}))
    if document is None:
        body = _no_data_message("decision diagnostics (written by the worker while it runs)", diagnostics_path(track_key))
    else:
        body = _decision_panels(document, settings)
    return html.Div([
        header,
        html.Div(body, id="decision-panels"),
        _card(dcc.Graph(figure=_history_strip(df), config={"displaylogo": False})),
    ])


def _render_fill_model_tab(df: pd.DataFrame, track_key: str, settings: Dict[str, Any]) -> html.Div:
    path = fill_table_path(track_key)
    table = load_fill_table(path)
    rows = calibration_rows(df)
    adverse = adverse_selection(df, (10, 30, 60, 300, 600))
    health = []
    if table:
        updated = str(table.get("updated_at", ""))[:19].replace("T", " ")
        health += [
            html.Div(f"✓ {os.path.basename(path)} — method {table.get('method')} · updated {updated} UTC"),
            html.Div(f"✓ {table.get('samples', '?')} price-path samples · {table.get('own_orders', 0)} own orders · window {table.get('window_days')} d · half-life {table.get('half_life_days')} d"),
            html.Div(f"✓ horizon {table.get('horizon_seconds')} s · {len(table.get('buckets', []))} buckets · sources {', '.join(table.get('sources', []))}"),
        ]
    else:
        health.append(html.Div(f"✗ no fill table at {path} — the strategy uses the formula (κ = {settings.get('fill_kappa')}, λτ = {settings.get('fill_lambda_tau')})"))
    if rows:
        health.append(html.Div(f"✓ calibration from {sum(r['n'] for r in rows)} own terminal orders in this view"))
    else:
        health.append(html.Div("• no own orders with an outcome yet — calibration appears after the first fills/cancels"))
    table_rows = []
    if table:
        for b in sorted(table["buckets"], key=lambda b: float(b.get("distance", 0))):
            table_rows.append({"distance": f"{float(b['distance']) * 100:.2f}%", "p bid": f"{float(b.get('p_bid', 0)):.3f}",
                               "p ask": f"{float(b.get('p_ask', 0)):.3f}", "n bid": int(b.get("n_bid", 0)), "n ask": int(b.get("n_ask", 0)),
                               "own bid": int(b.get("n_bid_own", 0)), "own ask": int(b.get("n_ask_own", 0))})
    return html.Div([
        html.Div([
            _card(dcc.Graph(figure=_fill_curve_chart(table, settings), config={"displaylogo": False})),
            _card(dcc.Graph(figure=_calibration_chart(rows), config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "1.4fr 1fr", "gap": "1rem"}),
        html.Div([
            _card(dcc.Graph(figure=_adverse_chart(adverse), config={"displaylogo": False})),
            _card(_h3("Table health"), html.Div(health, style={"fontFamily": FONT_FAMILY, "fontSize": "0.88rem", "lineHeight": 1.8, "color": TEXT_SECONDARY})),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1rem"}),
        _card(_h3("Measured table", "p = P(fill within one lifetime)"), dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ("distance", "p bid", "p ask", "n bid", "n ask", "own bid", "own ask")],
            data=table_rows, style_table={"overflowX": "auto"},
            style_header={"backgroundColor": PAGE, "fontWeight": 600, "color": TEXT_PRIMARY, "fontFamily": FONT_FAMILY, "border": f"1px solid {GRIDLINE}"},
            style_cell={"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.82rem", "border": f"1px solid {GRIDLINE}", "padding": "0.35rem 0.5rem", "textAlign": "right"},
        )) if table_rows else html.Div(),
    ])


def _render_orderbook_tab(df: pd.DataFrame, comparing: bool) -> html.Div:
    """Render the Order Book tab (levels, spreads)."""
    levels_fig = _line_chart(df, ["Bid Level", "Ask Level"], "Selected depth levels")
    spreads_fig = _line_chart(df, ["Bid Spread", "Ask Spread"], "Quoted spreads — mid − bid, ask − mid (price units)")
    cards = [
        _card(dcc.Graph(id="levels-chart", figure=levels_fig, config={"displaylogo": False})),
        _card(dcc.Graph(id="spreads-chart", figure=spreads_fig, config={"displaylogo": False})),
    ]
    if "Relative Quoted Spread" in df.columns and _col(df, "Relative Quoted Spread").notna().any():
        relative = df.assign(**{"Relative Quoted Spread %": _col(df, "Relative Quoted Spread") * 100})
        cards.append(_card(dcc.Graph(
            figure=_line_chart(relative, ["Relative Quoted Spread %"], "Relative quoted spread (ask − bid) / mid, %"),
            config={"displaylogo": False},
        )))

    return html.Div([
        html.Div(cards[:2], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1rem"}),
        *cards[2:],
    ])


def _render_trading_tab(df: pd.DataFrame, comparing: bool) -> html.Div:
    """Render the Rounds tab (one row per round)."""
    activity = activity_records(df)

    return html.Div([
        _card(
            _h3("Rounds", "newest first"),
            dash_table.DataTable(
                id="activity-table",
                columns=[{"name": c, "id": c} for c in ACTIVITY_COLUMNS],
                data=activity,
                page_size=20,
                sort_action="native",
                filter_action="native",
                style_table={"overflowX": "auto"},
                style_header={"backgroundColor": PAGE, "fontWeight": 600, "color": TEXT_PRIMARY,
                              "fontFamily": FONT_FAMILY, "border": f"1px solid {GRIDLINE}"},
                style_cell={"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.82rem",
                            "border": f"1px solid {GRIDLINE}", "padding": "0.4rem"},
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": PAGE},
                ],
            ),
        ),
    ])


def _render_attribution_tab(df: pd.DataFrame, comparing: bool) -> html.Div:
    """PnL attribution: did the result come from our quotes or from the price?
    Tiles with the session sums and the verdict, then the one chart."""
    att = pnl_attribution(df)
    if att.empty:
        return _no_data_message("PnL attribution (rows need inv_current, cash_current and a mid price)", "")
    totals = attribution_totals(att)
    start = totals["start_wealth"] or 0.0

    def share(value: float) -> str:
        return f"{value / start:+.3%} of starting wealth" if start else ""

    def tone(value: float) -> str:
        return STATUS_GOOD if value >= 0 else STATUS_CRITICAL

    scope = f"across {totals['sessions']} sessions" if comparing and totals["sessions"] > 1 else "this session"
    fees_detail = (f"gross edge {totals['trading'] + totals['fees']:+,.0f} · fees {totals['fees']:,.0f}"
                   if totals["fees"] else "fees are already inside this number")
    verdict_detail = (f"|trading| {abs(totals['trading']):,.0f} vs |price| {abs(totals['price']):,.0f} · "
                      + ("our own quoting made the result" if totals["verdict"] == "Mainly trading"
                         else "the price moved while we held inventory" if totals["verdict"] == "Mainly price"
                         else "no fills and no price move yet"))
    tiles = html.Div([
        _metric_card("Total PnL", f"{totals['total']:+,.0f}", tone(totals["total"]),
                     f"wealth − starting wealth, {scope} · {share(totals['total'])}"),
        _metric_card("Trading PnL (our fills, net of fees)", f"{totals['trading']:+,.0f}", tone(totals["trading"]), fees_detail),
        _metric_card("Price PnL (market moves on held inventory)", f"{totals['price']:+,.0f}", tone(totals["price"]),
                     share(totals["price"])),
        _metric_card("Verdict", totals["verdict"], TEXT_PRIMARY, verdict_detail),
    ], style={"display": "flex", "gap": "0.75rem", "flexWrap": "wrap", "marginBottom": "1rem"})

    text_style = {"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.9rem", "margin": "0 0 0.4rem"}
    mono = {"fontFamily": "ui-monospace, Consolas, monospace", "fontSize": "0.85rem", "color": TEXT_PRIMARY}
    how = _card(
        _h3("How to read it"),
        html.P("Every round the engine records cash, inventory and the mid price, so wealth is "
               "W = cash + inventory × mid. The change of W from one round to the next splits "
               "exactly into two parts:", style=text_style),
        html.P(html.Span("Price PnL   = inventory held at the start of the round × (mid now − mid before)", style=mono), style=text_style),
        html.P(html.Span("Trading PnL = change of cash + change of inventory × mid now   (our fills against the mid, net of fees)", style=mono), style=text_style),
        html.P("Orange is what a passive holder of our inventory would have made; blue is what our quotes "
               "earned if the price had never moved; black is their sum, the real result. Where black hugs "
               "orange the market made the result; where it hugs blue, we did. Fills are marked at the mid "
               "of the round they were processed in, so a few seconds of price drift inside a round leaks "
               "into the blue line. Derivation and a worked example: docs/algorithms/PNL_ATTRIBUTION.md.",
               style=text_style),
    )
    return html.Div([
        tiles,
        _card(dcc.Graph(figure=_attribution_chart(att, comparing), config={"displaylogo": False})),
        how,
    ])


def _gate_table(rows: List[Dict[str, Any]]) -> html.Div:
    data = [{"metric": r["metric"], "value": r["display"], "gate": r["threshold"],
             "result": "✓ pass" if r["passed"] else ("✗ fail" if r["passed"] is not None else "• not measurable yet")} for r in rows]
    passed = sum(1 for r in rows if r["passed"])
    measurable = sum(1 for r in rows if r["passed"] is not None)
    return html.Div([
        html.Div(f"gate: {passed} of {measurable} measurable conditions passed ({len(rows) - measurable} not measurable yet)",
                 style={"fontFamily": FONT_FAMILY, "fontWeight": 600, "marginBottom": "0.5rem",
                        "color": STATUS_GOOD if passed == len(rows) else STATUS_WARN}),
        dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ("metric", "value", "gate", "result")], data=data,
            style_table={"overflowX": "auto"},
            style_header={"backgroundColor": PAGE, "fontWeight": 600, "color": TEXT_PRIMARY, "fontFamily": FONT_FAMILY, "border": f"1px solid {GRIDLINE}"},
            style_cell={"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.85rem", "border": f"1px solid {GRIDLINE}", "padding": "0.4rem 0.6rem", "textAlign": "left"},
            style_data_conditional=[
                {"if": {"filter_query": '{result} contains "pass"', "column_id": "result"}, "color": STATUS_GOOD, "fontWeight": 600},
                {"if": {"filter_query": '{result} contains "fail"', "column_id": "result"}, "color": STATUS_CRITICAL, "fontWeight": 600},
            ],
        ),
    ])


def _render_validation_tab(
    df: pd.DataFrame, track_key: str, session_id,
    risk_free_rate: float = 0.0, target_return: float = 0.0,
) -> html.Div:
    """Render the Validation tab: the demo-to-real gate and the metrics."""
    settings = track_settings(track_key)
    engine = create_validation_engine(
        risk_free_rate=risk_free_rate, target_return=target_return
    )
    result = engine.validate_from_csv(df)
    report = generate_validation_report(result)

    metrics = report["summary"]

    def metric_color(value, higher_is_better=True):
        if value is None:
            return MUTED
        if higher_is_better:
            return STATUS_GOOD if value > 0 else STATUS_CRITICAL
        return STATUS_CRITICAL if value > 0 else STATUS_GOOD

    equity_df = pd.DataFrame({
        "Timestamp": result.equity_curve.index,
        "Equity": result.equity_curve.values
    })
    equity_fig = _line_chart(equity_df, ["Equity"], "Equity Curve")

    drawdown_df = pd.DataFrame({
        "Timestamp": result.drawdown_curve.index,
        "Drawdown": result.drawdown_curve.values
    })
    drawdown_fig = _line_chart(drawdown_df, ["Drawdown"], "Drawdown Curve")
    if drawdown_fig.data:
        drawdown_fig.data[0].fill = "tozeroy"
        drawdown_fig.data[0].fillcolor = "rgba(208, 59, 59, 0.15)"
        drawdown_fig.data[0].line.color = STATUS_CRITICAL

    if not result.snapshots_df.empty and "inventory" in result.snapshots_df.columns:
        inv_df = result.snapshots_df.sort_values("timestamp").rename(
            columns={"timestamp": "Timestamp"}
        )
        inv_fig = _line_chart(inv_df, ["inventory"], "Inventory Over Time")
        inv_hist = go.Figure()
        inv_hist.add_trace(go.Histogram(
            x=result.snapshots_df["inventory"].dropna(),
            nbinsx=30,
            marker_color=SERIES[2],
            opacity=0.75,
        ))
        inv_hist = _style(inv_hist, "Inventory Distribution")
    else:
        inv_fig = _empty_fig("No inventory data available")
        inv_hist = _empty_fig("No inventory data available")

    try:
        gate = gate_rows(df, settings)
    except Exception as exc:  # the gate must never hide the rest of the tab
        gate = []
        gate_error = str(exc)
    else:
        gate_error = None

    return html.Div([
        _card(
            _h3("Switch-to-real gate", "every metric against its threshold (docs/algorithms/ALGORITHM_1_V2.md §7)"),
            _gate_table(gate) if gate else html.Div(f"gate unavailable: {gate_error}", style={"color": MUTED}),
        ),
        html.Div([
            _card(dcc.Graph(figure=_position_chart(df, settings), config={"displaylogo": False})),
            _card(dcc.Graph(figure=_fills_by_day_chart(fills_by_day(df)), config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1rem"}),
        _card(
            _h3("Performance Summary"),
            html.Div([
                _metric_card("Total Return", f"{metrics['total_return_pct']:+.2f}%",
                             metric_color(metrics['total_return_pct'] / 100)),
                _metric_card("Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}" if metrics['sharpe_ratio'] else "N/A",
                             metric_color(metrics['sharpe_ratio'] or 0)),
                _metric_card("Sortino Ratio", f"{metrics['sortino_ratio']:.2f}" if metrics['sortino_ratio'] else "N/A",
                             metric_color(metrics['sortino_ratio'] or 0)),
                _metric_card("Max Drawdown", f"{metrics['max_drawdown_pct']:.2f}%",
                             metric_color(metrics['max_drawdown_pct'] / 100, higher_is_better=False)),
                _metric_card("Calmar Ratio", f"{metrics['calmar_ratio']:.2f}" if metrics['calmar_ratio'] else "N/A",
                             metric_color(metrics['calmar_ratio'] or 0)),
            ], style={"display": "flex", "gap": "0.75rem", "flexWrap": "wrap"}),
        ),
        html.Div([
            _card(dcc.Graph(figure=equity_fig, config={"displaylogo": False})),
            _card(dcc.Graph(figure=drawdown_fig, config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1rem"}),
        _card(
            _h3("Trading Statistics"),
            html.Div([
                _metric_card("Win Rate", f"{metrics['win_rate_pct']:.2f}%",
                             metric_color(metrics['win_rate_pct'] / 100)),
                _metric_card("Profit Factor", f"{metrics['profit_factor']:.2f}" if metrics['profit_factor'] else "∞",
                             metric_color(metrics['profit_factor'] or 0)),
                _metric_card("Expected Value", f"{metrics['expected_value']:.4f}",
                             metric_color(metrics['expected_value'])),
                _metric_card("Avg Trade PnL", f"{metrics['avg_trade_pnl']:.4f}",
                             metric_color(metrics['avg_trade_pnl'])),
            ], style={"display": "flex", "gap": "0.75rem", "flexWrap": "wrap"}),
        ),
        _card(
            _h3("Inventory Analysis"),
            html.Div([
                _card(dcc.Graph(figure=inv_fig, config={"displaylogo": False})),
                _card(dcc.Graph(figure=inv_hist, config={"displaylogo": False})),
            ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1rem"}),
            html.Div([
                _metric_card("Avg Inventory", f"{metrics['avg_inventory']:.6f}", MUTED),
                _metric_card("Max Inventory", f"{metrics['max_inventory']:.6f}", MUTED),
                _metric_card("Inventory Volatility", f"{metrics['inventory_volatility']:.6f}", MUTED),
                _metric_card("Avg Holding Time", f"{metrics['average_holding_time_seconds']:.0f}s"
                             if metrics['average_holding_time_seconds'] else "N/A", MUTED),
            ], style={"display": "flex", "gap": "0.75rem", "flexWrap": "wrap", "marginTop": "1rem"}),
        ),
    ])


if __name__ == "__main__":
    app.run(
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASHBOARD_PORT", "8050")),
        debug=False, use_reloader=False,
    )
