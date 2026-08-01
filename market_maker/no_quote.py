"""Restart-safe operational monitoring for consecutive no-quote rounds."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class NoQuoteSnapshot:
    session_id: str
    symbol: str
    consecutive_count: int = 0
    episode_started_at: Optional[str] = None
    latest_at: Optional[str] = None
    reason: Optional[str] = None
    diagnostics: Dict[str, Any] = None
    alert_state: str = "idle"
    last_alert_at: Optional[str] = None
    last_alert_reason: Optional[str] = None
    last_message_id: Optional[int] = None

    @property
    def episode_seconds(self) -> int:
        if not self.episode_started_at or not self.latest_at:
            return 0
        start = datetime.fromisoformat(self.episode_started_at)
        latest = datetime.fromisoformat(self.latest_at)
        return max(0, int((latest - start).total_seconds()))


@dataclass(frozen=True)
class NoQuoteDecision:
    kind: Optional[str]
    snapshot: NoQuoteSnapshot
    payload: Dict[str, Any]
    pending: bool = False


class NoQuoteMonitor:
    def __init__(
        self,
        store,
        session_id: str,
        symbol: str,
        *,
        alert_rounds: int,
        alert_seconds: float,
        cooldown_seconds: float,
    ):
        self.db_path = store.db_path
        self.session_id = str(session_id)
        self.symbol = symbol.upper()
        self.alert_rounds = max(1, int(alert_rounds))
        self.alert_seconds = max(0.0, float(alert_seconds))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS no_quote_state (
                    session_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, symbol)
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO no_quote_state(session_id, symbol, state_json)
                VALUES (?, ?, ?)
                """,
                (self.session_id, self.symbol, json.dumps(self._default_state())),
            )

    def _default_state(self) -> dict:
        return {
            "consecutive_count": 0,
            "episode_started_at": None,
            "latest_at": None,
            "reason": None,
            "diagnostics": {},
            "alerted": False,
            "last_alert_at": None,
            "last_alert_reason": None,
            "last_message_id": None,
            "delivery_suppressed": False,
            "pending_kind": None,
            "pending_payload": {},
            "last_round": 0,
        }

    def _load(self) -> dict:
        with self._connect() as connection:
            record = connection.execute(
                "SELECT state_json FROM no_quote_state WHERE session_id=? AND symbol=?",
                (self.session_id, self.symbol),
            ).fetchone()
        state = self._default_state()
        if record:
            state.update(json.loads(record[0]))
        return state

    def _save(self, state: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO no_quote_state(session_id, symbol, state_json)
                VALUES (?, ?, ?)
                """,
                (
                    self.session_id,
                    self.symbol,
                    json.dumps(state, sort_keys=True, separators=(",", ":")),
                ),
            )

    def _snapshot(self, state: dict) -> NoQuoteSnapshot:
        if state.get("pending_kind"):
            alert_state = f"pending_{state['pending_kind']}"
        elif state.get("alerted") and state.get("delivery_suppressed"):
            alert_state = "suppressed"
        elif state.get("alerted"):
            alert_state = "alerted"
        elif state.get("consecutive_count", 0):
            alert_state = "tracking"
        else:
            alert_state = "idle"
        return NoQuoteSnapshot(
            session_id=self.session_id,
            symbol=self.symbol,
            consecutive_count=int(state.get("consecutive_count", 0)),
            episode_started_at=state.get("episode_started_at"),
            latest_at=state.get("latest_at"),
            reason=state.get("reason"),
            diagnostics=state.get("diagnostics") or {},
            alert_state=alert_state,
            last_alert_at=state.get("last_alert_at"),
            last_alert_reason=state.get("last_alert_reason"),
            last_message_id=state.get("last_message_id"),
        )

    def snapshot(self) -> NoQuoteSnapshot:
        return self._snapshot(self._load())

    @staticmethod
    def _iso(now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("no-quote timestamps must be timezone-aware")
        return now.isoformat()

    def _decision(self, state: dict, kind: Optional[str]) -> NoQuoteDecision:
        return NoQuoteDecision(
            kind=kind,
            snapshot=self._snapshot(state),
            payload=dict(state.get("pending_payload") or {}),
            pending=bool(kind),
        )

    def record_no_quote(
        self,
        round_no: int,
        now: datetime,
        reason: str,
        diagnostics: Dict[str, Any],
    ) -> NoQuoteDecision:
        state = self._load()
        now_iso = self._iso(now)
        if state["consecutive_count"] == 0:
            state["episode_started_at"] = now_iso
        state["consecutive_count"] += 1
        state["latest_at"] = now_iso
        state["reason"] = str(reason)
        state["diagnostics"] = diagnostics
        state["last_round"] = int(round_no)

        if state.get("pending_kind"):
            self._save(state)
            return self._decision(state, state["pending_kind"])

        snapshot = self._snapshot(state)
        threshold_reached = (
            snapshot.consecutive_count >= self.alert_rounds
            or snapshot.episode_seconds >= self.alert_seconds
        )
        kind = None
        if state.get("alerted") and state.get("last_alert_reason") != reason:
            kind = "state_change"
        elif not state.get("alerted") and threshold_reached:
            kind = "alert"
        elif state.get("alerted") and state.get("last_alert_at"):
            elapsed = (now - datetime.fromisoformat(state["last_alert_at"])).total_seconds()
            if elapsed >= self.cooldown_seconds:
                kind = "reminder"
        if kind:
            state["pending_kind"] = kind
            state["pending_payload"] = {
                "kind": kind,
                "round": int(round_no),
                "count": snapshot.consecutive_count,
                "episode_seconds": snapshot.episode_seconds,
                "reason": reason,
                "diagnostics": diagnostics,
            }
        self._save(state)
        return self._decision(state, kind)

    def record_quote(self, round_no: int, now: datetime) -> NoQuoteDecision:
        state = self._load()
        if state["consecutive_count"] == 0:
            return self._decision(state, None)
        snapshot = self._snapshot(state)
        should_recover = bool(state.get("alerted"))
        recovery_payload = {
            "kind": "recovered",
            "round": int(round_no),
            "episode_count": snapshot.consecutive_count,
            "episode_seconds": max(
                0,
                int((now - datetime.fromisoformat(state["episode_started_at"])).total_seconds()),
            ),
            "reason": state.get("reason"),
        }
        last_message_id = state.get("last_message_id")
        state.update(self._default_state())
        state["last_round"] = int(round_no)
        state["last_message_id"] = last_message_id
        if should_recover:
            state["pending_kind"] = "recovered"
            state["pending_payload"] = recovery_payload
        self._save(state)
        return self._decision(state, "recovered" if should_recover else None)

    def pending_decision(self) -> NoQuoteDecision:
        state = self._load()
        return self._decision(state, state.get("pending_kind"))

    def mark_delivered(self, decision: NoQuoteDecision, message_id: int) -> None:
        state = self._load()
        if not decision.kind or state.get("pending_kind") != decision.kind:
            return
        if decision.kind == "recovered":
            state["alerted"] = False
        else:
            state["alerted"] = True
            state["last_alert_at"] = state.get("latest_at")
            state["last_alert_reason"] = state.get("reason")
        state["delivery_suppressed"] = False
        state["last_message_id"] = int(message_id)
        state["pending_kind"] = None
        state["pending_payload"] = {}
        self._save(state)

    def suppress_pending(self) -> None:
        """Clear delivery work when Telegram is intentionally disabled."""
        state = self._load()
        if state.get("pending_kind") == "recovered":
            state["alerted"] = False
            state["delivery_suppressed"] = False
        elif state.get("pending_kind"):
            state["alerted"] = True
            state["last_alert_at"] = state.get("latest_at")
            state["last_alert_reason"] = state.get("reason")
            state["delivery_suppressed"] = True
        state["pending_kind"] = None
        state["pending_payload"] = {}
        self._save(state)
