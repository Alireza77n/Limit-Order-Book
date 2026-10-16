"""The dashboard's Algorithm tab: pick one algorithm (its own panels, and
every other tab shows its rounds only) or all of them side by side."""

import json

import pandas as pd
import pytest
from dash import no_update

import dashboard
from config import MMConfig, TRADING_PROFILES
from fill_model import write_fill_table
from strategy.algo3 import Algo3Strategy
from test_dashboard_v2 import _components, _diagnostics_document, _example_book, _frame, _row, _text_of


def _algo3_rows():
    """Three Algorithm 3 rounds of session s2: a buy fill, a quiet round and
    a sell fill at a profit."""
    common = {
        "Session_id": "s2", "Strategy": "algo3", "Quote Mode": "pair", "Selection Reason": "microprice_pair",
        "Bid Fill Prob": None, "Ask Fill Prob": None, "Expected Value": None, "Reference Price": None,
        "Imbalance": 0.4, "Microprice": 100.2, "Centre": 100.3,
    }
    rows = [
        _row("2026-10-02 10:00:00", 1, **common, **{
            "Buy Order ID": "b9", "Buy Status": "filled", "Buy Original Amount": 1.0,
            "Buy Matched Amount": 1.0, "Buy Limit Price": 99.0,
        }),
        _row("2026-10-02 10:01:00", 2, **dict(common, Imbalance=-0.2, Microprice=99.9), inv_current=11.0),
        _row("2026-10-02 10:30:00", 3, **common, **{
            "Sell Order ID": "s9", "Sell Status": "filled", "Sell Original Amount": 1.0,
            "Sell Matched Amount": 1.0, "Sell Limit Price": 101.5, "Portfolio Wealth": 2002.5,
        }),
    ]
    return rows


def _mixed_frame():
    algo1 = _frame().assign(Strategy="algo1")
    algo3 = pd.DataFrame(_algo3_rows())
    algo3["Timestamp"] = pd.to_datetime(algo3["Timestamp"])
    return pd.concat([algo1, algo3], ignore_index=True)


def _algo3_document(round_no):
    cfg = MMConfig(**TRADING_PROFILES["usdt"])
    cfg.algo3_beta = 0.001
    return Algo3Strategy(cfg).diagnostics(_example_book(), 25.0, round_no=round_no, session_id="s2")


@pytest.fixture
def mixed_track(tmp_path, monkeypatch):
    """A usdt_demo track where session s1 ran Algorithm 1 and s2 Algorithm 3,
    with both algorithms' decision documents."""
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path / "state"))
    csv_path = tmp_path / "usdt_demo.csv"
    _mixed_frame().to_csv(csv_path, index=False)
    monkeypatch.setitem(dashboard.TRACKS, "usdt_demo", dict(dashboard.TRACKS["usdt_demo"], path=str(csv_path)))
    table_path = tmp_path / "fill_table_usdtirt.json"
    write_fill_table(
        str(table_path), pair="USDTIRT", horizon_seconds=600, method="test",
        buckets=[{"distance": d, "p_bid": p, "p_ask": p, "n_bid": 100, "n_ask": 100, "n_bid_own": 0, "n_ask_own": 0}
                 for d, p in ((0.001, 0.4), (0.002, 0.2), (0.004, 0.08))],
        extra={"samples": 1000, "own_orders": 0, "window_days": 14, "half_life_days": 3, "sources": ["x.csv"]},
    )
    monkeypatch.setattr(dashboard, "fill_table_path", lambda track_key: str(table_path))
    diag_path = tmp_path / "usdtirt_algo1_diagnostics.jsonl"
    with diag_path.open("w", encoding="utf-8") as stream:
        for round_no in (5, 6, 7):
            stream.write(json.dumps(dict(_diagnostics_document(), round=round_no)) + "\n")
        for round_no in (1, 2):
            stream.write(json.dumps(_algo3_document(round_no)) + "\n")
    monkeypatch.setattr(dashboard, "diagnostics_path", lambda track_key: str(diag_path))
    return tmp_path


# ---------- pure helpers ----------

def test_rows_without_a_strategy_are_algorithm_1():
    legacy = _frame()                                       # no Strategy column at all
    assert set(dashboard.strategy_column(legacy)) == {"algo1"}
    partial = _mixed_frame()
    partial.loc[0, "Strategy"] = None
    assert dashboard.strategy_column(partial).iloc[0] == "algo1"


def test_filter_algorithm_and_the_algorithms_present():
    frame = _mixed_frame()
    assert len(dashboard.filter_algorithm(frame, "algo3")) == 3
    assert len(dashboard.filter_algorithm(frame, "algo1")) == 5
    assert len(dashboard.filter_algorithm(frame, "all")) == 8
    assert dashboard.algorithms_in_rows(frame) == ["algo1", "algo3"]


