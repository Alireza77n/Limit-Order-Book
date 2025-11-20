from pathlib import Path
import pandas as pd
from dash import Dash, dcc, html
from dash.dependencies import Input, Output
import plotly.express as px

CSV_PATH = Path("/root/Nouri/Algo1/usdt_track.csv")

app = Dash(__name__)
app.title = "USDT Portfolio (Live)"
app.layout = html.Div([
    html.H3("USDT Portfolio Wealth (live)"),
    dcc.Graph(id="line"),
    dcc.Interval(id="tick", interval=20_000, n_intervals=0)  # 20s
])

@app.callback(Output("line", "figure"), Input("tick", "n_intervals"))
def refresh(_):
    if not CSV_PATH.exists():
        return px.line(title="Waiting for CSV...")
    df = pd.read_csv(CSV_PATH)
    if df.empty or "Timestamp" not in df or "Portfolio Wealth" not in df:
        return px.line(title="Waiting for data...")
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    fig = px.line(df, x="Timestamp", y="Portfolio Wealth", markers=True)
    fig.update_layout(xaxis_title="Time", yaxis_title="Portfolio Wealth")
    return fig

if __name__ == "__main__":
    # new API in recent Dash
    app.run(host="0.0.0.0", port=8050, debug=False, use_reloader=False)
