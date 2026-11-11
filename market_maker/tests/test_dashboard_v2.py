"""Unit tests for the version-2 dashboard: the pure helpers behind the
Overview / Quote decision / Fill model / Validation tabs, and smoke tests of
every tab renderer and callback (plain function calls, no Dash server)."""

import json
import math

import pandas as pd
import pytest

import dashboard
from config import MMConfig, TRADING_PROFILES
from fill_model import write_fill_table
from strategy.algo1 import Algo1Strategy

FEE = 0.001


# ---------- fixtures ----------

def _row(ts, round_no, **overrides):
    base = {
        "Session_id": "s1", "Algorithm": "1usdt", "Crypto": "USDTIRT", "Mode": "demo",
        "Round": round_no, "Timestamp": ts, "Quote Status": "quote_generated", "No Quote Reason": None,
        "Bid Level": 1, "Ask Level": 1, "inv_current": 10.0, "cash_current": 1000.0,
        "Portfolio Wealth": 2000.0, "Best Bid": 99.0, "Best Ask": 101.0, "Last Price": 100.0,
        "Bid Price": 98.0, "Ask Price": 102.0, "Mid Price": 100.0, "Net Return": 0.03,
        "Buy Order ID": None, "Buy Status": "not_sent", "Buy Original Amount": 0.0,
        "Buy Matched Amount": 0.0, "Buy Limit Price": None,
        "Sell Order ID": None, "Sell Status": "not_sent", "Sell Original Amount": 0.0,
        "Sell Matched Amount": 0.0, "Sell Limit Price": None, "Round Fee": 0.0,
        "Quote Mode": "pair", "Selection Reason": "pair_selected", "Centre": 100.0,
        "Bid Distance": 0.02, "Ask Distance": 0.02, "Bid Fill Prob": 0.5, "Ask Fill Prob": 0.5,
        "Expected Value": 0.0075, "Reference Price": 100.0,
    }
    base.update(overrides)
    return base


def _frame():
    """Five rounds: a buy fill, a quiet round, a no-quote round, an ask-only
    round whose sell fills, and a later round that closes the hour."""
    rows = [
        _row("2026-10-01 10:00:00", 1, **{
            "Buy Order ID": "b1", "Buy Status": "filled", "Buy Original Amount": 2.0,
            "Buy Matched Amount": 2.0, "Buy Limit Price": 98.0,
            "Sell Order ID": "s1", "Sell Status": "cancelled", "Sell Original Amount": 2.0,
            "Sell Limit Price": 102.0,
        }),
        _row("2026-10-01 10:01:00", 2, **{
            "Mid Price": 99.0, "Best Bid": 98.0, "Best Ask": 100.0, "inv_current": 12.0,
        }),
        _row("2026-10-01 10:02:00", 3, **{
            "Quote Status": "no_quote", "No Quote Reason": "no_side_clears_bar",
            "Quote Mode": None, "Selection Reason": None, "Bid Price": None, "Ask Price": None,
            "Bid Distance": None, "Ask Distance": None, "Bid Fill Prob": None, "Ask Fill Prob": None,
            "Expected Value": None, "Net Return": None, "inv_current": 12.0,
        }),
        _row("2026-10-01 10:03:00", 4, **{
            "Quote Mode": "ask_only", "Selection Reason": "ask_only_fallback", "Bid Price": None,
            "Bid Distance": None, "Bid Fill Prob": None,
            "Sell Order ID": "s2", "Sell Status": "filled", "Sell Original Amount": 1.0,
            "Sell Matched Amount": 1.0, "Sell Limit Price": 102.0,
            "Portfolio Wealth": 2003.0, "inv_current": 11.0,
        }),
        _row("2026-10-01 11:00:00", 5, **{"Portfolio Wealth": 1990.0, "inv_current": 11.0}),
    ]
    df = pd.DataFrame(rows)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    return df


def _example_book():
    """The running example of docs/MM_algo1v2.md, section 12."""
    bids = [[2_659_990, "20000"], [2_659_900, "300"], [2_658_000, "1500"], [2_655_000, "8000"],
            [2_650_000, "40000"], [2_640_000, "10000"], [2_630_000, "5000"], [2_620_000, "5000"],
            [2_610_000, "5000"], [2_600_000, "5000"], [2_590_000, "5000"]]
    asks = [[2_660_010, "5000"], [2_660_100, "400"], [2_662_000, "2000"], [2_665_000, "6000"],
            [2_670_000, "30000"], [2_680_000, "12000"], [2_690_000, "5000"], [2_700_000, "5000"],
            [2_710_000, "5000"], [2_720_000, "5000"], [2_730_000, "5000"]]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2660000"}


