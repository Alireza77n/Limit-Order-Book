"""Comprehensive history dashboard -- replaces live_plot.py.

Reads the CSV files run.py writes (real and demo, BTC and USDT) and
serves a live-updating Dash app: portfolio wealth, inventory & cash,
selected bid/ask depth levels with derived spreads, and a browsable
order-activity table, with a session picker for reviewing/comparing past
runs. Read-only against the CSVs -- never touches Nobitex or MySQL, so
it's safe to leave running against real or demo output alike.

Pure data-handling functions (load_track, with_derived_spreads,
session_ids, filter_session) are kept separate from the Dash app wiring
below so they can be unit-tested without starting a server -- see
tests/test_dashboard.py.
"""

from pathlib import Path
from typing import List, Optional

import pandas as pd
import plotly.express as px
from dash import Dash, dash_table, dcc, html
from dash.dependencies import Input, Output

from run import PROFILES, _demo_csv_path

# Every track this project can produce, explicitly labeled real vs demo so
# they're never confused with each other in the UI.
TRACKS = {
    "btc_real": {"label": "BTC — Real", "path": PROFILES["btc"]["output_csv"]},
    "btc_demo": {"label": "BTC — Demo (paper trading)",
                 "path": _demo_csv_path(PROFILES["btc"]["output_csv"])},
    "usdt_real": {"label": "USDT — Real", "path": PROFILES["usdt"]["output_csv"]},
    "usdt_demo": {"label": "USDT — Demo (paper trading)",
                  "path": _demo_csv_path(PROFILES["usdt"]["output_csv"])},
}

ACTIVITY_COLUMNS = [
    "Timestamp", "Session_id", "Bid Level", "Ask Level",
    "Bid Price Sent", "Ask Price Sent",
    "Buy Size", "Buy Sent", "Buy Status",
    "Sell Size", "Sell Sent", "Sell Status",
]


# ---------- Pure data handling (unit-tested, no Dash involved) ----------

def load_track(path: str) -> Optional[pd.DataFrame]:
    """Read a track's CSV. Returns None if it doesn't exist yet, is
    unreadable, or has no rows -- callers show a "no data yet" state."""
    p = Path(path)
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
    df["Bid Spread"] = df["Mid Price"] - df["Bid Price Sent"]
    df["Ask Spread"] = df["Ask Price Sent"] - df["Mid Price"]
    return df


def session_ids(df: pd.DataFrame) -> List:
    """Distinct Session_id values, most recent (largest) first."""
    return sorted(df["Session_id"].dropna().unique().tolist(), reverse=True)


def filter_session(df: pd.DataFrame, session_id) -> pd.DataFrame:
    """session_id of None/"all" returns every session (comparison view)."""
    if session_id in (None, "all"):
        return df
    return df[df["Session_id"] == session_id]


# ---------- Dash app ----------

app = Dash(__name__)
app.title = "Market-Making Dashboard"

app.layout = html.Div([
    html.H2("Market-Making History Dashboard"),
    html.Div([
        html.Label("Track"),
        dcc.Dropdown(
            id="track",
            options=[{"label": v["label"], "value": k} for k, v in TRACKS.items()],
            value="btc_real",
            clearable=False,
        ),
        html.Label("Session"),
        dcc.Dropdown(id="session", clearable=False),
    ], style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "1rem", "maxWidth": "600px"}),

    html.Div(id="status"),

    dcc.Graph(id="wealth-chart"),
    dcc.Graph(id="inventory-chart"),
    dcc.Graph(id="levels-chart"),

    html.H3("Order activity"),
    dash_table.DataTable(
        id="activity-table",
        columns=[{"name": c, "id": c} for c in ACTIVITY_COLUMNS],
        page_size=20,
        sort_action="native",
        filter_action="native",
        style_table={"overflowX": "auto"},
    ),

    dcc.Interval(id="tick", interval=20_000, n_intervals=0),  # auto-refresh every 20s
])


@app.callback(Output("session", "options"), Output("session", "value"),
              Input("track", "value"), Input("tick", "n_intervals"))
def refresh_session_options(track_key, _):
    df = load_track(TRACKS[track_key]["path"])
    if df is None:
        return [], None
    ids = session_ids(df)
    options = [{"label": "All sessions", "value": "all"}] + [
        {"label": str(s), "value": s} for s in ids
    ]
    return options, ids[0] if ids else "all"


@app.callback(
    Output("status", "children"),
    Output("wealth-chart", "figure"),
    Output("inventory-chart", "figure"),
    Output("levels-chart", "figure"),
    Output("activity-table", "data"),
    Input("track", "value"),
    Input("session", "value"),
    Input("tick", "n_intervals"),
)
def refresh_dashboard(track_key, session_id, _):
    track = TRACKS[track_key]
    empty = px.line(title="Waiting for data...")

    df = load_track(track["path"])
    if df is None:
        return (f"No data yet for {track['label']} ({track['path']}).",
                empty, empty, empty, [])

    df = filter_session(df, session_id)
    if df.empty:
        return "No rows for the selected session.", empty, empty, empty, []

    df = with_derived_spreads(df)
    color = "Session_id" if session_id in (None, "all") else None

    wealth_fig = px.line(df, x="Timestamp", y="Portfolio Wealth", color=color, markers=True)
    wealth_fig.update_layout(title="Portfolio wealth", xaxis_title="Time", yaxis_title="Wealth")

    inv_fig = px.line(df, x="Timestamp", y=["inv_current", "cash_current"])
    inv_fig.update_layout(title="Inventory & cash", xaxis_title="Time", yaxis_title="Value")

    levels_fig = px.line(df, x="Timestamp", y=["Bid Level", "Ask Level", "Bid Spread", "Ask Spread"])
    levels_fig.update_layout(title="Selected depth levels & derived spreads",
                              xaxis_title="Time", yaxis_title="Level / Spread")

    activity = df[ACTIVITY_COLUMNS].sort_values("Timestamp", ascending=False).head(200)
    status = f"{track['label']} — {len(df)} rows" + (
        "" if session_id in (None, "all") else f" (session {session_id})"
    )

    return status, wealth_fig, inv_fig, levels_fig, activity.to_dict("records")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False, use_reloader=False)
