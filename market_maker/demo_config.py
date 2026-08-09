"""Pair-aware Demo-mode configuration overlays."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple, Union

PathLike = Union[str, os.PathLike]


class OverlayValidationError(ValueError):
    pass


class OverlayLoadError(ValueError):
    pass


class ApplicationMode(str, Enum):
    HOT = "hot"
    RESTART = "restart"
    NEW_SESSION = "new_session"


@dataclass(frozen=True)
class FieldDefinition:
    value_type: type
    minimum: Optional[float]
    maximum: Optional[float]
    mode: ApplicationMode
    help_text: str


@dataclass(frozen=True)
class RuntimeReloadResult:
    ok: bool
    error: Optional[str] = None
    changed_fields: Tuple[str, ...] = ()
    snapshot: Optional[Dict[str, Any]] = None


FIELD_DEFINITIONS: Mapping[str, FieldDefinition] = MappingProxyType({
    "order_size": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Base-asset amount per order round."),
    "inv_max": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Maximum inventory the worker will hold."),
    "inv_min": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Minimum inventory the worker will hold."),
    "cancel_after_minutes": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Age before resting paper orders are cancelled."),
    "depth_max": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Maximum order-book depth used by the strategy."),
    "time_step": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Seconds between quoting rounds."),
    "demo_inv_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting inventory for a new Demo session."),
    "cash_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting quote cash for a new Demo session."),
    "commission_perc": FieldDefinition(float, 0, 1, ApplicationMode.NEW_SESSION, "Simulated fee fraction for a new Demo session."),
})

ALLOWED_FIELDS = frozenset(FIELD_DEFINITIONS)

# Old overlays may contain these fields from prior Demo releases. They are
# discarded only while loading existing files; new writes may never add them.
LEGACY_REMOVED_FIELDS = frozenset({
    "max_rounds",
    "demo_level1_fast_fill_enabled",
    "demo_level1_fill_delay_seconds",
    "demo_level1_fill_fraction",
    "demo_queue_enabled",
    "no_quote_alert_rounds",
    "no_quote_alert_seconds",
    "no_quote_alert_cooldown_seconds",
    "no_quote_recovery_alert",
    "algo1_diagnostics_enabled",
})

FORBIDDEN_FIELDS = frozenset({
    "pair", "currency_1", "currency_2", "algorithm_id", "token",
    "url_send_order", "url_wallet_balance", "session", "mode",
})


def editable_schema(pair: str, profiles: Mapping[str, Mapping[str, Any]]) -> Dict[str, FieldDefinition]:
    """Return the editable field definitions supported by ``pair``'s profile."""
    profile = profiles[pair]
    return {key: definition for key, definition in FIELD_DEFINITIONS.items() if key in profile}


def _schema_for(
    pair: Optional[str], profiles: Optional[Mapping[str, Mapping[str, Any]]]
) -> Mapping[str, FieldDefinition]:
    if pair is None and profiles is None:
        return FIELD_DEFINITIONS
    if pair is None or profiles is None:
        raise OverlayValidationError("pair and profiles must be provided together")
    return editable_schema(pair, profiles)


def _validate_desired_values(values: Mapping[str, Any]) -> None:
    if {"inv_min", "inv_max"} <= values.keys() and values["inv_min"] >= values["inv_max"]:
        raise OverlayValidationError("inv_min must be < inv_max")
    if "order_size" in values and values["order_size"] <= 0:
        raise OverlayValidationError("order_size must be > 0")


def _validate_field(key: str, raw_value: Any, definition: FieldDefinition) -> Any:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise OverlayValidationError(f"{key} must be numeric")
    if not math.isfinite(raw_value):
        raise OverlayValidationError(f"{key} must be finite")
    if definition.value_type is int:
        if not isinstance(raw_value, int):
            raise OverlayValidationError(f"{key} must be an integer")
        value = raw_value
    else:
        value = float(raw_value)
    if definition.minimum is not None and value < definition.minimum:
        raise OverlayValidationError(f"{key} must be >= {definition.minimum}")
    if definition.maximum is not None and value > definition.maximum:
        raise OverlayValidationError(f"{key} must be <= {definition.maximum}")
    return value