def _diagnostics_document(inventory=25.0):
    strategy = Algo1Strategy(MMConfig(**TRADING_PROFILES["usdt"]))
    return strategy.diagnostics(_example_book(), inventory, round_no=7, session_id="s1")


def _text_of(component) -> str:
    """Every string leaf of a Dash component tree, figures included."""
    if component is None:
        return ""
    if isinstance(component, str):
        return component
    if isinstance(component, (list, tuple)):
        return " ".join(_text_of(c) for c in component)
    if isinstance(component, dict):
        return " ".join(_text_of(v) for v in component.values())
    if hasattr(component, "to_plotly_json"):
        return _text_of(component.to_plotly_json())
    return str(component)


@pytest.fixture
def v2_track(tmp_path, monkeypatch):
    """A usdt_demo track with v2 rows, a measured fill table and three
    diagnostics documents (rounds 5, 6, 7)."""
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path / "state"))
    csv_path = tmp_path / "usdt_demo.csv"
    _frame().to_csv(csv_path, index=False)
    monkeypatch.setitem(dashboard.TRACKS, "usdt_demo", dict(dashboard.TRACKS["usdt_demo"], path=str(csv_path)))

    table_path = tmp_path / "fill_table_usdtirt.json"
    write_fill_table(
        str(table_path), pair="USDTIRT", horizon_seconds=600, method="test",
        buckets=[
            {"distance": d, "p_bid": p, "p_ask": p, "n_bid": 100, "n_ask": 100, "n_bid_own": 3, "n_ask_own": 2}
            for d, p in ((0.001, 0.4), (0.002, 0.2), (0.004, 0.08))
        ],
        extra={"samples": 1000, "own_orders": 5, "window_days": 14, "half_life_days": 3,
               "sources": ["usdt_demo.csv"]},
    )
    monkeypatch.setattr(dashboard, "fill_table_path", lambda track_key: str(table_path))

    diag_path = tmp_path / "usdtirt_algo1_diagnostics.jsonl"
    document = _diagnostics_document()
    with diag_path.open("w", encoding="utf-8") as stream:
        for round_no in (5, 6, 7):
            stream.write(json.dumps(dict(document, round=round_no)) + "\n")
    monkeypatch.setattr(dashboard, "diagnostics_path", lambda track_key: str(diag_path))
    return tmp_path


# ---------- track picker ----------

def test_default_track_is_the_most_recently_written_store(tmp_path, monkeypatch):
    import os
    import time

    for key in list(dashboard.TRACKS):
        monkeypatch.setitem(dashboard.TRACKS, key, dict(dashboard.TRACKS[key], path=str(tmp_path / f"{key}.csv")))
    assert dashboard.default_track() == next(iter(dashboard.TRACKS))     # nothing written yet

    now = time.time()
    (tmp_path / "btc_real.csv").write_text("x")
    os.utime(tmp_path / "btc_real.csv", (now - 3600, now - 3600))
    (tmp_path / "usdt_demo.sqlite3").write_text("x")                     # the sibling store counts too
    os.utime(tmp_path / "usdt_demo.sqlite3", (now - 60, now - 60))
    (tmp_path / "btc_demo.csv").write_text("x")
    os.utime(tmp_path / "btc_demo.csv", (now - 600, now - 600))

    assert dashboard.default_track() == "usdt_demo"


def test_load_track_cache_refreshes_when_the_store_changes(tmp_path):
    """Reads are cached per file signature (mtime, size) so the callbacks of
    one refresh tick share a load; a changed file is re-read, and callers
    get their own copy."""
    path = tmp_path / "track.csv"
    pd.DataFrame([_row("2026-10-01 10:00:00", 1)]).to_csv(path, index=False)
    first = dashboard.load_track(str(path))
    assert list(first["Round"]) == [1]

    pd.DataFrame([_row("2026-10-01 10:00:00", 1), _row("2026-10-01 10:00:10", 2)]).to_csv(path, index=False)
    second = dashboard.load_track(str(path))
    assert list(second["Round"]) == [1, 2]

    second["Round"] = 99                                   # a caller mutating its copy
    assert list(dashboard.load_track(str(path))["Round"]) == [1, 2]
    assert dashboard.load_track(str(tmp_path / "missing.csv")) is None