def test_algorithm_comparison_measures_each_algorithm_on_its_own_rounds():
    settings = dashboard.track_settings("usdt_demo")
    rows = {r["algorithm"]: r for r in dashboard.algorithm_comparison(_mixed_frame(), settings)}
    assert set(rows) == {"algo1", "algo3"}
    assert rows["algo1"]["rounds"] == 5 and rows["algo3"]["rounds"] == 3
    assert rows["algo3"]["quote_rate"] == 1.0 and rows["algo1"]["quote_rate"] == pytest.approx(0.8)
    assert rows["algo3"]["round_trips"] == 1 and rows["algo3"]["fills"] == 2
    assert rows["algo3"]["label"] == "MM_algo2"


def test_track_settings_carry_the_algorithm_and_algorithm_3_minimum_half_spread(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path))
    (tmp_path / "demo_config_usdt.json").write_text(json.dumps({"algorithm": "algo3", "algo3_beta": 0.001}))
    settings = dashboard.track_settings("usdt_demo")
    assert settings["algorithm"] == "algo3" and settings["algo3_beta"] == 0.001
    gross = settings["gross_bar"]
    assert settings["algo3_h_min"] == pytest.approx(gross / (2 + gross))


def test_the_fill_forecast_is_not_scored_for_rounds_without_a_forecast():
    rows = {r["metric"]: r for r in dashboard.gate_rows(pd.DataFrame(_algo3_rows()).assign(
        Timestamp=lambda d: pd.to_datetime(d["Timestamp"])), dashboard.track_settings("usdt_demo"))}
    assert rows["fill forecast error (predicted / realised − 1)"]["passed"] is None


# ---------- layout and callbacks ----------

def test_the_algorithm_tab_comes_first_and_its_list_is_always_in_the_layout():
    tabs = next(c for c in _components(dashboard.app.layout) if getattr(c, "id", None) == "main-tabs")
    assert [t.value for t in tabs.children][0] == "algorithm"
    picker = next(c for c in _components(dashboard.app.layout) if getattr(c, "id", None) == "algorithm-choice")
    assert [o["value"] for o in picker.options] == ["algo1", "algo3", "all"]
    assert picker.value == "all"


def test_the_list_of_algorithms_shows_on_the_algorithm_tab_only():
    assert dashboard.show_algorithm_picker("algorithm") == {}
    assert dashboard.show_algorithm_picker("overview") == {"display": "none"}


def test_deep_link_selects_the_algorithm():
    assert dashboard.apply_url_algorithm("?algo=algo3") == "algo3"
    assert dashboard.apply_url_algorithm("?tab=algorithm&algo=all") == "all"
    assert dashboard.apply_url_algorithm("?algo=algo9") is no_update
    assert dashboard.apply_url_algorithm(None) is no_update
    assert dashboard.apply_url("?tab=algorithm&track=usdt_demo") == ("algorithm", "usdt_demo")


def test_sessions_are_labelled_with_their_algorithm(mixed_track):
    options, value = dashboard.refresh_session_options("usdt_demo", 0, None)
    labels = {o["value"]: o["label"] for o in options}
    assert labels["s1"] == "s1 · MM_algo1v2" and labels["s2"] == "s2 · MM_algo2"
    assert value == "all"


# ---------- the Algorithm tab ----------

def test_all_algorithms_side_by_side(mixed_track):
    text = _text_of(dashboard.render_tab_content("algorithm", "usdt_demo", "all", 0, 0.0, 0.0, "all"))
    assert "Side by side" in text and "2 algorithm(s) in this view" in text
    assert "MM_algo1v2" in text and "MM_algo2" in text
    assert "Net PnL after fees — running sum per algorithm" in text
    assert "Algorithms side by side — same track, each on its own rounds" in text


def test_one_algorithm_shows_its_own_panels(mixed_track):
    text = _text_of(dashboard.render_tab_content("algorithm", "usdt_demo", "all", 0, 0.0, 0.0, "algo3"))
    assert "MM_algo2" in text and "Algorithm 3: order-book imbalance, microprice and a fixed spread" in text
    assert "Measures the imbalance of the resting volumes" in text
    assert "Order-book imbalance I = (V_bid − V_ask)" in text
    assert "Where the centre sat" in text
    assert "Quote rate (rounds)" in text and "100%" in text
    assert "algo3_beta" in text and "does not use the fill model" in text

    text = _text_of(dashboard.render_tab_content("algorithm", "usdt_demo", "all", 0, 0.0, 0.0, "algo1"))
    assert "Expected value J = p_bid · p_ask · R_net of the quote sent" in text
    assert "uses the fill model" in text


