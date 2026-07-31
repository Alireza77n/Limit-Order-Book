"""Unit tests for dashboard.py's pure data-handling functions -- no Dash
server involved, just CSV-in/DataFrame-out logic."""

import pandas as pd
import pytest

from dashboard import filter_session, load_track, session_ids, with_derived_spreads

COLUMNS = [
    "Session_id", "Algorithm", "Crypto", "Timestamp",
    "Bid Level", "Ask Level", "inv_current", "cash_current", "Portfolio Wealth",
    "Best Bid", "Best Ask", "Last Price", "Bid Price Sent", "Ask Price Sent",
    "Buy Size", "Sell Size", "Buy Sent", "Sell Sent", "Buy Status", "Sell Status",
]


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