def test_deep_links_select_tab_track_and_session(monkeypatch):
    from dash import no_update

    assert dashboard.apply_url("?tab=decision&track=usdt_demo") == ("decision", "usdt_demo")
    assert dashboard.apply_url("?tab=nope&track=nope") == (no_update, no_update)
    assert dashboard.apply_url("") == (no_update, no_update)
    assert dashboard.apply_url(None) == (no_update, no_update)

    monkeypatch.setattr(dashboard, "load_track", lambda path: pd.DataFrame([{"Session_id": "s1"}, {"Session_id": "s2"}]))
    assert dashboard.refresh_session_options("btc_real", 0, None, "?session=s2")[1] == "s2"
    assert dashboard.refresh_session_options("btc_real", 0, None, "?session=all")[1] == "all"
    assert dashboard.refresh_session_options("btc_real", 0, None, "?session=missing")[1] == "all"
    assert dashboard.refresh_session_options("btc_real", 1, "s1", "?session=s2")[1] == "s1"   # a made choice wins


# ---------- decisions ----------

def test_quote_mode_counts_from_the_quote_mode_column():
    assert dashboard.quote_mode_counts(_frame()) == {
        "pair": 3, "bid_only": 0, "ask_only": 1, "none": 1, "total": 5,
    }


def test_quote_mode_counts_fall_back_to_prices_for_legacy_rows():
    df = pd.DataFrame([
        {"Bid Price Sent": 98.0, "Ask Price Sent": 102.0},
        {"Bid Price Sent": 98.0, "Ask Price Sent": None},
        {"Bid Price Sent": None, "Ask Price Sent": None},
    ])
    counts = dashboard.quote_mode_counts(df)
    assert (counts["pair"], counts["bid_only"], counts["ask_only"], counts["none"]) == (1, 1, 0, 1)


def test_no_quote_reason_counts():
    assert dashboard.no_quote_reason_counts(_frame()) == {"no_side_clears_bar": 1}
    assert dashboard.no_quote_reason_counts(pd.DataFrame([{"Round": 1}])) == {}


def test_activity_records_include_the_decision_columns():
    record = dashboard.activity_records(_frame())[0]      # newest first
    assert record["Round"] == 5
    assert record["Quote Mode"] == "pair"
    assert record["Bid Fill Prob"] == 0.5
    assert record["Selection Reason"] == "pair_selected"


# ---------- fills, spread, adverse selection ----------

def test_fill_events_list_each_filled_order_with_its_placement_data():
    events = dashboard.fill_events(_frame())
    assert list(events["side"]) == ["buy", "sell"]
    assert list(events["price"]) == [98.0, 102.0]
    assert list(events["amount"]) == [2.0, 1.0]
    assert list(events["fill_prob"]) == [0.5, 0.5]
    assert list(events["Round"]) == [1, 4]


def test_fill_events_tolerate_legacy_live_rows_with_the_amount_in_the_status_cell():
    df = pd.DataFrame([{
        "Timestamp": pd.Timestamp("2026-10-01 10:00:00"), "Round": 1,
        "Buy Status": 0.5, "Sell Status": "clid-2",
        "Bid Price Sent": 98.0, "Ask Price Sent": 102.0,
    }])
    events = dashboard.fill_events(df)
    assert len(events) == 1
    assert events.iloc[0]["side"] == "buy"
    assert events.iloc[0]["amount"] == 0.5
    assert math.isnan(events.iloc[0]["fill_prob"])


def test_fills_per_hour_uses_the_time_span_of_the_rows():
    assert dashboard.fills_per_hour(_frame()) == pytest.approx(2.0)   # two fills in one hour
    assert dashboard.fills_per_hour(_frame().head(1)) is None


def test_round_trips_match_buys_to_later_sells_fifo():
    trips = dashboard.round_trips(_frame(), FEE)
    assert len(trips) == 1
    assert trips[0]["amount"] == 1.0            # 2 bought, 1 sold -> one unit still held
    assert trips[0]["net"] == pytest.approx((1 - FEE) ** 2 * 102.0 / 98.0 - 1)


def test_realised_vs_computed_spread():
    spread = dashboard.realised_vs_computed_spread(_frame(), FEE)
    assert spread["trips"] == 1
    assert spread["realised"] == pytest.approx((1 - FEE) ** 2 * 102.0 / 98.0 - 1)
    assert spread["computed"] == pytest.approx(0.03)     # mean Net Return of the two-sided rounds