def validate_overrides(
    overrides: Dict[str, Any], *, pair: Optional[str] = None,
    profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Validate and type-coerce a new overlay payload without mutating it."""
    if not isinstance(overrides, dict):
        raise OverlayValidationError("overrides must be a dict")

    schema = _schema_for(pair, profiles)
    validated: Dict[str, Any] = {}
    for key, raw_value in overrides.items():
        if key in FORBIDDEN_FIELDS:
            raise OverlayValidationError(f"field is not configurable: {key}")
        if key not in schema:
            raise OverlayValidationError(f"unknown field: {key}")

        validated[key] = _validate_field(key, raw_value, schema[key])
    _validate_desired_values(validated)
    return validated


def overlay_path(state_dir: PathLike, pair: str) -> Path:
    return Path(state_dir) / f"demo_config_{pair}.json"


def runtime_snapshot_path(state_dir: PathLike, pair: str) -> Path:
    return Path(state_dir) / f"runtime_config_{pair}.json"


def _load_document(path: PathLike) -> Dict[str, Any]:
    overlay_file = Path(path)
    if not overlay_file.exists():
        return {}
    try:
        document = json.loads(overlay_file.read_text())
    except json.JSONDecodeError as error:
        raise OverlayLoadError(f"invalid JSON in overlay {overlay_file}: {error.msg}") from error
    except OSError as error:
        raise OverlayLoadError(f"unable to read overlay {overlay_file}: {error}") from error
    if not isinstance(document, dict):
        raise OverlayLoadError(f"overlay must contain a JSON object: {overlay_file}")
    return {key: value for key, value in document.items() if key not in LEGACY_REMOVED_FIELDS}


def load_overrides(
    path: PathLike, *, pair: Optional[str] = None,
    profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Read a validated overlay, dropping only recognized legacy keys."""
    return validate_overrides(_load_document(path), pair=pair, profiles=profiles)


def load_desired_config(
    pair: str, state_dir: PathLike, profiles: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    """Resolve a pair's editable profile defaults with its validated overlay."""
    schema = editable_schema(pair, profiles)
    desired = {key: profiles[pair][key] for key in schema}
    desired.update(load_overrides(overlay_path(state_dir, pair), pair=pair, profiles=profiles))
    desired = validate_overrides(desired, pair=pair, profiles=profiles)
    _validate_desired_values(desired)
    return desired


def desired_digest(values: Mapping[str, Any]) -> str:
    """Return a stable digest for a resolved desired configuration."""
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


CONFIG_STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    "applied": "Applied now",
    "waiting": "Saved; waiting for running worker",
    "pending_new_session": "Saved; applies next session",
    "stopped": "Worker stopped; applies when started",
    "stale": "Runtime status unknown/stale",
    "failed": "Failed to apply",
})


def _snapshot_is_current_shape(
    pair: str, snapshot: Any, current_session_id: Optional[str],
) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    if snapshot.get("schema_version") != 1 or snapshot.get("pair") != pair:
        return False
    if snapshot.get("status") not in {"applied", "error"}:
        return False
    if not isinstance(snapshot.get("applied"), Mapping):
        return False
    if not isinstance(snapshot.get("pending_new_session"), Mapping):
        return False
    if not isinstance(snapshot.get("desired_digest"), str):
        return False
    if not isinstance(snapshot.get("session_id"), str):
        return False
    if (
        current_session_id is not None
        and snapshot.get("session_id") != str(current_session_id)
    ):
        return False
    updated_at = snapshot.get("updated_at")
    if not isinstance(updated_at, str):
        return False
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    error = snapshot.get("error")
    if snapshot["status"] == "applied":
        return error is None
    return isinstance(error, str) and bool(error.strip())


