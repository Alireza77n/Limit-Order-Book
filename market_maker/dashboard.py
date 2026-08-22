"""Comprehensive history dashboard -- replaces live_plot.py.

Reads the CSV files run.py writes (real and demo, BTC and USDT) and
serves a live-updating Dash app: portfolio wealth, inventory, cash,
selected bid/ask depth levels, derived spreads, and a browsable
order-activity table, with a session picker for reviewing/comparing past
runs. Read-only against the CSVs -- never touches Nobitex or MySQL, so
it's safe to leave running against real or demo output alike.

Pure data-handling functions (load_track, with_derived_spreads,
session_ids, filter_session) are kept separate from the Dash app wiring
below so they can be unit-tested without starting a server -- see
tests/test_dashboard.py.

Chart design follows a fixed categorical palette (never Plotly's default
color cycle) and puts each differently-scaled measure on its own chart --
inventory (base currency, e.g. BTC) and cash (quote currency, e.g. IRT)
differ by many orders of magnitude, as do depth levels (small integers)
and price spreads, so each gets its own single-axis chart rather than
being crammed onto one dual-scale plot.
"""

import fcntl
from pathlib import Path
from typing import List, Optional

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dash_table, dcc, html
from dash.dependencies import Input, Output, State

from config import DEMO_STATE_DIR, TRADING_PROFILES, demo_csv_path
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
TRACKS = {
    "btc_real": {"label": "BTC — Real", "path": TRADING_PROFILES["btc"]["output_csv"], "status": "real"},
    "btc_demo": {"label": "BTC — Demo (paper trading)",
                 "path": demo_csv_path(TRADING_PROFILES["btc"]["output_csv"]), "status": "demo"},
    "usdt_real": {"label": "USDT — Real", "path": TRADING_PROFILES["usdt"]["output_csv"], "status": "real"},
    "usdt_demo": {"label": "USDT — Demo (paper trading)",
                  "path": demo_csv_path(TRADING_PROFILES["usdt"]["output_csv"]), "status": "demo"},
}

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
]

# ---------- Design tokens (dataviz skill: validated reference palette) ----------
# Categorical slots 1/2/3 (blue/orange/aqua) -- these three clear the
# all-pairs CVD/normal-vision floors together, so charts with up to three
# series never need to fold into "Other".
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
STATUS_CRITICAL = "#d03b3b"  # real money -- draws the eye
STATUS_GOOD = "#0ca30c"      # demo/paper -- safe
FONT_FAMILY = "system-ui, -apple-system, 'Segoe UI', sans-serif"


# ---------- Pure data handling (unit-tested, no Dash involved) ----------

def load_track(path: str) -> Optional[pd.DataFrame]:
    """Read a track's CSV. Returns None if it doesn't exist yet, is
    unreadable, or has no rows -- callers show a "no data yet" state."""
    p = Path(path)
    store_path = p.with_suffix(".sqlite3")
    df = None
    if store_path.exists():
        try:
            df = DemoResultStore.open_existing(store_path).load_view()
        except Exception:
            df = None
    if df is None or df.empty:
        if not p.exists():
            return None
        try:
            df = pd.read_csv(path)
        except Exception:
            return None
    if df.empty:
        return None
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    return df


def with_derived_spreads(df: pd.DataFrame) -> pd.DataFrame:
    """Add Mid Price / Bid Spread / Ask Spread columns.

    Derived purely from already-logged Best Bid / Best Ask / Bid Price
    Sent / Ask Price Sent -- the same formulas as README.md's Algorithm 1
    "Step 5: Reported spreads" (s_b = mid - bid_price, s_a = ask_price -
    mid). No new data collected; engine.py's CSV schema is untouched.
    """
    df = df.copy()
    df["Mid Price"] = (df["Best Bid"] + df["Best Ask"]) / 2.0
    bid_column = "Bid Price" if "Bid Price" in df.columns else "Bid Price Sent"
    ask_column = "Ask Price" if "Ask Price" in df.columns else "Ask Price Sent"
    df["Bid Spread"] = df["Mid Price"] - df[bid_column]
    df["Ask Spread"] = df[ask_column] - df["Mid Price"]
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
    Price, so the line charts render a gap. Without this the gaps look like
    lost data -- in the observed USDT session 1827 of 2085 rounds were
    legitimate `profitability_threshold` refusals. Returns "" when the
    columns aren't present (older Real-mode CSVs) or nothing was skipped.
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