def test_adverse_selection_measures_the_mid_move_after_our_fills():
    adverse = dashboard.adverse_selection(_frame(), (60,))
    # buy at 98 at 10:00 -> mid 60 s later is 99; sell at 102 at 10:03 -> next mid (11:00) is 100
    assert adverse[60]["n"] == 2
    assert adverse[60]["mean"] == pytest.approx(((99 - 98) / 98 + (102 - 100) / 102) / 2)


def test_adverse_selection_without_fills_is_not_measurable():
    df = _frame().copy()
    df["Buy Status"] = "not_sent"
    df["Sell Status"] = "not_sent"
    assert dashboard.adverse_selection(df, (60,))[60] == {"mean": None, "n": 0}


# ---------- distances, inventory ----------

def test_quote_distances_prefer_the_logged_distance_and_fall_back_to_prices():
    logged = dashboard.quote_distances(_frame())
    assert sorted(logged["side"].unique()) == ["ask", "bid"]
    assert logged["distance"].round(9).unique().tolist() == [0.02]

    legacy = pd.DataFrame([{"Best Bid": 99.0, "Best Ask": 101.0, "Bid Price Sent": 98.0, "Ask Price Sent": 103.0}])
    distances = dashboard.quote_distances(legacy).set_index("side")["distance"]
    assert distances["bid"] == pytest.approx(0.02)
    assert distances["ask"] == pytest.approx(0.03)


def test_inventory_position_is_clipped_to_the_band():
    df = pd.DataFrame({"inv_current": [-1.0, 0.0, 25.0, 50.0, 80.0]})
    assert dashboard.inventory_position(df, 0.0, 50.0).tolist() == [0.0, 0.0, 0.5, 1.0, 1.0]
    assert dashboard.inventory_position(df, 0.0, 0.0).tolist() == [0.5] * 5


# ---------- fill model files ----------

def test_load_fill_table_reads_the_builder_output_and_rejects_garbage(tmp_path):
    path = tmp_path / "fill_table_usdtirt.json"
    write_fill_table(str(path), pair="USDTIRT", horizon_seconds=600, method="test",
                     buckets=[{"distance": 0.001, "p_bid": 0.4, "p_ask": 0.35, "n_bid": 10, "n_ask": 12}])
    table = dashboard.load_fill_table(str(path))
    assert table["method"] == "test"
    assert table["buckets"][0]["p_bid"] == 0.4
    (tmp_path / "bad.json").write_text("{not json")
    assert dashboard.load_fill_table(str(tmp_path / "bad.json")) is None
    assert dashboard.load_fill_table(str(tmp_path / "missing.json")) is None
    (tmp_path / "list.json").write_text("[1, 2]")
    assert dashboard.load_fill_table(str(tmp_path / "list.json")) is None


def test_fill_table_and_diagnostics_paths_follow_the_pair_symbol():
    assert dashboard.fill_table_path("usdt_demo").endswith("fill_table_usdtirt.json")
    assert dashboard.diagnostics_path("btc_demo").endswith("btcirt_algo1_diagnostics.jsonl")


def test_load_diagnostics_returns_the_newest_documents_in_order(tmp_path):
    path = tmp_path / "usdtirt_algo1_diagnostics.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for i in range(500):
            stream.write(json.dumps({"session_id": "s", "round": i, "pad": "x" * 300}) + "\n")
    documents = dashboard.load_diagnostics(str(path), limit=50)
    assert [d["round"] for d in documents] == list(range(450, 500))
    assert dashboard.load_diagnostics(str(tmp_path / "none.jsonl")) == []


