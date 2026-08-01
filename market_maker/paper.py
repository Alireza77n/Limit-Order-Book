import fcntl
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


EPSILON = 1e-12


@dataclass
class PaperOrder:
    order_id: str
    client_order_id: str
    session_id: str
    symbol: str
    side: str
    limit_price: float
    selected_level: int
    original_amount: float
    matched_amount: float
    processed_matched_amount: float
    remaining_amount: float
    status: str
    created_at: datetime
    updated_at: datetime
    queue_ahead: float
    fill_reason: Optional[str]
    average_fill_price: float
    total_fee: float
    telegram_submission_message_id: Optional[int]
    telegram_last_reported_matched: float
    fill_notional: float = 0.0
    telegram_submission_attempted: bool = False
    telegram_terminal_reported: bool = False
    round_no: int = 0
    telegram_pending_event: Optional[dict] = None
    telegram_round_message_id: Optional[int] = None
    cancellation_context: Optional[dict] = None


@dataclass(frozen=True)
class PaperEvent:
    order_id: str
    status: str
    reason: str
    new_fill: float = 0.0
    cumulative_matched: float = 0.0
    remaining: float = 0.0
    fill_price: float = 0.0
    fee: float = 0.0
    fee_quote: float = 0.0
    inventory_after: float = 0.0
    cash_after: float = 0.0
    wealth_after: float = 0.0
    cancelled_remainder: float = 0.0
    released_locked_amount: float = 0.0
    released_locked_kind: Optional[str] = None
    released_locked_unit: Optional[str] = None
    initiator: Optional[str] = None
    call_site: Optional[str] = None
    session_id: Optional[str] = None
    symbol: Optional[str] = None
    process_id: int = 0
    timestamp: Optional[str] = None


@dataclass(frozen=True)
class PaperSnapshot:
    session_id: str
    symbol: str
    inventory: float
    cash: float
    locked_cash: float
    locked_inventory: float
    available_cash: float
    available_inventory: float
    fees_quote: float
    valuation_price: float
    wealth: float


@dataclass(frozen=True)
class FeeScopes:
    session_pair: float
    session_all_symbols: float
    pair_all_time: float
    all_symbols_all_time: float