def _style(fig: go.Figure, title: str) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=TEXT_PRIMARY)),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT_FAMILY, color=TEXT_SECONDARY, size=12),
        hovermode="x unified",
        margin=dict(l=48, r=24, t=48, b=36),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
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


def _status_badge(status: str) -> html.Span:
    if status == "real":
        color, icon, text = STATUS_CRITICAL, "●", "REAL — places real orders"
    else:
        color, icon, text = STATUS_GOOD, "●", "DEMO — simulated, no real orders"
    return html.Span([icon + " ", text], style={
        "color": color, "fontWeight": 600, "fontSize": "0.85rem",
        "border": f"1px solid {color}", "borderRadius": "999px",
        "padding": "0.25rem 0.75rem", "display": "inline-block",
    })


def _card(*children) -> html.Div:
    return html.Div(children, style={
        "background": SURFACE, "border": f"1px solid {GRIDLINE}",
        "borderRadius": "12px", "padding": "1.25rem",
        "boxShadow": "0 1px 3px rgba(11,11,11,0.06)", "marginBottom": "1.25rem",
    })


def _tab_style() -> dict:
    return {
        "padding": "0.75rem 1.5rem",
        "fontFamily": FONT_FAMILY,
        "fontSize": "0.9rem",
        "fontWeight": 500,
        "color": TEXT_SECONDARY,
        "backgroundColor": PAGE,
        "border": f"1px solid {GRIDLINE}",
        "borderBottom": "none",
        "borderRadius": "8px 8px 0 0",
        "marginRight": "0.25rem",
    }


def _tab_selected_style() -> dict:
    return {
        "padding": "0.75rem 1.5rem",
        "fontFamily": FONT_FAMILY,
        "fontSize": "0.9rem",
        "fontWeight": 600,
        "color": TEXT_PRIMARY,
        "backgroundColor": SURFACE,
        "border": f"1px solid {GRIDLINE}",
        "borderBottom": f"2px solid {SERIES[0]}",
        "borderRadius": "8px 8px 0 0",
        "marginRight": "0.25rem",
    }


def _metric_card(label: str, value: str, color: str = TEXT_PRIMARY) -> html.Div:
    """Create a metric card for the validation dashboard."""
    return html.Div([
        html.Div(label, style={
            "fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY,
            "fontSize": "0.75rem", "fontWeight": 500,
            "textTransform": "uppercase", "letterSpacing": "0.05em",
            "marginBottom": "0.25rem",
        }),
        html.Div(value, style={
            "fontFamily": FONT_FAMILY, "color": color,
            "fontSize": "1.5rem", "fontWeight": 600,
        }),
    ], style={
        "background": SURFACE, "border": f"1px solid {GRIDLINE}",
        "borderRadius": "10px", "padding": "1rem 1.25rem",
        "boxShadow": "0 1px 2px rgba(11,11,11,0.04)",
        "minWidth": "140px", "textAlign": "center",
    })


# ---------- Dash app ----------

app = Dash(__name__)
app.title = "Market-Making Dashboard"

