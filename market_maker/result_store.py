"""Bounded, transactional persistence for Demo reporting rows."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import pandas as pd


class DemoResultStore:
    """Keep mutable Demo rows in SQLite and CSV as a bounded export only."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        db_path,
        columns: Sequence[str],
        csv_export_path=None,
        csv_export_max_rows: int = 10_000,
        read_only: bool = False,
    ):
        self.db_path = Path(db_path)
        self.columns = list(columns)
        self.csv_export_path = Path(csv_export_path) if csv_export_path else None
        self.csv_export_max_rows = max(1, int(csv_export_max_rows))
        self.read_only = bool(read_only)
        if self.read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(self.db_path)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    @staticmethod
    def _read_only_uri(path: Path) -> str:
        return Path(path).resolve().as_uri() + "?mode=ro"

    @classmethod
    def _writable(cls, path: Path) -> bool:
        return os.access(path, os.W_OK) and os.access(Path(path).parent, os.W_OK)

    def _connect(self):
        if self.read_only:
            # A reader on a read-only mount (the dashboard and admin
            # containers mount results :ro). mode=ro never attempts a
            # write; the WAL pragma is skipped because the writer set it
            # and re-setting it would need write access.
            connection = sqlite3.connect(self._read_only_uri(self.db_path), uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(self.db_path, timeout=5.0)
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @classmethod
    def open_existing(
        cls, db_path, csv_export_path=None, csv_export_max_rows=10_000, read_only=None,
    ):
        """Reopen a store an earlier process created, recovering its column
        schema from the db itself. csv_export_path is NOT stored in the db --
        pass it explicitly if this instance needs to call
        export_bounded_csv(); omitted, exports silently no-op (csv_export_path
        stays None), which is correct for read-only callers like dashboard.py.

        read_only=None opens the store read-only when the file or its
        directory is not writable (a :ro container mount): such a reader
        must never run the schema/metadata upsert of a writer, which fails
        with "attempt to write a readonly database" and used to push the
        dashboard and admin onto the bounded CSV export, hiding every
        session older than its newest rows."""
        path = Path(db_path)
        if not path.exists():
            raise FileNotFoundError(path)
        if read_only is None:
            read_only = not cls._writable(path)
        with sqlite3.connect(cls._read_only_uri(path), uri=True, timeout=5.0) as connection:
            record = connection.execute(
                "SELECT value FROM metadata WHERE key='columns'"
            ).fetchone()
        if not record:
            raise ValueError(f"Demo result store has no column metadata: {path}")
        return cls(
            path, json.loads(record[0]),
            csv_export_path=csv_export_path, csv_export_max_rows=csv_export_max_rows,
            read_only=read_only,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    round_no INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    buy_order_id TEXT,
                    sell_order_id TEXT,
                    UNIQUE(session_id, symbol, round_no)
                );
                CREATE INDEX IF NOT EXISTS idx_rounds_buy_order_id
                    ON rounds(buy_order_id);
                CREATE INDEX IF NOT EXISTS idx_rounds_sell_order_id
                    ON rounds(sell_order_id);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('columns', ?)",
                (json.dumps(self.columns),),
            )

    @staticmethod
    def _json_safe(value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if hasattr(value, "item"):
            return value.item()
        return value

    def _normalize_row(self, row: Mapping[str, object]) -> dict:
        return {column: self._json_safe(row.get(column)) for column in self.columns}

    def append_round(self, row: Mapping[str, object]) -> int:
        document = self._normalize_row(row)
        session_id = str(document.get("Session_id") or "")
        symbol = str(document.get("Crypto") or "")
        round_no = int(document.get("Round") or 0)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rounds(
                    session_id, symbol, round_no, payload_json,
                    buy_order_id, sell_order_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, symbol, round_no) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    buy_order_id=excluded.buy_order_id,
                    sell_order_id=excluded.sell_order_id
                """,
                (
                    session_id,
                    symbol,
                    round_no,
                    payload,
                    document.get("Buy Order ID"),
                    document.get("Sell Order ID"),
                ),
            )
            record = connection.execute(
                "SELECT id FROM rounds WHERE session_id=? AND symbol=? AND round_no=?",
                (session_id, symbol, round_no),
            ).fetchone()
        return int(record[0])

    def latest_round_no(self, session_id: str, symbol: str) -> int:
        with self._connect() as connection:
            record = connection.execute(
                "SELECT COALESCE(MAX(round_no), 0) FROM rounds "
                "WHERE session_id=? AND symbol=?",
                (str(session_id), str(symbol)),
            ).fetchone()
        return int(record[0])

    def update_order_rows(
        self,
        order_fields: Mapping[str, Mapping[str, object]],
        financial_fields: Optional[Mapping[str, Mapping[str, object]]] = None,
    ) -> int:
        financial_fields = financial_fields or {}
        changed = 0
        with self._connect() as connection:
            for order_id, lifecycle in order_fields.items():
                rows = connection.execute(
                    """
                    SELECT id, payload_json FROM rounds
                    WHERE buy_order_id=? OR sell_order_id=?
                    """,
                    (order_id, order_id),
                ).fetchall()
                for row_id, payload in rows:
                    document = json.loads(payload)
                    document.update(
                        {key: self._json_safe(value) for key, value in lifecycle.items()}
                    )
                    document.update(
                        {
                            key: self._json_safe(value)
                            for key, value in financial_fields.get(order_id, {}).items()
                        }
                    )
                    connection.execute(
                        "UPDATE rounds SET payload_json=? WHERE id=?",
                        (
                            json.dumps(document, sort_keys=True, separators=(",", ":")),
                            row_id,
                        ),
                    )
                    changed += 1
        return changed

    def load_view(
        self, limit: Optional[int] = None, session_id: Optional[str] = None,
    ) -> pd.DataFrame:
        """Rows as a DataFrame, oldest first; the newest ``limit`` rows when
        given, and only one session's rows when ``session_id`` is given."""
        where = "WHERE session_id=?" if session_id is not None else ""
        parameters: list = [str(session_id)] if session_id is not None else []
        query = f"SELECT payload_json FROM rounds {where} ORDER BY id"
        if limit is not None:
            query = (
                "SELECT payload_json FROM ("
                f"SELECT id, payload_json FROM rounds {where} ORDER BY id DESC LIMIT ?"
                ") ORDER BY id"
            )
            parameters.append(max(0, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        documents = [json.loads(record[0]) for record in rows]
        return pd.DataFrame(documents, columns=self.columns, dtype=object)

    def session_summary(self) -> list:
        """Every session in the store, oldest first: id, row count, first and
        last timestamp, the algorithms its rounds ran in the order they first
        ran, and the rounds of each (rows written before the Strategy column
        existed count as algo1). Cheap (one aggregate query), however large
        the store."""
        query = (
            "SELECT session_id, COALESCE(json_extract(payload_json, '$.Strategy'), 'algo1') AS strategy, "
            "COUNT(*), MIN(json_extract(payload_json, '$.Timestamp')), "
            "MAX(json_extract(payload_json, '$.Timestamp')) "
            "FROM rounds GROUP BY session_id, strategy ORDER BY MIN(id)"
        )
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        sessions: Dict[str, Dict[str, Any]] = {}
        for session_id, strategy, count, first, last in rows:
            entry = sessions.setdefault(str(session_id), {
                "session_id": str(session_id), "rounds": 0, "first_timestamp": None,
                "last_timestamp": None, "algorithms": [], "algorithm_rounds": {},
            })
            entry["rounds"] += int(count)
            if first is not None and (entry["first_timestamp"] is None or first < entry["first_timestamp"]):
                entry["first_timestamp"] = first
            if last is not None and (entry["last_timestamp"] is None or last > entry["last_timestamp"]):
                entry["last_timestamp"] = last
            entry["algorithms"].append(str(strategy))
            entry["algorithm_rounds"][str(strategy)] = int(count)
        return list(sessions.values())

    def export_bounded_csv(self) -> bool:
        """Write the newest ``csv_export_max_rows`` rows (oldest first) to
        the CSV, streaming one row at a time. Building a DataFrame of the
        whole export first cost tens of MB of transient objects per call on
        a long-lived store, which is what a worker does every minute."""
        if self.csv_export_path is None:
            return False
        self.csv_export_path.parent.mkdir(parents=True, exist_ok=True)
        query = (
            "SELECT payload_json FROM ("
            "SELECT id, payload_json FROM rounds ORDER BY id DESC LIMIT ?"
            ") ORDER BY id"
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.csv_export_path.name}.",
            suffix=".tmp",
            dir=self.csv_export_path.parent,
        )
        if hasattr(os, "fchmod"):
            # mkstemp creates 0600; the export is read by other users (the
            # dashboard, and the worker when the host agent re-exports as root)
            os.fchmod(fd, 0o644)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(self.columns)
                with self._connect() as connection:
                    for (payload,) in connection.execute(query, (self.csv_export_max_rows,)):
                        document = json.loads(payload)
                        writer.writerow(
                            [self._csv_cell(document.get(column)) for column in self.columns]
                        )
            os.replace(temporary, self.csv_export_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True

    @staticmethod
    def _csv_cell(value):
        """The cell text pandas' ``to_csv`` would have produced: empty for
        null, ``True``/``False`` for booleans, the shortest float repr."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, float):
            return repr(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        return value

    def compact(self, retain_rows: int) -> int:
        retain_rows = max(0, int(retain_rows))
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0])
            remove = max(0, total - retain_rows)
            if remove:
                connection.execute(
                    "DELETE FROM rounds WHERE id IN (SELECT id FROM rounds ORDER BY id LIMIT ?)",
                    (remove,),
                )
        return remove

    def delete_session(self, session_id: str) -> int:
        """Permanently remove one session's round history. Returns the row
        count removed. Caller is responsible for re-exporting the CSV
        (export_bounded_csv) afterwards -- not done here so a caller
        deleting several sessions in a row only re-exports once."""
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM rounds WHERE session_id=?", (str(session_id),)
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM rounds WHERE session_id=?", (str(session_id),)
            )
        return int(count)

    def import_legacy_csv_once(self, csv_path) -> int:
        source = Path(csv_path)
        if not source.exists() or source.stat().st_size == 0:
            return 0
        fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
        metadata_key = f"legacy_csv:{source.resolve()}"
        with self._connect() as connection:
            record = connection.execute(
                "SELECT value FROM metadata WHERE key=?", (metadata_key,)
            ).fetchone()
        if record and record[0] == fingerprint:
            return 0
        frame = pd.read_csv(source).astype(object)
        imported = 0
        for column in self.columns:
            if column not in frame.columns:
                frame[column] = None
        for document in frame[self.columns].to_dict(orient="records"):
            self.append_round(document)
            imported += 1
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (metadata_key, fingerprint),
            )
        return imported
