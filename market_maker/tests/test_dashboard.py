"""Unit tests for dashboard.py's pure data-handling functions -- no Dash
server involved, just CSV-in/DataFrame-out logic."""

import json

import pandas as pd
import pytest

from dashboard import activity_records, filter_session, load_track, session_ids, with_derived_spreads
from config import TRADING_PROFILES
from demo_config import desired_digest, load_desired_config
from result_store import DemoResultStore

COLUMNS = [
    "Session_id", "Algorithm", "Crypto", "Timestamp",
    "Bid Level", "Ask Level", "inv_current", "cash_current", "Portfolio Wealth",
    "Best Bid", "Best Ask", "Last Price", "Bid Price Sent", "Ask Price Sent",
    "Buy Size", "Sell Size", "Buy Sent", "Sell Sent", "Buy Status", "Sell Status",
]

DEMO_ROW = {
    "Session_id": "shared-session",
    "Algorithm": "1usdt",
    "Crypto": "USDTIRT",
    "Mode": "demo",
    "Round": 2,
    "Timestamp": "2026-08-01 12:00:01",
    "Quote Status": "quote_generated",
    "No Quote Reason": None,
    "Bid Level": 2,
    "Ask Level": 3,
    "inv_current": 0.4,
    "cash_current": 960.0,
    "Locked Cash": 0.0,
    "Locked Inventory": 0.0,
    "Available Cash": 960.0,
    "Available Inventory": 0.4,
    "Portfolio Wealth": 1000.0,
    "Best Bid": 99.0,
    "Best Ask": 100.0,
    "Last Price": 100.0,
    "Bid Price": 98.0,
    "Ask Price": 101.0,
    "Buy Order ID": "buy-1",
    "Buy Status": "partially_filled",
    "Buy Not Sent Reason": None,
    "Buy Original Amount": 1.0,
    "Buy Matched Amount": 0.4,
    "Buy Processed Matched Amount": 0.4,
    "Buy Remaining Amount": 0.6,
    "Buy Fill Reason": "trade_cross",
    "Buy Selected Level": 2,
    "Buy Limit Price": 98.0,
    "Buy Locked Cash": 58.8,
    "Buy Telegram Message ID": 101,
    "Sell Order ID": None,
    "Sell Status": "not_sent_insufficient_inventory",
    "Sell Not Sent Reason": "insufficient_inventory",
    "Sell Original Amount": 0.0,
    "Sell Matched Amount": 0.0,
    "Sell Processed Matched Amount": 0.0,
    "Sell Remaining Amount": 0.0,
    "Sell Fill Reason": None,
    "Sell Selected Level": None,
    "Sell Limit Price": None,
    "Sell Locked Inventory": 0.0,
    "Sell Telegram Message ID": None,
    "Round Fee": 0.04,
    "Session Pair Fee": 0.04,
    "Session All Symbols Fee": 0.04,
    "Pair All Time Fee": 1.04,
    "All Symbols All Time Fee": 2.04,
}


def _make_csv(tmp_path, rows):
    path = tmp_path / "track.csv"
    pd.DataFrame(rows, columns=COLUMNS).to_csv(path, index=False)
    return str(path)


def test_load_track_missing_file_returns_none(tmp_path):
    assert load_track(str(tmp_path / "does_not_exist.csv")) is None


def test_load_track_empty_file_returns_none(tmp_path):
    path = tmp_path / "empty.csv"
    pd.DataFrame(columns=COLUMNS).to_csv(path, index=False)
    assert load_track(str(path)) is None


def test_load_track_parses_timestamp(tmp_path):
    path = _make_csv(tmp_path, [
        [1, "1btc", "BTCIRT", "2026-07-31 10:00:00", 1, 1, 0.0001, 1000, 1100,
         100, 101, 100.5, 99.01, 101.99, 0.00001, 0.00001, 1, 1, "id1", "id2"],
    ])
    df = load_track(path)
    assert df is not None
    assert pd.api.types.is_datetime64_any_dtype(df["Timestamp"])


def test_with_derived_spreads_matches_readme_formulas(tmp_path):
    path = _make_csv(tmp_path, [
        [1, "1btc", "BTCIRT", "2026-07-31 10:00:00", 1, 1, 0.0001, 1000, 1100,
         100, 101, 100.5, 99.0, 102.0, 0.00001, 0.00001, 1, 1, "id1", "id2"],
    ])
    df = with_derived_spreads(load_track(path))
    mid = (100 + 101) / 2.0  # 100.5
    assert df["Mid Price"].iloc[0] == pytest.approx(mid)
    assert df["Bid Spread"].iloc[0] == pytest.approx(mid - 99.0)
    assert df["Ask Spread"].iloc[0] == pytest.approx(102.0 - mid)