class PaperLedger:
    def __init__(
        self,
        *,
        session_id: str,
        symbol: str,
        initial_inventory: float,
        initial_cash: float,
        commission_perc: float,
        valuation_price: float,
        level1_fast_fill_enabled: bool = True,
        level1_fill_delay_seconds: float = 0.0,
        level1_fill_fraction: float = 1.0,
        queue_enabled: bool = False,
        state_path=None,
        fee_state_path=None,
        archive_path=None,
        hot_terminal_order_limit: int = 1_000,
        archive_max_bytes: int = 10_000_000,
    ) -> None:
        if not 0 < level1_fill_fraction <= 1:
            raise ValueError("level1_fill_fraction must be in (0, 1]")
        self.session_id = str(session_id)
        self.symbol = symbol.upper()
        self.inventory = float(initial_inventory)
        self.cash = float(initial_cash)
        self.commission_perc = float(commission_perc)
        self.valuation_price = float(valuation_price)
        self.level1_fast_fill_enabled = bool(level1_fast_fill_enabled)
        self.level1_fill_delay_seconds = float(level1_fill_delay_seconds)
        self.level1_fill_fraction = float(level1_fill_fraction)
        self.queue_enabled = bool(queue_enabled)
        self.state_path = Path(state_path) if state_path is not None else None
        self.fee_state_path = Path(fee_state_path) if fee_state_path is not None else None
        if archive_path is not None:
            self.archive_path = Path(archive_path)
        elif self.state_path is not None:
            self.archive_path = self.state_path.with_name(
                f"{self.state_path.stem}-archive.jsonl"
            )
        else:
            self.archive_path = None
        self.hot_terminal_order_limit = max(0, int(hot_terminal_order_limit))
        self.archive_max_bytes = max(1, int(archive_max_bytes))
        self.fees_quote = 0.0
        self.active_orders: Dict[str, PaperOrder] = {}
        self.order_history: Dict[str, PaperOrder] = {}
        self._archived_order_ids = self._load_archived_order_ids()

    @staticmethod
    def _atomic_json_write(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("x") as temp_file:
                json.dump(document, temp_file, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _order_document(self, order: PaperOrder) -> dict:
        document = asdict(order)
        document["created_at"] = order.created_at.isoformat()
        document["updated_at"] = order.updated_at.isoformat()
        return document

    def _persist(self) -> None:
        if self.state_path is None:
            return
        document = {
            "version": 2,
            "config": {
                "session_id": self.session_id,
                "symbol": self.symbol,
                "commission_perc": self.commission_perc,
                "level1_fast_fill_enabled": self.level1_fast_fill_enabled,
                "level1_fill_delay_seconds": self.level1_fill_delay_seconds,
                "level1_fill_fraction": self.level1_fill_fraction,
                "queue_enabled": self.queue_enabled,
                "fee_state_path": str(self.fee_state_path) if self.fee_state_path else None,
                "archive_path": str(self.archive_path) if self.archive_path else None,
                "hot_terminal_order_limit": self.hot_terminal_order_limit,
                "archive_max_bytes": self.archive_max_bytes,
            },
            "account": {
                "inventory": self.inventory,
                "cash": self.cash,
                "valuation_price": self.valuation_price,
                "fees_quote": self.fees_quote,
            },
            "active_order_ids": sorted(self.active_orders),
            "orders": [self._order_document(order) for order in self.order_history.values()],
        }
        self._atomic_json_write(self.state_path, document)

    @classmethod
    def load(cls, state_path):
        path = Path(state_path)
        document = json.loads(path.read_text())
        config = document["config"]
        account = document["account"]
        ledger = cls(
            session_id=config["session_id"],
            symbol=config["symbol"],
            initial_inventory=account["inventory"],
            initial_cash=account["cash"],
            commission_perc=config["commission_perc"],
            valuation_price=account["valuation_price"],
            level1_fast_fill_enabled=config["level1_fast_fill_enabled"],
            level1_fill_delay_seconds=config["level1_fill_delay_seconds"],
            level1_fill_fraction=config["level1_fill_fraction"],
            queue_enabled=config["queue_enabled"],
            state_path=path,
            fee_state_path=config.get("fee_state_path"),
            archive_path=config.get("archive_path"),
            hot_terminal_order_limit=config.get("hot_terminal_order_limit", 1_000),
            archive_max_bytes=config.get("archive_max_bytes", 10_000_000),
        )
        ledger.fees_quote = float(account.get("fees_quote", 0.0))
        for order_document in document.get("orders", []):
            values = dict(order_document)
            values.setdefault("telegram_submission_attempted", False)
            values.setdefault("telegram_terminal_reported", False)
            values.setdefault("round_no", 0)
            values.setdefault("telegram_pending_event", None)
            values.setdefault("telegram_round_message_id", None)
            values.setdefault("cancellation_context", None)
            values["created_at"] = datetime.fromisoformat(values["created_at"])
            values["updated_at"] = datetime.fromisoformat(values["updated_at"])
            order = PaperOrder(**values)
            ledger.order_history[order.client_order_id] = order
        for order_id in document.get("active_order_ids", []):
            ledger.active_orders[order_id] = ledger.order_history[order_id]
        return ledger

    @staticmethod
    def _migrate_fee_document(document: dict) -> dict:
        applied_events = document.pop("applied_events", {})
        high_watermarks = document.setdefault("order_high_watermarks", {})
        for event_id, fee_quote in applied_events.items():
            try:
                order_key, cumulative_text = event_id.rsplit(":", 1)
                cumulative = float(cumulative_text)
            except (AttributeError, TypeError, ValueError):
                continue
            record = high_watermarks.setdefault(
                order_key, {"cumulative_amount": 0.0, "fee_quote": 0.0}
            )
            record["cumulative_amount"] = max(
                float(record.get("cumulative_amount", 0.0)), cumulative
            )
            record["fee_quote"] = float(record.get("fee_quote", 0.0)) + float(fee_quote)
        return document

    def _fee_document(self) -> dict:
        if self.fee_state_path is None:
            return self._migrate_fee_document({
                "sessions": {self.session_id: {self.symbol: self.fees_quote}},
                "all_time": {self.symbol: self.fees_quote},
            })
        if not self.fee_state_path.exists():
            return self._migrate_fee_document({"sessions": {}, "all_time": {}})
        try:
            return self._migrate_fee_document(json.loads(self.fee_state_path.read_text()))
        except (OSError, ValueError, TypeError):
            return self._migrate_fee_document({"sessions": {}, "all_time": {}})

    def _record_shared_fee(
        self,
        fee_quote: float,
        *,
        order_id: str,
        cumulative_amount: float,
    ) -> None:
        if self.fee_state_path is None:
            return
        self.fee_state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.fee_state_path.with_name(f".{self.fee_state_path.name}.lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            document = self._fee_document()
            order_key = f"{self.session_id}:{self.symbol}:{order_id}"
            high_watermarks = document.setdefault("order_high_watermarks", {})
            existing = high_watermarks.get(order_key, {})
            if cumulative_amount <= float(existing.get("cumulative_amount", 0.0)) + EPSILON:
                # Persist the normalized v2 document even when this call is a
                # duplicate of a migrated v1 event.
                self._atomic_json_write(self.fee_state_path, document)
                return
            session = document.setdefault("sessions", {}).setdefault(self.session_id, {})
            session[self.symbol] = float(session.get(self.symbol, 0.0)) + fee_quote
            all_time = document.setdefault("all_time", {})
            all_time[self.symbol] = float(all_time.get(self.symbol, 0.0)) + fee_quote
            high_watermarks[order_key] = {
                "cumulative_amount": float(cumulative_amount),
                "fee_quote": float(existing.get("fee_quote", 0.0)) + float(fee_quote),
            }
            self._atomic_json_write(self.fee_state_path, document)

    def _archive_candidates(self) -> List[PaperOrder]:
        terminal = [
            order for order in self.order_history.values()
            if order.status in {"filled", "cancelled", "rejected"}
            and order.client_order_id not in self.active_orders
            and order.telegram_pending_event is None
            and not (
                order.telegram_submission_attempted
                and order.telegram_submission_message_id is None
            )
        ]
        terminal.sort(key=lambda order: (order.updated_at, order.client_order_id))
        if len(terminal) <= self.hot_terminal_order_limit:
            return []
        return terminal[: len(terminal) - self.hot_terminal_order_limit]

    def _archive_files(self) -> List[Path]:
        if self.archive_path is None:
            return []
        pattern = f"{self.archive_path.stem}*{self.archive_path.suffix}"
        files = list(self.archive_path.parent.glob(pattern))

        def segment_number(path: Path) -> int:
            if path == self.archive_path:
                return 0
            suffix = path.stem.removeprefix(f"{self.archive_path.stem}.")
            try:
                return int(suffix) + 1
            except ValueError:
                return 0

        return sorted(files, key=segment_number)

    def _load_archived_order_ids(self) -> set:
        archived = set()
        for archive_file in self._archive_files():
            try:
                lines = archive_file.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if record.get("record_type") == "paper_order" and record.get("order_id"):
                    archived.add(str(record["order_id"]))
        return archived

    def _archive_segment(self, encoded_size: int) -> Path:
        files = self._archive_files()
        if not files:
            return self.archive_path
        latest = files[-1]
        if latest.stat().st_size + encoded_size <= self.archive_max_bytes:
            return latest
        index = len(files)
        return self.archive_path.with_name(
            f"{self.archive_path.stem}.{index:04d}{self.archive_path.suffix}"
        )

    def _append_archive_record(self, order: PaperOrder) -> bool:
        if self.archive_path is None or order.client_order_id in self._archived_order_ids:
            return False
        fee_record = self._fee_document().get("order_high_watermarks", {}).get(
            f"{self.session_id}:{self.symbol}:{order.client_order_id}"
        )
        record = {
            "record_type": "paper_order",
            "version": 1,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "order_id": order.client_order_id,
            "order": self._order_document(order),
            "fee_high_watermark": fee_record,
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        archive_file = self._archive_segment(len(encoded.encode("utf-8")))
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        with archive_file.open("a") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self._archived_order_ids.add(order.client_order_id)
        return True

    def _prune_fee_high_watermarks(self, order_ids) -> None:
        if self.fee_state_path is None or not order_ids:
            return
        lock_path = self.fee_state_path.with_name(f".{self.fee_state_path.name}.lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            document = self._fee_document()
            high_watermarks = document.setdefault("order_high_watermarks", {})
            for order_id in order_ids:
                high_watermarks.pop(
                    f"{self.session_id}:{self.symbol}:{order_id}", None
                )
            self._atomic_json_write(self.fee_state_path, document)

    def compact(self) -> int:
        candidates = self._archive_candidates()
        if not candidates:
            return 0
        for order in candidates:
            self._append_archive_record(order)
        removed_ids = [order.client_order_id for order in candidates]
        original_history = dict(self.order_history)
        try:
            for order_id in removed_ids:
                self.order_history.pop(order_id, None)
            self._persist()
        except Exception:
            self.order_history = original_history
            raise
        self._prune_fee_high_watermarks(removed_ids)
        return len(removed_ids)

    def fee_scopes(self) -> FeeScopes:
        document = self._fee_document()
        session = document.get("sessions", {}).get(self.session_id, {})
        all_time = document.get("all_time", {})
        return FeeScopes(
            session_pair=float(session.get(self.symbol, 0.0)),
            session_all_symbols=sum(float(value) for value in session.values()),
            pair_all_time=float(all_time.get(self.symbol, 0.0)),
            all_symbols_all_time=sum(float(value) for value in all_time.values()),
        )

    def snapshot(self, valuation_price: Optional[float] = None) -> PaperSnapshot:
        if valuation_price is not None:
            self.valuation_price = float(valuation_price)
        locked_cash = sum(
            order.remaining_amount * order.limit_price
            for order in self.active_orders.values()
            if order.side == "buy"
        )
        locked_inventory = sum(
            order.remaining_amount
            for order in self.active_orders.values()
            if order.side == "sell"
        )
        return PaperSnapshot(
            session_id=self.session_id,
            symbol=self.symbol,
            inventory=self.inventory,
            cash=self.cash,
            locked_cash=locked_cash,
            locked_inventory=locked_inventory,
            available_cash=max(0.0, self.cash - locked_cash),
            available_inventory=max(0.0, self.inventory - locked_inventory),
            fees_quote=self.fees_quote,
            valuation_price=self.valuation_price,
            wealth=self.inventory * self.valuation_price + self.cash,
        )

    def get_order(self, client_order_id: str) -> PaperOrder:
        try:
            return self.order_history[client_order_id]
        except KeyError as exc:
            raise KeyError(f"Unknown paper order: {client_order_id}") from exc

    def record_telegram_submission(
        self, client_order_id: str, *, message_id: Optional[int] = None
    ) -> None:
        order = self.get_order(client_order_id)
        order.telegram_submission_attempted = True
        if message_id is not None:
            order.telegram_submission_message_id = int(message_id)
        self._persist()

    def record_telegram_round_parent(
        self, client_order_id: str, *, message_id: int
    ) -> None:
        order = self.get_order(client_order_id)
        order.telegram_round_message_id = int(message_id)
        self._persist()

    def record_telegram_event(self, event: PaperEvent) -> None:
        order = self.get_order(event.order_id)
        if event.status in {"partially_filled", "filled"}:
            order.telegram_last_reported_matched = max(
                order.telegram_last_reported_matched,
                float(event.cumulative_matched),
            )
        if event.status == "cancelled":
            order.telegram_terminal_reported = True
        order.telegram_pending_event = None
        self._persist()

    def record_telegram_pending(self, event: PaperEvent) -> None:
        order = self.get_order(event.order_id)
        order.telegram_pending_event = asdict(event)
        self._persist()

    def submit_order(
        self,
        *,
        side: str,
        limit_price: float,
        amount: float,
        selected_level: int,
        client_order_id: str,
        now: datetime,
        queue_ahead: float = 0.0,
        round_no: int = 0,
    ) -> PaperOrder:
        if client_order_id in self.order_history:
            raise ValueError(f"Paper order already exists: {client_order_id}")
        side = side.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if amount <= 0 or limit_price <= 0:
            raise ValueError("amount and limit_price must be positive")

        snapshot = self.snapshot()
        rejected_reason = None
        if side == "buy" and amount * limit_price > snapshot.available_cash + EPSILON:
            rejected_reason = "insufficient_cash"
        if side == "sell" and amount > snapshot.available_inventory + EPSILON:
            rejected_reason = "insufficient_inventory"

        order = PaperOrder(
            order_id=client_order_id,
            client_order_id=client_order_id,
            session_id=self.session_id,
            symbol=self.symbol,
            side=side,
            limit_price=float(limit_price),
            selected_level=int(selected_level),
            original_amount=float(amount),
            matched_amount=0.0,
            processed_matched_amount=0.0,
            remaining_amount=float(amount),
            status="rejected" if rejected_reason else "open",
            created_at=now,
            updated_at=now,
            queue_ahead=max(0.0, float(queue_ahead)),
            fill_reason=rejected_reason,
            average_fill_price=0.0,
            total_fee=0.0,
            telegram_submission_message_id=None,
            telegram_last_reported_matched=0.0,
            round_no=int(round_no),
        )
        self.order_history[client_order_id] = order
        if not rejected_reason:
            self.active_orders[client_order_id] = order
        self._persist()
        return order

    def apply_cumulative_fill(
        self,
        client_order_id: str,
        *,
        cumulative_matched: float,
        fill_price: float,
        reason: str,
        now: datetime,
    ) -> Optional[PaperEvent]:
        order = self.get_order(client_order_id)
        cumulative = min(order.original_amount, max(0.0, float(cumulative_matched)))
        new_fill = cumulative - order.processed_matched_amount
        if new_fill <= EPSILON:
            return None
        if order.status not in {"open", "partially_filled"}:
            return None

        price = float(fill_price)
        fee = new_fill * self.commission_perc
        if order.side == "buy":
            self.cash -= new_fill * price
            self.inventory += new_fill - fee
            fee_quote = fee * price
        else:
            quote_fee = new_fill * price * self.commission_perc
            fee = quote_fee
            self.inventory -= new_fill
            self.cash += new_fill * price - quote_fee
            fee_quote = quote_fee
        self.fees_quote += fee_quote
        self._record_shared_fee(
            fee_quote,
            order_id=client_order_id,
            cumulative_amount=cumulative,
        )

        order.fill_notional += new_fill * price
        order.matched_amount = cumulative
        order.processed_matched_amount = cumulative
        order.remaining_amount = max(0.0, order.original_amount - cumulative)
        order.average_fill_price = order.fill_notional / cumulative
        order.total_fee += fee
        order.fill_reason = reason
        order.updated_at = now
        order.status = "filled" if order.remaining_amount <= EPSILON else "partially_filled"
        if order.status == "filled":
            order.remaining_amount = 0.0
            self.active_orders.pop(client_order_id, None)

        self._persist()

        snap = self.snapshot()
        return PaperEvent(
            order_id=client_order_id,
            status=order.status,
            reason=reason,
            new_fill=new_fill,
            cumulative_matched=cumulative,
            remaining=order.remaining_amount,
            fill_price=price,
            fee=fee,
            fee_quote=fee_quote,
            inventory_after=self.inventory,
            cash_after=self.cash,
            wealth_after=snap.wealth,
        )

    def process_market(
        self,
        *,
        best_bid: float,
        best_ask: float,
        last_price: float,
        now: datetime,
        trade_price: Optional[float] = None,
        traded_volume: Optional[float] = None,
    ) -> List[PaperEvent]:
        self.valuation_price = float(last_price)
        events: List[PaperEvent] = []
        for order_id in list(self.active_orders):
            order = self.active_orders.get(order_id)
            if order is None:
                continue
            elapsed = (now - order.created_at).total_seconds()
            if (
                self.level1_fast_fill_enabled
                and order.selected_level == 1
                and elapsed >= self.level1_fill_delay_seconds
            ):
                amount = min(order.remaining_amount, order.original_amount * self.level1_fill_fraction)
                event = self.apply_cumulative_fill(
                    order_id,
                    cumulative_matched=order.matched_amount + amount,
                    fill_price=order.limit_price,
                    reason="level1_fast_fill",
                    now=now,
                )
                if event:
                    events.append(event)
                continue

            trade_eligible = trade_price is not None and (
                (order.side == "buy" and trade_price <= order.limit_price)
                or (order.side == "sell" and trade_price >= order.limit_price)
            )
            touch_eligible = (
                (order.side == "buy" and best_ask <= order.limit_price)
                or (order.side == "sell" and best_bid >= order.limit_price)
            )
            if not trade_eligible and not touch_eligible:
                continue

            reason = "trade_cross" if trade_eligible else "price_touch"
            fill_amount = order.remaining_amount
            fill_price = float(trade_price) if trade_eligible else order.limit_price
            if self.queue_enabled and trade_eligible and traded_volume is not None:
                volume = max(0.0, float(traded_volume))
                consumed = min(order.queue_ahead, volume)
                order.queue_ahead -= consumed
                volume -= consumed
                if volume <= EPSILON:
                    self._persist()
                    continue
                fill_amount = min(order.remaining_amount, volume)
                reason = "queue_consumed"

            event = self.apply_cumulative_fill(
                order_id,
                cumulative_matched=order.matched_amount + fill_amount,
                fill_price=fill_price,
                reason=reason,
                now=now,
            )
            if event:
                events.append(event)
        return events

    @staticmethod
    def _symbol_currencies(symbol: str) -> tuple[str, str]:
        normalized = str(symbol).upper()
        for quote in ("USDT", "IRT", "USD", "EUR", "BTC", "ETH"):
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return normalized[:-len(quote)], quote
        return normalized, "QUOTE"

    def cancel_order(
        self,
        client_order_id: str,
        *,
        reason: str,
        now: datetime,
        initiator: str = "paper_ledger",
        call_site: str = "PaperLedger.cancel_order",
    ) -> PaperEvent:
        order = self.get_order(client_order_id)
        if order.status not in {"open", "partially_filled"}:
            raise ValueError(f"Order is not cancellable: {client_order_id}")
        remainder = order.remaining_amount
        released = remainder * order.limit_price if order.side == "buy" else remainder
        base_currency, quote_currency = self._symbol_currencies(order.symbol)
        released_kind = "cash" if order.side == "buy" else "inventory"
        released_unit = quote_currency if order.side == "buy" else base_currency
        process_id = os.getpid()
        cancellation_context = {
            "reason": str(reason),
            "initiator": str(initiator),
            "call_site": str(call_site),
            "session_id": self.session_id,
            "symbol": self.symbol,
            "process_id": process_id,
            "timestamp": now.isoformat(),
            "released_locked_amount": released,
            "released_locked_kind": released_kind,
            "released_locked_unit": released_unit,
        }
        order.status = "cancelled"
        order.fill_reason = reason
        order.cancellation_context = cancellation_context
        order.updated_at = now
        self.active_orders.pop(client_order_id, None)
        self._persist()
        snap = self.snapshot()
        event = PaperEvent(
            order_id=client_order_id,
            status="cancelled",
            reason=reason,
            cumulative_matched=order.matched_amount,
            remaining=remainder,
            inventory_after=self.inventory,
            cash_after=self.cash,
            wealth_after=snap.wealth,
            cancelled_remainder=remainder,
            released_locked_amount=released,
            released_locked_kind=released_kind,
            released_locked_unit=released_unit,
            initiator=str(initiator),
            call_site=str(call_site),
            session_id=self.session_id,
            symbol=self.symbol,
            process_id=process_id,
            timestamp=now.isoformat(),
        )
        print(json.dumps({
            "event": "paper_order_cancelled",
            "reason": event.reason,
            "initiator": event.initiator,
            "call_site": event.call_site,
            "session_id": event.session_id,
            "symbol": event.symbol,
            "process_id": event.process_id,
            "order_id": event.order_id,
            "released_locked_amount": event.released_locked_amount,
            "released_locked_kind": event.released_locked_kind,
            "released_locked_unit": event.released_locked_unit,
            "timestamp": event.timestamp,
        }, sort_keys=True), flush=True)
        return event

    def cancel_stale(self, *, max_age_seconds: float, now: datetime) -> List[PaperEvent]:
        events = []
        for order_id in list(self.active_orders):
            order = self.active_orders[order_id]
            if (now - order.created_at).total_seconds() > max_age_seconds:
                events.append(self.cancel_order(
                    order_id,
                    reason="stale_timeout",
                    now=now,
                    initiator="paper_ledger_stale_monitor",
                    call_site="PaperLedger.cancel_stale",
                ))
        return events
