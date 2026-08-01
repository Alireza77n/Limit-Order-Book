"""Unit tests for dashboard.py's pure data-handling functions -- no Dash
server involved, just CSV-in/DataFrame-out logic."""

import pandas as pd
import pytest

from dashboard import activity_records, filter_session, load_track, session_ids, with_derived_spreads

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