app.layout = html.Div([
    html.Div([
        html.H1("Market-Making History Dashboard", style={
            "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY, "fontSize": "1.5rem", "margin": 0,
        }),
        html.P(
            "Real Nobitex market data, Algorithm 1 quoting decisions, and every "
            "round's outcome -- real and demo runs always clearly labeled.",
            style={"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY, "marginTop": "0.4rem"},
        ),
    ], style={"marginBottom": "1.5rem"}),

    _card(
        html.Div([
            html.Div([
                html.Label("Track", style={"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY,
                                            "fontSize": "0.85rem", "fontWeight": 600}),
                dcc.Dropdown(
                    id="track",
                    options=[{"label": v["label"], "value": k} for k, v in TRACKS.items()],
                    value="btc_real", clearable=False,
                ),
            ]),
            html.Div([
                html.Label("Session", style={"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY,
                                              "fontSize": "0.85rem", "fontWeight": 600}),
                dcc.Dropdown(id="session", clearable=False),
                html.Div(
                    "To permanently delete a session and its history, use the "
                    "Admin panel's Sessions page (this dashboard is read-only).",
                    style={"color": MUTED, "fontFamily": FONT_FAMILY, "fontSize": "0.75rem",
                           "marginTop": "0.35rem"},
                ),
            ]),
            html.Div([
                html.Label("Risk-free rate % (annual, Validation tab)",
                           style={"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY,
                                  "fontSize": "0.85rem", "fontWeight": 600}),
                dcc.Input(id="risk-free-rate-pct", type="number", value=0.0, step=0.1,
                          style={"width": "100%"}),
            ]),
            html.Div([
                html.Label("Target return % (annual, Sortino MAR)",
                           style={"color": TEXT_SECONDARY, "fontFamily": FONT_FAMILY,
                                  "fontSize": "0.85rem", "fontWeight": 600}),
                dcc.Input(id="target-return-pct", type="number", value=0.0, step=0.1,
                          style={"width": "100%"}),
            ]),
            html.Div(id="status-badge", style={"display": "flex", "alignItems": "flex-end"}),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr auto auto auto", "gap": "1.5rem",
                   "alignItems": "start"}),
        html.Div(id="status-text", style={
            "color": MUTED, "fontFamily": FONT_FAMILY, "fontSize": "0.85rem", "marginTop": "0.75rem",
        }),
    ),

    html.Div(
        _card(
            html.H3("Effective Demo configuration", style={
                "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY,
                "fontSize": "1.1rem", "marginTop": 0,
            }),
            html.Div(id="effective-config-summary", style={
                "color": MUTED, "fontFamily": FONT_FAMILY,
                "fontSize": "0.85rem", "marginBottom": "0.75rem",
            }),
            dash_table.DataTable(
                id="effective-config-table",
                columns=[{"name": field, "id": field} for field in (
                    "Field", "Desired", "Applied", "Mode", "Status",
                )],
                style_table={"overflowX": "auto"},
                style_header={
                    "backgroundColor": PAGE, "fontWeight": 600,
                    "color": TEXT_PRIMARY, "fontFamily": FONT_FAMILY,
                    "border": f"1px solid {GRIDLINE}",
                },
                style_cell={
                    "fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY,
                    "fontSize": "0.85rem", "border": f"1px solid {GRIDLINE}",
                    "padding": "0.5rem", "textAlign": "left",
                },
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": PAGE},
                ],
            ),
        ),
        id="effective-config-panel",
    ),

    dcc.Tabs(
        id="main-tabs",
        value="overview",
        children=[
            dcc.Tab(label="Overview", value="overview", style=_tab_style(), selected_style=_tab_selected_style()),
            dcc.Tab(label="Order Book", value="orderbook", style=_tab_style(), selected_style=_tab_selected_style()),
            dcc.Tab(label="Trading", value="trading", style=_tab_style(), selected_style=_tab_selected_style()),
            dcc.Tab(label="Validation", value="validation", style=_tab_style(), selected_style=_tab_selected_style()),
        ],
        style={"marginBottom": "1.25rem"},
    ),

    html.Div(id="tab-content"),

    dcc.Interval(id="tick", interval=20_000, n_intervals=0),  # auto-refresh every 20s
], style={"background": PAGE, "padding": "1.5rem 2rem", "minHeight": "100vh"})


@app.callback(Output("session", "options"), Output("session", "value"),
              Input("track", "value"), Input("tick", "n_intervals"),
              State("session", "value"))
