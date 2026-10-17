"""The dashboard's own metrics on the BTC demo's MM_algo2 rounds (session of 15 Oct, switched to
MM_algo2 on 17 Oct 11:15 UTC). Read-only; prints one JSON document.
Run on the server inside the deployed image, read-only (the output is dashboard_metrics_mm_algo2_20261017.json):
  docker run --rm --user 1000:1000 -e LOB_DISABLE_DOTENV=1 -e PYTHONPATH=/app/market_maker     -v /opt/limit-order-book/runtime/state:/state:ro -v /opt/limit-order-book/runtime/results:/results:ro     -v <folder of this script>:/code:ro limit-order-book-demo:local python /code/dashboard_metrics_mm_algo2.py"""
import json
import math

import dashboard as d

SESSION = "demo-20261015T131629Z-2f38f0a407fa"
applied = json.load(open("/state/runtime_config_btc.json")).get("applied", {})
settings = {k: applied.get(k) for k in ("commission_perc", "exp_ret_min", "inv_min", "inv_max", "order_size", "algorithm")}

df = d.load_track("/results/btc_track_demo.csv", SESSION)
part = d.filter_algorithm(df, "algo3")
fee = float(settings["commission_perc"])


def clean(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        return clean(value.item())
    return value


modes = d.quote_mode_counts(part)
fills = d.fill_events(part)
att = d.pnl_attribution(part)
out = {
    "session": SESSION, "settings": settings,
    "rows_loaded": int(len(df)), "algo3_rows": int(len(part)),
    "first": str(part["Timestamp"].min()), "last": str(part["Timestamp"].max()),
    "hours": d.span_hours(part),
    "modes": modes,
    "fills": int(len(fills)),
    "fills_by_side": {str(k): int(v) for k, v in fills["side"].value_counts().items()} if "side" in fills.columns else None,
    "fills_per_hour": d.fills_per_hour(part),
    "spread": d.realised_vs_computed_spread(part, fee),
    "adverse": d.adverse_selection(part, (60, 300)),
    "attribution": d.attribution_totals(att),
    "gate": [{k: r[k] for k in ("metric", "display", "threshold", "passed")} for r in d.gate_rows(part, settings)],
    "comparison": d.algorithm_comparison(df, settings),
    "inventory_first_last": [float(part["inv_current"].iloc[0]), float(part["inv_current"].iloc[-1])] if len(part) else None,
    "wealth_first_last": [float(part["Portfolio Wealth"].iloc[0]), float(part["Portfolio Wealth"].iloc[-1])] if len(part) else None,
}
print(json.dumps(clean(out), indent=1, default=str))