def test_an_algorithm_that_has_not_run_yet_says_how_to_start_it(tmp_path, monkeypatch):
    csv_path = tmp_path / "usdt_demo.csv"
    _frame().to_csv(csv_path, index=False)                 # Algorithm 1 rounds only
    monkeypatch.setitem(dashboard.TRACKS, "usdt_demo", dict(dashboard.TRACKS["usdt_demo"], path=str(csv_path)))
    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path / "state"))
    text = _text_of(dashboard.render_tab_content("algorithm", "usdt_demo", "all", 0, 0.0, 0.0, "algo3"))
    assert "No rounds of MM_algo2 in this view yet" in text
    assert "Config USDT → Algorithm and choose MM_algo2:" in text
    assert "No rounds of MM_algo2" in _text_of(dashboard.render_tab_content("overview", "usdt_demo", "all", 0, 0.0, 0.0, "algo3"))


# ---------- every other tab follows the choice ----------

@pytest.mark.parametrize("tab", [t for t in dashboard.TAB_VALUES if t != "algorithm"])
@pytest.mark.parametrize("algorithm", ["algo1", "algo3", "all"])
def test_every_tab_renders_for_each_choice(mixed_track, tab, algorithm):
    rendered = dashboard.render_tab_content(tab, "usdt_demo", "all", 0, 0.0, 0.0, algorithm)
    text = _text_of(rendered)
    assert "No data yet" not in text and "No rounds of" not in text


def test_status_line_counts_the_chosen_algorithm_only(mixed_track):
    assert dashboard.refresh_status_text("usdt_demo", "all", 0, "algo3").startswith("3 rounds — MM_algo2 only")
    assert dashboard.refresh_status_text("usdt_demo", "all", 0, "all").startswith("8 rounds")


def test_rounds_table_follows_the_choice(mixed_track):
    rows = dashboard.refresh_rounds_table(1, "usdt_demo", "all", "algo3")
    assert {r["Strategy"] for r in rows} == {"algo3"} and len(rows) == 3


def test_quote_decision_shows_the_round_as_the_algorithm_that_made_it(mixed_track):
    rendered = dashboard.render_tab_content("decision", "usdt_demo", "all", 0, 0.0, 0.0, "algo3")
    dropdown = next(c for c in _components(rendered) if getattr(c, "id", None) == "decision-round")
    assert [o["value"] for o in dropdown.options] == ["s2:2", "s2:1"]
    text = _text_of(rendered)
    assert "Top-k book levels" in text and "Decision trace" in text and "MM_algo2, this round" in text
    assert "h_min" in text

    rendered = dashboard.render_tab_content("decision", "usdt_demo", "all", 0, 0.0, 0.0, "all")
    dropdown = next(c for c in _components(rendered) if getattr(c, "id", None) == "decision-round")
    assert len(dropdown.options) == 5
    assert any("MM_algo2" in o["label"] for o in dropdown.options)
    assert any("MM_algo1v2" in o["label"] for o in dropdown.options)

    assert "MM_algo1v2, this round" in _text_of(dashboard.refresh_decision_panels("s1:6", "usdt_demo", "algo1"))
    assert "MM_algo2, this round" in _text_of(dashboard.refresh_decision_panels("s2:1", "usdt_demo", "algo3"))


def test_fill_model_tab_explains_that_algorithm_3_has_no_forecast(mixed_track):
    text = _text_of(dashboard.render_tab_content("fillmodel", "usdt_demo", "all", 0, 0.0, 0.0, "algo3"))
    assert "never reads a fill probability" in text


def test_algorithm_3_decision_trace_walks_the_specification_steps():
    document = _algo3_document(3)
    text = _text_of(dashboard._algo3_decision_trace(document, dashboard.track_settings("usdt_demo")))
    for fragment in ("imbalance over k = 3 levels", "microprice", "centre", "half-spread h = max", "fee check: R_net"):
        assert fragment in text
    ladder = dashboard._algo3_ladder_chart(document)
    names = [trace.name for trace in ladder.data]
    assert any(name.startswith("our bid") for name in names) and any(name.startswith("centre") for name in names)
    prices = [trace.y[0] for trace in ladder.data]
    assert prices == sorted(prices, reverse=True)          # the legend reads like the price axis


def test_the_picker_and_the_headings_show_the_exact_branch_names(mixed_track):
    picker = next(c for c in _components(dashboard.app.layout) if getattr(c, "id", None) == "algorithm-choice")
    names = [o["label"].children[0].children for o in picker.options]
    assert names == ["MM_algo1v2", "MM_algo2", "All algorithms"]
    # the description heading is not the upper-cased generic heading
    rendered = dashboard.render_tab_content("algorithm", "usdt_demo", "all", 0, 0.0, 0.0, "algo1")
    heading = next(c for c in _components(rendered)
                   if getattr(c, "children", None) == "MM_algo1v2" and type(c).__name__ == "Span")
    assert "textTransform" not in (heading.style or {})