def refresh_session_options(track_key, _, current_value):
    """Default the session picker to "All sessions (compare)" on first
    load, not the newest single session. session.py's
    initialize_stack_for_workers() deliberately mints a new session_id on
    every full-stack restart (once no worker lease is held) -- that's
    correct/intentional, but it means the newest session is near-empty
    right after a restart. Defaulting to it made the dashboard look wiped
    on every restart even though every prior session's rows are still in
    the store; the admin panel's audit log and orders view were never
    session-filtered, so they never showed that false reset. Defaulting to
    "all" on first load keeps both pages showing the same cumulative
    picture -- an operator can still narrow to one session from the
    dropdown when they want to.

    This callback also fires on every 20s auto-refresh tick (Input "tick")
    purely to pick up newly-created/pruned session ids for the options
    list -- it must NOT also reset the operator's current selection back
    to "all" on every tick, which is what it used to do by unconditionally
    returning "all" as the value regardless of what was already selected.
    `State("session", "value")` reads the current selection without itself
    retriggering this callback (State never fires a callback, only Input
    does), so we can compare against it: keep it if it's still a valid
    choice, only fall back to "all" if it no longer is (session pruned, or
    the track was switched to one with a different session universe)."""
    df = load_track(TRACKS[track_key]["path"])
    if df is None:
        return [], None
    ids = session_ids(df)
    options = [{"label": "All sessions (compare)", "value": "all"}] + [
        {"label": str(s), "value": s} for s in ids
    ]
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
    Output("wealth-chart", "figure"),
    Output("inventory-chart", "figure"),
    Output("cash-chart", "figure"),
    Output("levels-chart", "figure"),
    Output("spreads-chart", "figure"),
    Output("activity-table", "data"),
    Input("track", "value"),
    Input("session", "value"),
    Input("tick", "n_intervals"),
)
def refresh_dashboard(track_key, session_id, _):
    track = TRACKS[track_key]
    waiting = _empty_fig("Waiting for data...")

    df = load_track(track["path"])
    if df is None:
        msg = f"No data yet for {track['label']} ({track['path']})."
        return msg, waiting, waiting, waiting, waiting, waiting, []

    df = filter_session(df, session_id)
    if df.empty:
        msg = "No rows for the selected session."
        return msg, waiting, waiting, waiting, waiting, waiting, []

    df = with_derived_spreads(df).sort_values("Timestamp")
    comparing = session_id in (None, "all")

    wealth_fig = _line_chart(df, ["Portfolio Wealth"], "Portfolio wealth",
                              color_by="Session_id" if comparing else None)
    inv_fig = _line_chart(df, ["inv_current"], "Inventory (base currency)")
    cash_fig = _line_chart(df, ["cash_current"], "Cash (quote currency)")
    levels_fig = _line_chart(df, ["Bid Level", "Ask Level"], "Selected depth levels")
    spread_series = ["Bid Spread", "Ask Spread"]
    if "Relative Quoted Spread" in df.columns:
        spread_series.append("Relative Quoted Spread")
    spreads_fig = _line_chart(df, spread_series, "Quoted spreads")

    activity = activity_records(df)
    status = f"{len(df)} rows" + ("" if comparing else f" — session {session_id}")
    status += no_quote_summary(df)

    return status, wealth_fig, inv_fig, cash_fig, levels_fig, spreads_fig, activity


@app.callback(
    Output("tab-content", "children"),
    Input("main-tabs", "value"),
    Input("track", "value"),
    Input("session", "value"),
    Input("tick", "n_intervals"),
    Input("risk-free-rate-pct", "value"),
    Input("target-return-pct", "value"),
)
def render_tab_content(active_tab, track_key, session_id, _, risk_free_rate_pct, target_return_pct):
    """Render content for the active tab."""
    track = TRACKS[track_key]
    waiting = _empty_fig("Waiting for data...")
    
    df = load_track(track["path"])
    if df is None:
        return _no_data_message(track["label"], track["path"])
    
    df = filter_session(df, session_id)
    if df.empty:
        return _no_data_message("selected session", "")
    
    df = with_derived_spreads(df).sort_values("Timestamp")
    comparing = session_id in (None, "all")
    
    if active_tab == "overview":
        return _render_overview_tab(df, comparing)
    elif active_tab == "orderbook":
        return _render_orderbook_tab(df, comparing)
    elif active_tab == "trading":
        return _render_trading_tab(df, comparing)
    elif active_tab == "validation":
        return _render_validation_tab(
            df, track_key, session_id,
            risk_free_rate=(risk_free_rate_pct or 0.0) / 100.0,
            target_return=(target_return_pct or 0.0) / 100.0,
        )
    
    return html.Div("Unknown tab")


def _no_data_message(label: str, path: str) -> html.Div:
    """Return a standardized no-data message."""
    msg = f"No data yet for {label}" + (f" ({path})" if path else ".")
    return html.Div([
        _card(html.P(msg, style={"color": MUTED, "fontFamily": FONT_FAMILY, "textAlign": "center", "padding": "2rem"})),
    ])


