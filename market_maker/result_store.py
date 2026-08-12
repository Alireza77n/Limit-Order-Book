"""Bounded, transactional persistence for Demo reporting rows."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

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
    ):
        self.db_path = Path(db_path)
        self.columns = list(columns)
        self.csv_export_path = Path(csv_export_path) if csv_export_path else None
        self.csv_export_max_rows = max(1, int(csv_export_max_rows))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @classmethod
    def open_existing(cls, db_path, csv_export_path=None, csv_export_max_rows=10_000):
        """Reopen a store an earlier process created, recovering its column
        schema from the db itself. csv_export_path is NOT stored in the db --
        pass it explicitly if this instance needs to call
        export_bounded_csv(); omitted, exports silently no-op (csv_export_path
        stays None), which is correct for read-only callers like dashboard.py."""
        path = Path(db_path)
        if not path.exists():
            raise FileNotFoundError(path)
        with sqlite3.connect(path, timeout=5.0) as connection:
            record = connection.execute(
                "SELECT value FROM metadata WHERE key='columns'"
            ).fetchone()
        if not record:
            raise ValueError(f"Demo result store has no column metadata: {path}")
        return cls(
            path, json.loads(record[0]),
            csv_export_path=csv_export_path, csv_export_max_rows=csv_export_max_rows,
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

    def load_view(self, limit: Optional[int] = None) -> pd.DataFrame:
        query = "SELECT payload_json FROM rounds ORDER BY id"
        parameters: tuple = ()
        if limit is not None:
            query = (
                "SELECT payload_json FROM ("
                "SELECT id, payload_json FROM rounds ORDER BY id DESC LIMIT ?"
                ") ORDER BY id"
            )
            parameters = (max(0, int(limit)),)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        documents = [json.loads(record[0]) for record in rows]
        return pd.DataFrame(documents, columns=self.columns, dtype=object)

    def export_bounded_csv(self) -> bool:
        if self.csv_export_path is None:
            return False
        self.csv_export_path.parent.mkdir(parents=True, exist_ok=True)
        view = self.load_view(limit=self.csv_export_max_rows)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.csv_export_path.name}.",
            suffix=".tmp",
            dir=self.csv_export_path.parent,
        )
        os.close(fd)
        try:
            view.to_csv(temporary, index=False)
            os.replace(temporary, self.csv_export_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True

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
