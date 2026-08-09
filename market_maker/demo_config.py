"""Pair-aware Demo-mode configuration overlays."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

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


FIELD_DEFINITIONS = {
    "order_size": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Base-asset amount per order round."),
    "inv_max": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Maximum inventory the worker will hold."),
    "inv_min": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Minimum inventory the worker will hold."),
    "cancel_after_minutes": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Age before resting paper orders are cancelled."),
    "depth_max": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Maximum order-book depth used by the strategy."),
    "time_step": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Seconds between quoting rounds."),
    "demo_inv_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting inventory for a new Demo session."),
    "cash_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting quote cash for a new Demo session."),
    "commission_perc": FieldDefinition(float, 0, 1, ApplicationMode.NEW_SESSION, "Simulated fee fraction for a new Demo session."),
}

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
    if {"inv_min", "inv_max"} <= values.keys() and values["inv_min"] > values["inv_max"]:
        raise OverlayValidationError("inv_min must be <= inv_max")
    if "order_size" in values and values["order_size"] <= 0:
        raise OverlayValidationError("order_size must be > 0")


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

        definition = schema[key]
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise OverlayValidationError(f"{key} must be numeric")
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
        validated[key] = value
    return validated


def overlay_path(state_dir: PathLike, pair: str) -> Path:
    return Path(state_dir) / f"demo_config_{pair}.json"


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
    _validate_desired_values(desired)
    return desired


def desired_digest(values: Mapping[str, Any]) -> str:
    """Return a stable digest for a resolved desired configuration."""
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w") as tmp_file:
        json.dump(document, tmp_file, sort_keys=True, indent=2)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, path)


def write_overrides(
    path: PathLike, overrides: Dict[str, Any], *, pair: str,
    profiles: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate and atomically persist a pair-aware overlay for a new write."""
    validated = validate_overrides(overrides, pair=pair, profiles=profiles)
    desired = {key: profiles[pair][key] for key in editable_schema(pair, profiles)}
    desired.update(validated)
    _validate_desired_values(desired)

    overlay_file = Path(path)
    previous_file = overlay_file.with_name(overlay_file.name + ".previous")
    if overlay_file.exists():
        _atomic_write(previous_file, _load_document(overlay_file))
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