def _render_overview_tab(df: pd.DataFrame, comparing: bool) -> html.Div:
    """Render the Overview tab (wealth, inventory, cash)."""
    wealth_fig = _line_chart(df, ["Portfolio Wealth"], "Portfolio wealth",
                              color_by="Session_id" if comparing else None)
    inv_fig = _line_chart(df, ["inv_current"], "Inventory (base currency)")
    cash_fig = _line_chart(df, ["cash_current"], "Cash (quote currency)")
    
    return html.Div([
        _card(dcc.Graph(id="wealth-chart", figure=wealth_fig, config={"displaylogo": False})),
        html.Div([
            _card(dcc.Graph(id="inventory-chart", figure=inv_fig, config={"displaylogo": False})),
            _card(dcc.Graph(id="cash-chart", figure=cash_fig, config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1.25rem"}),
    ])


def _render_orderbook_tab(df: pd.DataFrame, comparing: bool) -> html.Div:
    """Render the Order Book tab (levels, spreads)."""
    levels_fig = _line_chart(df, ["Bid Level", "Ask Level"], "Selected depth levels")
    spread_series = ["Bid Spread", "Ask Spread"]
    if "Relative Quoted Spread" in df.columns:
        spread_series.append("Relative Quoted Spread")
    spreads_fig = _line_chart(df, spread_series, "Quoted spreads")
    
    return html.Div([
        html.Div([
            _card(dcc.Graph(id="levels-chart", figure=levels_fig, config={"displaylogo": False})),
            _card(dcc.Graph(id="spreads-chart", figure=spreads_fig, config={"displaylogo": False})),
        ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1.25rem"}),
    ])


def _render_trading_tab(df: pd.DataFrame, comparing: bool) -> html.Div:
    """Render the Trading tab (activity table)."""
    activity = activity_records(df)
    
    return html.Div([
        _card(
            html.H3("Order activity", style={
                "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY, "fontSize": "1.1rem", "marginTop": 0,
            }),
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
                style_cell={"fontFamily": FONT_FAMILY, "color": TEXT_SECONDARY, "fontSize": "0.85rem",
                            "border": f"1px solid {GRIDLINE}", "padding": "0.5rem"},
                style_data_conditional=[
                    {"if": {"row_index": "odd"}, "backgroundColor": PAGE},
                ],
            ),
        ),
    ])


def _render_validation_tab(
    df: pd.DataFrame, track_key: str, session_id,
    risk_free_rate: float = 0.0, target_return: float = 0.0,
) -> html.Div:
    """Render the Validation tab with all metrics and charts."""
    # Run validation
    engine = create_validation_engine(
        risk_free_rate=risk_free_rate, target_return=target_return
    )
    result = engine.validate_from_csv(df)
    report = generate_validation_report(result)
    
    metrics = report["summary"]
    charts = report["charts"]
    
    # Determine colors for metric cards
    def metric_color(value, higher_is_better=True):
        if value is None:
            return MUTED
        if higher_is_better:
            return STATUS_GOOD if value > 0 else STATUS_CRITICAL
        return STATUS_CRITICAL if value > 0 else STATUS_GOOD
    
    # Build equity and drawdown charts from validation result
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
    # Make drawdown area chart
    drawdown_fig.data[0].fill = "tozeroy"
    drawdown_fig.data[0].fillcolor = "rgba(208, 59, 59, 0.15)"
    drawdown_fig.data[0].line.color = STATUS_CRITICAL
    
    # Inventory chart
    if not result.snapshots_df.empty and "inventory" in result.snapshots_df.columns:
        inv_df = result.snapshots_df.sort_values("timestamp")
        inv_fig = _line_chart(inv_df, ["inventory"], "Inventory Over Time")
    else:
        inv_fig = _empty_fig("No inventory data available")
    
    # Inventory distribution histogram
    if not result.snapshots_df.empty and "inventory" in result.snapshots_df.columns:
        inv_hist = go.Figure()
        inv_hist.add_trace(go.Histogram(
            x=result.snapshots_df["inventory"].dropna(),
            nbinsx=30,
            marker_color=SERIES[2],
            opacity=0.75,
        ))
        inv_hist = _style(inv_hist, "Inventory Distribution")
    else:
        inv_hist = _empty_fig("No inventory data available")
    
    return html.Div([
        # Section 1: Performance Summary
        _card(
            html.H3("Performance Summary", style={
                "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY, "fontSize": "1.1rem", "marginTop": 0, "marginBottom": "1rem",
            }),
            html.Div([
                _metric_card("Total Return", f"{metrics['total_return_pct']:+.2f}%", 
                           metric_color(metrics['total_return_pct'] / 100)),
                _metric_card("Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}" if metrics['sharpe_ratio'] else "N/A",
                           metric_color(metrics['sharpe_ratio'] or 0)),
                _metric_card("Sortino Ratio", f"{metrics['sortino_ratio']:.2f}" if metrics['sortino_ratio'] else "N/A",
                           metric_color(metrics['sortino_ratio'] or 0)),
            ], style={"display": "flex", "gap": "1rem", "flexWrap": "wrap"}),
        ),
        
        # Section 2: Risk Analysis
        _card(
            html.H3("Risk Analysis", style={
                "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY, "fontSize": "1.1rem", "marginTop": 0, "marginBottom": "1rem",
            }),
            html.Div([
                _card(dcc.Graph(figure=equity_fig, config={"displaylogo": False})),
                _card(dcc.Graph(figure=drawdown_fig, config={"displaylogo": False})),
            ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1.25rem"}),
            html.Div([
                _metric_card("Max Drawdown", f"{metrics['max_drawdown_pct']:.2f}%",
                           metric_color(metrics['max_drawdown_pct'] / 100, higher_is_better=False)),
                _metric_card("Calmar Ratio", f"{metrics['calmar_ratio']:.2f}" if metrics['calmar_ratio'] else "N/A",
                           metric_color(metrics['calmar_ratio'] or 0)),
            ], style={"display": "flex", "gap": "1rem", "flexWrap": "wrap", "marginTop": "1rem"}),
        ),
        
        # Section 3: Trading Statistics
        _card(
            html.H3("Trading Statistics", style={
                "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY, "fontSize": "1.1rem", "marginTop": 0, "marginBottom": "1rem",
            }),
            html.Div([
                _metric_card("Win Rate", f"{metrics['win_rate_pct']:.2f}%",
                           metric_color(metrics['win_rate_pct'] / 100)),
                _metric_card("Profit Factor", f"{metrics['profit_factor']:.2f}" if metrics['profit_factor'] else "∞",
                           metric_color(metrics['profit_factor'] or 0)),
                _metric_card("Expected Value", f"{metrics['expected_value']:.4f}",
                           metric_color(metrics['expected_value'])),
                _metric_card("Avg Trade PnL", f"{metrics['avg_trade_pnl']:.4f}",
                           metric_color(metrics['avg_trade_pnl'])),
            ], style={"display": "flex", "gap": "1rem", "flexWrap": "wrap"}),
        ),
        
        # Section 4: Inventory Analysis
        _card(
            html.H3("Inventory Analysis", style={
                "fontFamily": FONT_FAMILY, "color": TEXT_PRIMARY, "fontSize": "1.1rem", "marginTop": 0, "marginBottom": "1rem",
            }),
            html.Div([
                _card(dcc.Graph(figure=inv_fig, config={"displaylogo": False})),
                _card(dcc.Graph(figure=inv_hist, config={"displaylogo": False})),
            ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1.25rem"}),
            html.Div([
                _metric_card("Avg Inventory", f"{metrics['avg_inventory']:.6f}", MUTED),
                _metric_card("Max Inventory", f"{metrics['max_inventory']:.6f}", MUTED),
                _metric_card("Inventory Volatility", f"{metrics['inventory_volatility']:.6f}", MUTED),
                _metric_card("Avg Holding Time", f"{metrics['average_holding_time_seconds']:.0f}s" 
                           if metrics['average_holding_time_seconds'] else "N/A", MUTED),
            ], style={"display": "flex", "gap": "1rem", "flexWrap": "wrap", "marginTop": "1rem"}),
        ),
    ])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False, use_reloader=False)