def test_session_ids_sorted_most_recent_first(tmp_path):
    path = _make_csv(tmp_path, [
        [1, "1btc", "BTCIRT", "2026-07-31 10:00:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
        [3, "1btc", "BTCIRT", "2026-07-31 10:01:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
        [2, "1btc", "BTCIRT", "2026-07-31 10:02:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
    ])
    df = load_track(path)
    assert session_ids(df) == [3, 2, 1]


def test_filter_session_all_returns_everything(tmp_path):
    path = _make_csv(tmp_path, [
        [1, "1btc", "BTCIRT", "2026-07-31 10:00:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
        [2, "1btc", "BTCIRT", "2026-07-31 10:01:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
    ])
    df = load_track(path)
    assert len(filter_session(df, "all")) == 2
    assert len(filter_session(df, None)) == 2


def test_filter_session_specific_id_filters_rows(tmp_path):
    path = _make_csv(tmp_path, [
        [1, "1btc", "BTCIRT", "2026-07-31 10:00:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
        [2, "1btc", "BTCIRT", "2026-07-31 10:01:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
    ])
    df = load_track(path)
    filtered = filter_session(df, 2)
    assert len(filtered) == 1
    assert filtered["Session_id"].iloc[0] == 2


def test_session_picker_defaults_to_all_not_newest(tmp_path, monkeypatch):
    """session.py mints a new session_id on every full-stack restart (see
    initialize_stack_for_workers) -- the newest session is near-empty right
    after one. Defaulting the dashboard's picker to that newest session
    made a restart look like data loss even though every prior session's
    rows are still in the store. Must default to "all" instead."""
    import dashboard

    path = _make_csv(tmp_path, [
        [1, "1btc", "BTCIRT", "2026-07-31 10:00:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
        [2, "1btc", "BTCIRT", "2026-07-31 10:01:00", 1, 1, 0, 0, 0, 100, 101, 100, 99, 102, 0, 0, 0, 0, "a", "b"],
    ])
    monkeypatch.setitem(dashboard.TRACKS, "btc_demo", dict(dashboard.TRACKS["btc_demo"], path=path))

    options, default_value = dashboard.refresh_session_options("btc_demo", 0)

    assert default_value == "all"
    assert options[0] == {"label": "All sessions (compare)", "value": "all"}
    assert {opt["value"] for opt in options[1:]} == {1, 2}


def test_demo_spreads_use_explicit_quote_price_columns():
    frame = pd.DataFrame([DEMO_ROW])

    result = with_derived_spreads(frame)

    assert result["Bid Spread"].iloc[0] == pytest.approx(1.5)
    assert result["Ask Spread"].iloc[0] == pytest.approx(1.5)


def test_demo_activity_matches_explicit_csv_lifecycle_fields():
    frame = pd.DataFrame([DEMO_ROW])

    records = activity_records(frame)

    assert len(records) == 1
    assert records[0]["Buy Order ID"] == "buy-1"
    assert records[0]["Buy Status"] == "partially_filled"
    assert records[0]["Buy Matched Amount"] == pytest.approx(0.4)
    assert records[0]["Buy Remaining Amount"] == pytest.approx(0.6)
    assert records[0]["Buy Telegram Message ID"] == 101
    assert records[0]["Sell Status"] == "not_sent_insufficient_inventory"


def test_load_track_prefers_sibling_sqlite_store_over_stale_csv(tmp_path):
    path = tmp_path / "track.csv"
    stale = {**DEMO_ROW, "Round": 1, "Timestamp": "2026-08-01 12:00:00"}
    fresh = {**DEMO_ROW, "Round": 2, "Timestamp": "2026-08-01 12:00:10"}
    pd.DataFrame([stale]).to_csv(path, index=False)
    store = DemoResultStore(path.with_suffix(".sqlite3"), list(DEMO_ROW))
    store.append_round(fresh)

    result = load_track(str(path))

    assert list(result["Round"]) == [2]
    assert pd.api.types.is_datetime64_any_dtype(result["Timestamp"])


def test_load_track_falls_back_to_csv_without_sqlite(tmp_path):
    path = tmp_path / "track.csv"
    pd.DataFrame([{**DEMO_ROW, "Round": 7}]).to_csv(path, index=False)

    assert list(load_track(str(path))["Round"]) == [7]


def test_activity_exposes_no_quote_operational_fields():
    frame = pd.DataFrame([{
        **DEMO_ROW,
        "No Quote Consecutive Count": 30,
        "No Quote Episode Seconds": 300,
        "No Quote Alert State": "alerted",
        "No Quote Candidate Diagnostics": '{"required_threshold":0.002}',
    }])

    record = activity_records(frame)[0]

    assert record["No Quote Consecutive Count"] == 30
    assert record["No Quote Episode Seconds"] == 300
    assert record["No Quote Alert State"] == "alerted"
    assert record["No Quote Candidate Diagnostics"] == '{"required_threshold":0.002}'


def test_no_quote_summary_explains_chart_gaps(tmp_path):
    """Rounds where Algorithm 1 declined to quote log no Bid/Ask Price, so the
    line charts gap. The status line must say so, otherwise the gaps read as
    lost data -- see reports/demo-portfolio-jump-investigation.md."""
    from dashboard import no_quote_summary

    df = pd.DataFrame([
        {"Quote Status": "no_quote", "No Quote Reason": "profitability_threshold"},
        {"Quote Status": "no_quote", "No Quote Reason": "profitability_threshold"},
        {"Quote Status": "quote_generated", "No Quote Reason": None},
    ])

    summary = no_quote_summary(df)

    assert "2 of 3 rounds produced no quote" in summary
    assert "profitability_threshold: 2" in summary
    assert "not missing data" in summary


def test_no_quote_summary_silent_when_every_round_quoted(tmp_path):
    from dashboard import no_quote_summary

    df = pd.DataFrame([{"Quote Status": "quote_generated", "No Quote Reason": None}])

    assert no_quote_summary(df) == ""


def test_no_quote_summary_tolerates_legacy_csv_without_the_column(tmp_path):
    """Older Real-mode CSVs have no Quote Status column at all."""
    from dashboard import no_quote_summary

    assert no_quote_summary(pd.DataFrame([{"Portfolio Wealth": 1.0}])) == ""


# ---------- Effective Demo configuration ----------

def _configure_dashboard_state(monkeypatch, tmp_path, session_id="session-current"):
    import dashboard

    monkeypatch.setattr(dashboard, "DEMO_STATE_DIR", str(tmp_path))
    (tmp_path / "stack_session.json").write_text(json.dumps({"session_id": session_id}))


def _save_desired(tmp_path, pair, **overrides):
    (tmp_path / f"demo_config_{pair}.json").write_text(json.dumps(overrides))
    return load_desired_config(pair, tmp_path, TRADING_PROFILES)


def _write_runtime_snapshot(
    tmp_path, pair, desired, *, applied=None, session_id="session-current",
):
    document = {
        "schema_version": 1,
        "pair": pair,
        "session_id": session_id,
        "applied": dict(desired if applied is None else applied),
        "pending_new_session": {
            field: desired[field]
            for field in ("demo_inv_initial", "cash_initial", "commission_perc")
            if field in desired and desired[field] != (applied or desired).get(field)
        },
        "desired_digest": desired_digest(desired),
        "status": "applied",
        "error": None,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    (tmp_path / f"runtime_config_{pair}.json").write_text(json.dumps(document))


def test_dashboard_config_uses_selected_pair_and_reports_pending(tmp_path, monkeypatch):
    import dashboard

    _configure_dashboard_state(monkeypatch, tmp_path)
    desired = _save_desired(tmp_path, "btc", order_size=0.2, commission_perc=0.02)
    applied = dict(desired, commission_perc=0.001)
    _write_runtime_snapshot(tmp_path, "btc", desired, applied=applied)

    view = dashboard.load_effective_demo_config("btc_demo")

    assert view["pair"] == "btc"
    assert view["desired"]["commission_perc"] == pytest.approx(0.02)
    assert view["applied"]["order_size"] == pytest.approx(0.2)
    assert view["pending_new_session"]["commission_perc"] == pytest.approx(0.02)
    assert view["status"]["fields"]["commission_perc"]["label"] == (
        "Saved; applies next session"
    )


def test_dashboard_config_rejects_snapshot_from_another_stack_session(
    tmp_path, monkeypatch,
):
    import dashboard

    _configure_dashboard_state(monkeypatch, tmp_path, session_id="session-current")
    desired = _save_desired(tmp_path, "usdt", order_size=2.0)
    _write_runtime_snapshot(
        tmp_path, "usdt", desired, session_id="session-previous",
    )

    view = dashboard.load_effective_demo_config("usdt_demo")

    assert view["status"]["state"] == "stale"
    assert view["applied"] == {}


def test_real_track_has_no_demo_config_panel():
    import dashboard

    assert dashboard.load_effective_demo_config("btc_real") is None


def test_effective_config_panel_uses_shared_field_rows(tmp_path, monkeypatch):
    import dashboard

    _configure_dashboard_state(monkeypatch, tmp_path)
    desired = _save_desired(tmp_path, "btc", commission_perc=0.02)
    _write_runtime_snapshot(
        tmp_path, "btc", desired, applied=dict(desired, commission_perc=0.001),
    )

    style, summary, rows = dashboard.refresh_effective_config_panel("btc_demo", 4)

    commission = next(row for row in rows if row["Field"] == "commission_perc")
    assert style == {}
    assert summary == "BTC Demo configuration — Worker stopped; applies when started"
    assert commission == {
        "Field": "commission_perc",
        "Desired": 0.02,
        "Applied": 0.001,
        "Mode": "new_session",
        "Status": "Saved; applies next session",
    }
    hidden_style, hidden_summary, hidden_rows = (
        dashboard.refresh_effective_config_panel("btc_real", 5)
    )
    assert hidden_style == {"display": "none"}
    assert hidden_summary == ""
    assert hidden_rows == []