def test_load_diagnostics_skips_a_corrupt_line(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"round": 1}\n{broken\n{"round": 2}\n', encoding="utf-8")
    assert [d["round"] for d in dashboard.load_diagnostics(str(path))] == [1, 2]


def test_pick_document_prefers_the_selected_round_else_the_newest():
    documents = [{"session_id": "s", "round": r} for r in (1, 2, 3)]
    assert dashboard._pick_document(documents, "s:2")["round"] == 2
    assert dashboard._pick_document(documents, "s:99")["round"] == 3
    assert dashboard._pick_document(documents, None)["round"] == 3
    assert dashboard._pick_document([], "s:1") is None


def test_formula_probability_is_monotone_and_bounded():
    values = [dashboard.formula_probability(d, 400.0, 1.0) for d in (0.0, 0.001, 0.005, 0.02)]
    assert values == sorted(values, reverse=True)
    assert all(0.0 <= v <= 1.0 for v in values)
    assert values[0] == pytest.approx(1 - math.exp(-1))


# ---------- calibration and the gate ----------

def test_calibration_rows_compare_predicted_with_realised_per_bucket():
    rows = dashboard.calibration_rows(_frame())
    bid = next(r for r in rows if r["side"] == "bid")
    ask = next(r for r in rows if r["side"] == "ask")
    assert bid == {"side": "bid", "distance": 0.024, "n": 1, "realised": 1.0, "predicted": 0.5}
    assert (ask["n"], ask["realised"], ask["predicted"]) == (2, 0.5, 0.5)   # s1 cancelled, s2 filled


def test_fills_by_day_sums_predicted_and_realised_fills():
    frame = dashboard.fills_by_day(_frame())
    assert len(frame) == 1
    assert frame.iloc[0]["predicted"] == pytest.approx(1.5)   # b1 0.5 + s1 0.5 + s2 0.5
    assert frame.iloc[0]["realised"] == 2                      # b1 and s2 filled


def test_drawdown_fraction():
    assert dashboard.drawdown_fraction(_frame()) == pytest.approx(13.0 / 2003.0)
    assert dashboard.drawdown_fraction(pd.DataFrame([{"Round": 1}])) is None


def test_gate_rows_cover_every_gate_metric(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path))
    settings = dict(dashboard.track_settings("usdt_demo"), commission_perc=FEE, inv_min=0.0, inv_max=50.0)
    rows = dashboard.gate_rows(_frame(), settings)
    by_metric = {r["metric"]: r for r in rows}
    assert len(rows) == 9
    assert by_metric["rounds with a quote"]["value"] == pytest.approx(0.8)
    assert by_metric["rounds with a quote"]["passed"] is True
    assert by_metric["fills per hour"]["value"] == pytest.approx(2.0)
    assert by_metric["completed round trips"]["passed"] is False          # 1 < 30
    assert by_metric["net PnL after fees"]["value"] == pytest.approx(-10.0)
    assert by_metric["net PnL after fees"]["passed"] is False
    assert by_metric["mark-to-market drawdown"]["value"] == pytest.approx(13.0 / 2003.0)
    assert by_metric["time at band edges (x < 0.1 or > 0.9)"]["passed"] is True
    assert by_metric["fill forecast error (predicted / realised − 1)"]["value"] == pytest.approx(1.5 / 2 - 1)


def test_gate_rows_mark_unmeasurable_metrics_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path))
    df = pd.DataFrame([{"Timestamp": pd.Timestamp("2026-10-01"), "Session_id": "s1", "inv_current": 1.0}])
    rows = dashboard.gate_rows(df, dashboard.track_settings("btc_demo"))
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["fills per hour"]["passed"] is None
    assert by_metric["net PnL after fees"]["passed"] is None
    assert by_metric["adverse selection, 60 s after fills"]["passed"] is None


# ---------- settings ----------

def test_track_settings_merge_the_profile_and_compute_the_gross_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path))
    settings = dashboard.track_settings("usdt_demo")
    profile = TRADING_PROFILES["usdt"]
    assert settings["commission_perc"] == profile["commission_perc"]
    assert settings["inv_max"] == profile["inv_max"]
    assert settings["fill_kappa"] == profile["fill_kappa"]
    fee = profile["commission_perc"]
    assert settings["gross_bar"] == pytest.approx((1 + profile["exp_ret_min"]) / (1 - fee) ** 2 - 1)


def test_track_settings_apply_the_demo_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path))
    (tmp_path / "demo_config_usdt.json").write_text(json.dumps({"ev_min": 0.0005, "inv_max": 80}))
    settings = dashboard.track_settings("usdt_demo")
    assert settings["ev_min"] == pytest.approx(0.0005)
    assert settings["inv_max"] == 80


# ---------- rendering (plain function calls) ----------

@pytest.mark.parametrize("tab", ["overview", "decision", "fillmodel", "orderbook", "validation", "trading", "attribution"])
def test_every_tab_renders_for_a_v2_track(v2_track, tab):
    rendered = dashboard.render_tab_content(tab, "usdt_demo", "all", 0, 0.0, 0.0, None)
    assert "No data yet" not in _text_of(rendered)