def config_status(
    pair: str,
    desired: Mapping[str, Any],
    snapshot: Optional[Mapping[str, Any]],
    worker_running: bool,
    *,
    current_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe desired versus worker-owned runtime state without Flask I/O.

    The result is JSON-safe and intentionally contains both machine states and
    operator labels so Admin and Dashboard can render the same truth. A
    missing, old-schema, malformed, or wrong-pair snapshot is never used as
    evidence that desired values are applied.
    """
    desired_values = dict(desired)
    base_fields = {
        field: {
            "desired": value,
            "applied": None,
            "mode": FIELD_DEFINITIONS[field].mode.value,
            "state": "stale",
            "label": CONFIG_STATUS_LABELS["stale"],
        }
        for field, value in desired_values.items()
        if field in FIELD_DEFINITIONS
    }
    desired_config_digest = desired_digest(desired_values)

    if not _snapshot_is_current_shape(pair, snapshot, current_session_id):
        return {
            "pair": pair,
            "state": "stale",
            "label": CONFIG_STATUS_LABELS["stale"],
            "worker_running": bool(worker_running),
            "session_id": None,
            "desired_digest": desired_config_digest,
            "snapshot_digest": None,
            "applied": {},
            "pending_new_session": {},
            "error": None,
            "fields": base_fields,
        }

    assert snapshot is not None  # narrowed by _snapshot_is_current_shape
    applied = dict(snapshot["applied"])
    pending = dict(snapshot["pending_new_session"])
    snapshot_digest = snapshot["desired_digest"]
    digest_matches = snapshot_digest == desired_config_digest

    if not set(desired_values) <= set(applied):
        return config_status(
            pair, desired_values, None, worker_running,
            current_session_id=current_session_id,
        )

    effective_pending = {
        field: desired_values[field]
        for field in desired_values
        if field in FIELD_DEFINITIONS
        and FIELD_DEFINITIONS[field].mode is ApplicationMode.NEW_SESSION
        and desired_values[field] != applied[field]
    }

    if snapshot["status"] == "error":
        failed_fields = {
            field: dict(row, applied=applied.get(field), state="failed",
                        label=CONFIG_STATUS_LABELS["failed"])
            for field, row in base_fields.items()
        }
        return {
            "pair": pair,
            "state": "failed",
            "label": CONFIG_STATUS_LABELS["failed"],
            "worker_running": bool(worker_running),
            "session_id": snapshot["session_id"],
            "desired_digest": desired_config_digest,
            "snapshot_digest": snapshot_digest,
            "applied": applied,
            "pending_new_session": effective_pending,
            "error": snapshot.get("error"),
            "fields": failed_fields,
        }

    if digest_matches and snapshot["status"] == "applied":
        hot_mismatches = {
            field for field in desired_values
            if field in FIELD_DEFINITIONS
            and FIELD_DEFINITIONS[field].mode is ApplicationMode.HOT
            and desired_values[field] != applied[field]
        }
        if pending != effective_pending or hot_mismatches:
            return config_status(
                pair, desired_values, None, worker_running,
                current_session_id=current_session_id,
            )

    fields: Dict[str, Dict[str, Any]] = {}
    for field, row in base_fields.items():
        applied_value = applied[field]
        definition = FIELD_DEFINITIONS[field]
        if not worker_running:
            state = (
                "pending_new_session"
                if definition.mode is ApplicationMode.NEW_SESSION
                and applied_value != row["desired"]
                else "stopped"
            )
        elif definition.mode is ApplicationMode.NEW_SESSION:
            if applied_value != row["desired"]:
                state = "pending_new_session"
            else:
                state = "applied" if digest_matches else "waiting"
        elif not digest_matches:
            state = "waiting"
        elif applied_value == row["desired"]:
            state = "applied"
        else:
            state = "waiting"
        fields[field] = dict(
            row,
            applied=applied_value,
            state=state,
            label=CONFIG_STATUS_LABELS[state],
        )

    field_states = {row["state"] for row in fields.values()}
    if not worker_running:
        overall_state = "stopped"
    elif "waiting" in field_states:
        overall_state = "waiting"
    elif "pending_new_session" in field_states:
        overall_state = "pending_new_session"
    else:
        overall_state = "applied"
    return {
        "pair": pair,
        "state": overall_state,
        "label": CONFIG_STATUS_LABELS[overall_state],
        "worker_running": bool(worker_running),
        "session_id": snapshot["session_id"],
        "desired_digest": desired_config_digest,
        "snapshot_digest": snapshot_digest,
        "applied": applied,
        "pending_new_session": effective_pending,
        "error": snapshot.get("error"),
        "fields": fields,
    }


def _atomic_write(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w") as tmp_file:
        json.dump(document, tmp_file, sort_keys=True, indent=2, allow_nan=False)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, path)


def read_runtime_snapshot(state_dir: PathLike, pair: str) -> Optional[Dict[str, Any]]:
    """Read a worker-owned runtime snapshot, or ``None`` when absent."""
    path = runtime_snapshot_path(state_dir, pair)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise OverlayLoadError(f"invalid JSON in runtime snapshot {path}: {error.msg}") from error
    except OSError as error:
        raise OverlayLoadError(f"unable to read runtime snapshot {path}: {error}") from error
    if not isinstance(document, dict):
        raise OverlayLoadError(f"runtime snapshot must contain a JSON object: {path}")
    return document


def write_runtime_snapshot(
    state_dir: PathLike, pair: str, document: Dict[str, Any]
) -> Dict[str, Any]:
    """Atomically publish a worker-owned runtime snapshot."""
    if not isinstance(document, dict):
        raise OverlayValidationError("runtime snapshot must be a dict")
    if document.get("pair") != pair:
        raise OverlayValidationError("runtime snapshot pair does not match target pair")
    _atomic_write(runtime_snapshot_path(state_dir, pair), document)
    return document


def write_overrides(
    path: PathLike, overrides: Dict[str, Any], *, pair: str,
    profiles: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate and atomically persist a pair-aware overlay for a new write."""
    validated = validate_overrides(overrides, pair=pair, profiles=profiles)
    desired = {key: profiles[pair][key] for key in editable_schema(pair, profiles)}
    desired.update(validated)
    validate_overrides(desired, pair=pair, profiles=profiles)
    _validate_desired_values(desired)

    overlay_file = Path(path)
    previous_file = overlay_file.with_name(overlay_file.name + ".previous")
    if overlay_file.exists():
        _atomic_write(previous_file, load_overrides(overlay_file, pair=pair, profiles=profiles))
    _atomic_write(overlay_file, validated)
    return validated


def has_previous(path: PathLike) -> bool:
    overlay_file = Path(path)
    return overlay_file.with_name(overlay_file.name + ".previous").exists()


def rollback_overrides(path: PathLike) -> Dict[str, Any]:
    """Restore the overlay from its `.previous` slot."""
    overlay_file = Path(path)
    previous_file = overlay_file.with_name(overlay_file.name + ".previous")
    if not previous_file.exists():
        raise OverlayValidationError(f"no previous overlay to restore: {path}")
    validated = load_overrides(previous_file)
    _atomic_write(overlay_file, validated)
    return validated


def apply_overrides(cfg: Any, overrides: Dict[str, Any]) -> Any:
    """Apply a validated overlay dict onto an MMConfig instance in place."""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class RuntimeConfigSynchronizer:
    """Apply validated hot fields and publish the worker's runtime truth."""

    SNAPSHOT_SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        cfg: Any,
        ledger: Any,
        pair: str,
        state_dir: PathLike,
        profiles: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.pair = pair
        self.state_dir = Path(state_dir)
        self.profiles = profiles
        self._desired_digest: Optional[str] = None
        self._session_values = {
            "demo_inv_initial": float(ledger.inventory),
            "cash_initial": float(ledger.cash),
            "commission_perc": float(ledger.commission_perc),
        }

    def _applied_values(self, desired: Mapping[str, Any]) -> Dict[str, Any]:
        schema = editable_schema(self.pair, self.profiles)
        applied: Dict[str, Any] = {}
        for field, definition in schema.items():
            if definition.mode is ApplicationMode.NEW_SESSION:
                applied[field] = self._session_values[field]
            else:
                applied[field] = getattr(self.cfg, field)
        return applied

    def _snapshot(
        self,
        desired: Mapping[str, Any],
        *,
        digest: str,
        status: str,
        error: Optional[str],
    ) -> Dict[str, Any]:
        schema = editable_schema(self.pair, self.profiles)
        applied = self._applied_values(desired)
        pending = {
            field: desired[field]
            for field, definition in schema.items()
            if definition.mode is ApplicationMode.NEW_SESSION
            and desired[field] != applied[field]
        }
        return {
            "schema_version": self.SNAPSHOT_SCHEMA_VERSION,
            "pair": self.pair,
            "session_id": str(self.cfg.session),
            "applied": applied,
            "pending_new_session": pending,
            "desired_digest": digest,
            "status": status,
            "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _publish_error(self, error: Exception) -> RuntimeReloadResult:
        self._desired_digest = None
        message = f"{type(error).__name__}: {error}"
        try:
            previous = read_runtime_snapshot(self.state_dir, self.pair)
        except OverlayLoadError:
            previous = None
        if previous is None or previous.get("session_id") != str(self.cfg.session):
            desired = {
                field: getattr(self.cfg, field)
                for field in editable_schema(self.pair, self.profiles)
            }
            snapshot = self._snapshot(
                desired,
                digest=self._desired_digest or desired_digest(desired),
                status="error",
                error=message,
            )
        else:
            snapshot = dict(previous)
            snapshot.update({
                "status": "error",
                "error": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        try:
            write_runtime_snapshot(self.state_dir, self.pair, snapshot)
        except (OSError, OverlayValidationError) as write_error:
            message = f"{message}; snapshot publish failed: {write_error}"
        return RuntimeReloadResult(ok=False, error=message, snapshot=snapshot)

    def reload_at_round_boundary(self) -> RuntimeReloadResult:
        try:
            desired = load_desired_config(self.pair, self.state_dir, self.profiles)
            digest = desired_digest(desired)
        except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as error:
            return self._publish_error(error)

        if digest == self._desired_digest:
            try:
                current_snapshot = read_runtime_snapshot(self.state_dir, self.pair)
            except OverlayLoadError:
                current_snapshot = None
            if current_snapshot is not None and current_snapshot.get("status") == "applied":
                return RuntimeReloadResult(ok=True, snapshot=current_snapshot)

        schema = editable_schema(self.pair, self.profiles)
        hot_values = {
            field: desired[field]
            for field, definition in schema.items()
            if definition.mode is ApplicationMode.HOT
        }
        changed_fields = tuple(
            field for field, value in hot_values.items()
            if getattr(self.cfg, field) != value
        )
        previous_hot_values = {
            field: getattr(self.cfg, field) for field in hot_values
        }
        for field, value in hot_values.items():
            setattr(self.cfg, field, value)

        snapshot = self._snapshot(
            desired,
            digest=digest,
            status="applied",
            error=None,
        )
        try:
            write_runtime_snapshot(self.state_dir, self.pair, snapshot)
        except (OSError, OverlayValidationError) as error:
            for field, value in previous_hot_values.items():
                setattr(self.cfg, field, value)
            return self._publish_error(error)
        self._desired_digest = digest
        return RuntimeReloadResult(
            ok=True,
            changed_fields=changed_fields,
            snapshot=snapshot,
        )
