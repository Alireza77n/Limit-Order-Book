import os
import sqlite3

import pandas as pd
import pytest

import result_store
from result_store import DemoResultStore


COLUMNS = [
    "Session_id",
    "Crypto",
    "Round",
    "Timestamp",
    "Buy Order ID",
    "Buy Status",
    "Sell Order ID",
    "Sell Status",
    "cash_current",
]


def row(round_no, *, buy_id=None, sell_id=None):
    return {
        "Session_id": "session-1",
        "Crypto": "BTCIRT",
        "Round": round_no,
        "Timestamp": f"2026-08-01 12:00:{round_no:02d}",
        "Buy Order ID": buy_id,
        "Buy Status": "open" if buy_id else "no_quote",
        "Sell Order ID": sell_id,
        "Sell Status": "open" if sell_id else "no_quote",
        "cash_current": 100.0,
    }


def test_append_and_indexed_lifecycle_update_preserve_prior_rows(tmp_path):
    store = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)
    store.append_round(row(1, buy_id="buy-1"))
    store.append_round(row(2, buy_id="buy-2"))

    store.update_order_rows(
        {"buy-1": {"Buy Status": "filled"}},
        {"buy-1": {"cash_current": 90.0}},
    )

    view = store.load_view()
    assert list(view["Round"]) == [1, 2]
    assert view.loc[0, "Buy Status"] == "filled"
    assert view.loc[0, "cash_current"] == 90.0
    assert view.loc[1, "Buy Status"] == "open"


def test_bounded_export_and_compaction(tmp_path):
    csv_path = tmp_path / "btc.csv"
    store = DemoResultStore(
        tmp_path / "btc.sqlite3",
        COLUMNS,
        csv_export_path=csv_path,
        csv_export_max_rows=3,
    )
    for number in range(1, 7):
        store.append_round(row(number))

    store.export_bounded_csv()

    assert list(pd.read_csv(csv_path)["Round"]) == [4, 5, 6]
    assert store.compact(retain_rows=4) == 2
    assert list(store.load_view()["Round"]) == [3, 4, 5, 6]


def test_streamed_export_formats_cells_like_pandas(tmp_path):
    """export_bounded_csv streams rows straight from SQLite (no DataFrame);
    nulls, booleans, floats and nested values must read back as before."""
    csv_path = tmp_path / "btc.csv"
    columns = COLUMNS + ["Flag", "Nested"]
    store = DemoResultStore(tmp_path / "btc.sqlite3", columns, csv_export_path=csv_path)
    store.append_round({**row(1, buy_id="buy-1"), "Buy Status": None, "cash_current": 1234.5,
                        "Flag": True, "Nested": {"a": 1}})
    store.append_round({**row(2), "cash_current": 7, "Flag": False, "Nested": None})

    store.export_bounded_csv()

    exported = pd.read_csv(csv_path)
    assert list(exported.columns) == columns
    assert list(exported["Round"]) == [1, 2]
    assert pd.isna(exported.loc[0, "Buy Status"])
    assert exported.loc[0, "cash_current"] == 1234.5
    assert exported.loc[1, "cash_current"] == 7
    assert list(exported["Flag"]) == [True, False]
    assert exported.loc[0, "Nested"] == '{"a":1}'
    assert pd.isna(exported.loc[1, "Nested"])
    assert not list(tmp_path.glob(".btc.csv.*.tmp"))


def test_legacy_csv_import_is_idempotent(tmp_path):
    csv_path = tmp_path / "legacy.csv"
    pd.DataFrame([row(1), row(2)]).to_csv(csv_path, index=False)
    store = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)

    assert store.import_legacy_csv_once(csv_path) == 2
    assert store.import_legacy_csv_once(csv_path) == 0
    assert len(store.load_view()) == 2


def test_append_updates_existing_round_instead_of_duplicating(tmp_path):
    store = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)
    store.append_round(row(1, buy_id="first"))
    store.append_round(row(1, buy_id="replacement"))

    view = store.load_view()
    assert len(view) == 1
    assert view.iloc[0]["Buy Order ID"] == "replacement"


def test_sqlite_indexes_order_ids(tmp_path):
    store = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)
    with sqlite3.connect(store.db_path) as connection:
        indexes = {record[1] for record in connection.execute("PRAGMA index_list(rounds)")}

    assert "idx_rounds_buy_order_id" in indexes
    assert "idx_rounds_sell_order_id" in indexes


def test_load_view_by_session_and_session_summary(tmp_path):
    store = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)
    for number in (1, 2):
        store.append_round(row(number))
    store.append_round({**row(3), "Session_id": "session-2"})
    store.append_round({**row(4), "Session_id": "session-2"})

    assert list(store.load_view(session_id="session-2")["Round"]) == [3, 4]
    assert list(store.load_view(limit=1, session_id="session-1")["Round"]) == [2]
    assert store.load_view(session_id="missing").empty
    summary = store.session_summary()
    assert [(s["session_id"], s["rounds"]) for s in summary] == [("session-1", 2), ("session-2", 2)]
    assert summary[0]["first_timestamp"] == "2026-08-01 12:00:01"
    assert summary[1]["last_timestamp"] == "2026-08-01 12:00:04"


def test_session_summary_names_the_algorithms_each_session_ran(tmp_path):
    """Rows written before the Strategy column existed count as Algorithm 1;
    a session that switched algorithm lists both."""
    store = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS + ["Strategy"])
    store.append_round(row(1))                                         # legacy row: no Strategy
    store.append_round({**row(2), "Strategy": "algo1"})
    store.append_round({**row(3), "Session_id": "session-2", "Strategy": "algo3"})
    store.append_round({**row(4), "Session_id": "session-3", "Strategy": "algo1"})
    store.append_round({**row(5), "Session_id": "session-3", "Strategy": "algo3"})

    summary = {s["session_id"]: s["algorithms"] for s in store.session_summary()}

    assert summary == {"session-1": ["algo1"], "session-2": ["algo3"], "session-3": ["algo1", "algo3"]}


def test_read_only_reader_sees_every_row_and_never_writes(tmp_path):
    """The dashboard and admin containers mount the results directory
    read-only; a reader opened there must read the writer's WAL store
    without touching it."""
    writer = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)
    writer.append_round(row(1))
    writer.append_round({**row(2), "Session_id": "session-2"})

    reader = DemoResultStore.open_existing(writer.db_path, read_only=True)

    assert reader.read_only
    assert list(reader.load_view()["Round"]) == [1, 2]
    assert [s["session_id"] for s in reader.session_summary()] == ["session-1", "session-2"]
    assert reader.latest_round_no("session-1", "BTCIRT") == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.append_round(row(3))
    writer.append_round(row(3))
    assert list(reader.load_view(session_id="session-1")["Round"]) == [1, 3]


def test_open_existing_goes_read_only_on_an_unwritable_store(tmp_path, monkeypatch):
    writer = DemoResultStore(tmp_path / "btc.sqlite3", COLUMNS)
    writer.append_round(row(1))
    real_access = os.access
    monkeypatch.setattr(
        result_store.os, "access",
        lambda path, mode, **kw: False if mode == os.W_OK else real_access(path, mode, **kw),
    )

    reader = DemoResultStore.open_existing(writer.db_path)
    forced_writer = DemoResultStore.open_existing(writer.db_path, read_only=False)

    assert reader.read_only and not forced_writer.read_only
    assert len(reader.load_view()) == 1
    assert forced_writer.append_round(row(2)) > 0