def test_overview_tab_shows_the_headline_numbers(v2_track):
    text = _text_of(dashboard.render_tab_content("overview", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert "Quote rate (rounds)" in text and "80%" in text
    assert "Fills / hour" in text and "2.00" in text
    assert "Adverse selection (60 s)" in text
    assert "Decisions — share of rounds" in text
    assert "Where the quotes sit" in text


def test_decision_tab_shows_candidates_heatmap_trace_and_history(v2_track):
    text = _text_of(dashboard.render_tab_content("decision", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert "Candidates" in text
    assert "Expected value J = p_bid · p_ask · R_net of every pair" in text
    assert "Decision trace" in text
    assert "pair selected" in text
    assert "Decision history" in text


def test_decision_round_selection_survives_a_refresh_tick(v2_track):
    assert dashboard.remember_decision_round("s1:6") == "s1:6"
    rendered = dashboard.render_tab_content("decision", "usdt_demo", "all", 3, 0.0, 0.0, "s1:6")
    dropdown = next(c for c in _components(rendered) if getattr(c, "id", None) == "decision-round")
    assert dropdown.value == "s1:6"
    assert [o["value"] for o in dropdown.options] == ["s1:7", "s1:6", "s1:5"]


def test_decision_panels_callback_falls_back_to_the_newest_round(v2_track):
    assert "round 5 ·" in _text_of(dashboard.refresh_decision_panels("s1:5", "usdt_demo"))
    assert "round 7 ·" in _text_of(dashboard.refresh_decision_panels("s1:999", "usdt_demo"))


def test_decision_tab_without_diagnostics_says_so(v2_track, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "diagnostics_path", lambda track_key: str(tmp_path / "missing.jsonl"))
    text = _text_of(dashboard.render_tab_content("decision", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert "No data yet" in text
    assert "No data yet" in _text_of(dashboard.refresh_decision_panels(None, "usdt_demo"))


def test_fill_model_tab_reports_table_health_and_the_formula_fallback(v2_track, tmp_path, monkeypatch):
    text = _text_of(dashboard.render_tab_content("fillmodel", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert "method test" in text
    assert "measured · bid" in text
    assert "calibration from 3 own terminal orders" in text
    assert "Measured table" in text

    monkeypatch.setattr(dashboard, "fill_table_path", lambda track_key: str(tmp_path / "none.json"))
    text = _text_of(dashboard.render_tab_content("fillmodel", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert "no fill table" in text and "formula" in text


def test_validation_tab_puts_the_gate_first(v2_track):
    text = _text_of(dashboard.render_tab_content("validation", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert text.index("Switch-to-real gate") < text.index("Performance Summary")
    assert "completed round trips" in text
    assert "not measurable yet" in text or "pass" in text


def test_rounds_table_refreshes_rows_in_place(v2_track):
    """The 20 s tick must not rebuild the Rounds table (that resets the
    operator's scroll): the rows are swapped through their own callback."""
    from dash import no_update

    rows = dashboard.refresh_rounds_table(3, "usdt_demo", "all")
    assert [r["Round"] for r in rows] == [5, 4, 3, 2, 1]
    assert rows[0]["Quote Mode"] == "pair"
    assert dashboard.refresh_rounds_table(3, "usdt_demo", "no-such-session") is no_update
    assert dashboard._triggered_by_tick() is False          # outside a callback


def test_status_text_counts_rounds_and_no_quote_rounds(v2_track):
    text = dashboard.refresh_status_text("usdt_demo", "all", 0)
    assert text.startswith("5 rounds")
    assert "1 of 5 rounds produced no quote" in text
    assert "no_side_clears_bar: 1" in text


def test_missing_track_renders_a_no_data_message(tmp_path, monkeypatch):
    monkeypatch.setitem(dashboard.TRACKS, "btc_demo", dict(dashboard.TRACKS["btc_demo"], path=str(tmp_path / "none.csv")))
    assert "No data yet" in _text_of(dashboard.render_tab_content("overview", "btc_demo", "all", 0, 0.0, 0.0, None))
    assert dashboard.refresh_status_text("btc_demo", "all", 0).startswith("No data yet")


def test_history_strip_and_reasons_chart_cover_each_decision():
    df = dashboard.with_derived_spreads(_frame())
    strip = dashboard._history_strip(df)
    assert len(strip.data[0].x) == 5
    reasons = dashboard._reasons_chart(df)
    labels = list(reasons.data[0].y)
    assert "quoted (pair)" in labels
    assert "no quote: no_side_clears_bar" in labels


def test_ev_heatmap_marks_rejected_pairs_and_the_sent_pair():
    document = _diagnostics_document()
    assert document["quote_mode"] == "pair"
    fig = dashboard._ev_heatmap(document)
    heat = fig.data[0]
    assert heat.type == "heatmap"
    marks = [t for row in heat.text for t in row]
    assert "✗" in marks and "" in marks                          # rejected and passing pairs
    hovers = [h for row in heat.customdata for h in row if h]
    assert any("passes" in h for h in hovers)
    assert any("fails the" in h for h in hovers)
    assert list(heat.y) == sorted(heat.y, key=lambda label: float(label.replace(",", "")))
    assert fig.data[-1].type == "scatter"                       # the square around the sent pair
    assert fig.data[-1].x[0] == f"{float(document['selected_ask_price']):,.0f}"
    assert fig.data[-1].y[0] == f"{float(document['selected_bid_price']):,.0f}"


def test_ev_heatmap_without_pairs_explains_why():
    document = _diagnostics_document(inventory=50.0)            # full band -> ask only, no pairs scored
    assert document["quote_mode"] in ("ask_only", "none")
    fig = dashboard._ev_heatmap(document)
    assert "No pair was scored" in fig.layout.title.text


def test_candidate_table_and_trace_render_a_real_document():
    document = _diagnostics_document()
    settings = {"commission_perc": 0.001, "exp_ret_min": 0.001}
    table = dashboard._candidate_table(document)
    assert len(table.data) == len(document["candidate_bids"]) + len(document["candidate_asks"])
    assert sum(1 for row in table.data if row["chosen"]) == 2
    trace = _text_of(dashboard._decision_trace(document, settings))
    assert "candidates on the grid" in trace
    assert "fill model: formula" in trace
    assert "pair selected" in trace


def _components(component):
    """Depth-first walk over a Dash component tree."""
    if component is None or isinstance(component, (str, int, float)):
        return
    if isinstance(component, (list, tuple)):
        for child in component:
            yield from _components(child)
        return
    yield component
    yield from _components(getattr(component, "children", None))


def test_track_view_is_bounded_to_the_newest_rows(tmp_path, monkeypatch):
    """A full store decoded into Python objects does not fit the dashboard
    container, so a track view holds the newest MAX_ROWS rounds (SQLite and
    CSV alike) and the status line says so."""
    from result_store import DemoResultStore

    monkeypatch.setattr(dashboard, "MAX_ROWS", 3)
    path = tmp_path / "usdt_track_demo.csv"
    rows = [_row(f"2026-10-01 10:00:{n:02d}", n) for n in range(1, 7)]
    pd.DataFrame(rows).to_csv(path, index=False)
    assert list(dashboard._read_track(str(path))["Round"]) == [4, 5, 6]

    store = DemoResultStore(path.with_suffix(".sqlite3"), list(rows[0]))
    for row in rows:
        store.append_round(row)
    assert list(dashboard._read_track(str(path))["Round"]) == [4, 5, 6]
    assert list(dashboard._read_track(str(path), max_rows=5)["Round"]) == [2, 3, 4, 5, 6]

    monkeypatch.setitem(dashboard.TRACKS, "usdt_demo", dict(dashboard.TRACKS["usdt_demo"], path=str(path)))
    text = dashboard.refresh_status_text("usdt_demo", "all", 0)
    assert text.startswith("3 rounds (the newest 3 rounds")


def test_load_track_loads_one_session_and_lists_every_session(tmp_path, monkeypatch):
    """The view is capped at MAX_ROWS newest rows, but every session stays
    selectable: the picker lists them from the store's summary and a selected
    session is loaded on its own, however old it is."""
    from result_store import DemoResultStore

    monkeypatch.setattr(dashboard, "MAX_ROWS", 3)
    path = tmp_path / "usdt_track_demo.csv"
    store = DemoResultStore(path.with_suffix(".sqlite3"), list(_row("2026-10-01 10:00:00", 1)))
    for n in range(1, 4):
        store.append_round({**_row(f"2026-10-01 09:00:{n:02d}", n), "Session_id": "old"})
    for n in range(4, 9):
        store.append_round({**_row(f"2026-10-01 10:00:{n:02d}", n), "Session_id": "new"})

    assert dashboard.available_sessions(str(path)) == ["new", "old"]
    assert list(dashboard.load_track(str(path))["Round"]) == [6, 7, 8]           # newest rows overall
    assert list(dashboard.load_track(str(path), "old")["Round"]) == [1, 2, 3]    # the old session itself
    assert list(dashboard.load_track(str(path), "new")["Round"]) == [6, 7, 8]
    assert dashboard.load_track(str(path), "missing") is None
    assert dashboard.available_sessions(str(tmp_path / "none.csv")) is None

    monkeypatch.setitem(dashboard.TRACKS, "usdt_demo", dict(dashboard.TRACKS["usdt_demo"], path=str(path)))
    options, value = dashboard.refresh_session_options("usdt_demo", 0, None)
    assert [o["value"] for o in options] == ["all", "new", "old"]
    assert dashboard.refresh_status_text("usdt_demo", "old", 0).startswith("3 rounds — session old")


# ---------- PnL attribution ----------

def _ledger_frame():
    """Three rounds of the worked example in docs/MM_algo1v2.md, appendix A.1:
    start with 10 units and 1000 cash at mid 100; buy 2 at 98 while the mid
    slips to 99; sell 1 at 102 with a 0.5 fee while the mid returns to 100."""
    rows = [
        _row("2026-10-01 10:00:00", 1, **{"inv_current": 10.0, "cash_current": 1000.0, "Mid Price": 100.0}),
        _row("2026-10-01 10:01:00", 2, **{"inv_current": 12.0, "cash_current": 804.0, "Mid Price": 99.0}),
        _row("2026-10-01 10:02:00", 3, **{"inv_current": 11.0, "cash_current": 905.5, "Mid Price": 100.0, "Round Fee": 0.5}),
    ]
    df = pd.DataFrame(rows)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    return df


def test_pnl_attribution_splits_every_round_exactly():
    att = dashboard.pnl_attribution(_ledger_frame())

    assert list(att["Price PnL"]) == pytest.approx([0.0, -10.0, 12.0])      # 10·(99−100), 12·(100−99)
    assert list(att["Trading PnL"]) == pytest.approx([0.0, 2.0, 1.5])      # 2·(99−98); 1·(102−100) − 0.5 fee
    assert list(att["Total PnL"]) == pytest.approx([0.0, -8.0, 13.5])
    assert list(att["Total PnL (cum)"]) == pytest.approx(list(att["Wealth"] - att["Wealth"].iloc[0]))
    totals = dashboard.attribution_totals(att)
    assert totals["total"] == pytest.approx(5.5)
    assert totals["price"] == pytest.approx(2.0) and totals["trading"] == pytest.approx(3.5)
    assert totals["fees"] == pytest.approx(0.5) and totals["start_wealth"] == pytest.approx(2000.0)
    assert totals["verdict"] == "Mainly trading" and totals["sessions"] == 1


def test_pnl_attribution_uses_the_book_midpoint_for_live_rows():
    live = _ledger_frame().drop(columns=["Mid Price", "Round Fee"])
    live["Best Bid"] = [99.0, 98.0, 99.0]
    live["Best Ask"] = [101.0, 100.0, 101.0]

    att = dashboard.pnl_attribution(live)

    assert list(att["Price PnL"]) == pytest.approx([0.0, -10.0, 12.0])
    assert list(att["Trading PnL"]) == pytest.approx([0.0, 2.0, 1.5])
    assert dashboard.attribution_totals(att)["fees"] == 0.0


def test_pnl_attribution_restarts_at_every_session():
    first, second = _ledger_frame(), _ledger_frame()
    second["Session_id"] = "s2"
    second["Timestamp"] = second["Timestamp"] + pd.Timedelta(hours=1)

    att = dashboard.pnl_attribution(pd.concat([second, first]))

    assert list(att["Session_id"]) == ["s1"] * 3 + ["s2"] * 3
    assert list(att["Total PnL (cum)"]) == pytest.approx([0.0, -8.0, 5.5] * 2)
    totals = dashboard.attribution_totals(att)
    assert totals["total"] == pytest.approx(11.0) and totals["sessions"] == 2


def test_pnl_attribution_is_empty_without_the_ledger_columns():
    bare = _ledger_frame().drop(columns=["inv_current", "cash_current"])
    assert dashboard.pnl_attribution(bare).empty
    assert dashboard.attribution_totals(dashboard.pnl_attribution(bare))["verdict"] == "Nothing yet"


def test_attribution_tab_shows_the_verdict_and_the_chart(v2_track):
    text = _text_of(dashboard.render_tab_content("attribution", "usdt_demo", "all", 0, 0.0, 0.0, None))
    assert "Total PnL" in text and "Trading PnL (our fills, net of fees)" in text
    assert "Price PnL (market moves on held inventory)" in text
    assert "Verdict" in text and "Where the PnL came from" in text
    assert "How to read it" in text
