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
    choices: Optional[Tuple[str, ...]] = None   # str fields: the only values accepted


# The algorithms the configuration may select (strategy/catalog.py's
# SELECTABLE_ALGORITHMS; pinned equal by tests/test_strategy_catalog.py).
ALGORITHM_CHOICES: Tuple[str, ...] = ("algo1", "algo3")


@dataclass(frozen=True)
class RuntimeReloadResult:
    ok: bool
    error: Optional[str] = None
    changed_fields: Tuple[str, ...] = ()
    snapshot: Optional[Dict[str, Any]] = None


FIELD_DEFINITIONS: Mapping[str, FieldDefinition] = MappingProxyType({
    # ---- Which algorithm runs (strategy/catalog.py); first, so the config pages show it on top ----
    "algorithm": FieldDefinition(str, None, None, ApplicationMode.HOT, "Live worker: the quoting algorithm it runs, read when it is started. algo1: distance grid, fill probabilities and expected value. algo3: order-book imbalance, microprice and a fixed spread. The Demo worker ignores it: it runs every algorithm enabled below, each in its own lane.", choices=ALGORITHM_CHOICES),
    "algo1_enabled": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Demo only: run MM_algo1v2 (algo1) on this pair in its own lane, with its own paper account and result files. The lane starts or stops at the worker's next check."),
    "algo3_enabled": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Demo only: run MM_algo2 (algo3) on this pair in its own lane, with its own paper account and result files. The lane starts or stops at the worker's next check."),
    "order_size": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Base amount per order. With inventory sizing on, each side is scaled around this amount (0 to 2x at strength 1)."),
    "inventory_sizing_enabled": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Scale buy and sell sizes linearly with inventory: buy more when inventory is low, sell more when it is high. Off sends a fixed order_size on both sides."),
    "inventory_sizing_strength": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Slope k of the linear rule. 0 keeps a fixed order_size; 1 makes the buy reach zero at inv_max and the sell reach zero at inv_min."),
    "inventory_sizing_min_fraction": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Smallest size of either side as a fraction of order_size. 0 lets a side go to zero."),
    "inv_max": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Maximum inventory the worker will hold."),
    "inv_min": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Minimum inventory the worker will hold."),
    "cancel_after_minutes": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Age before resting paper orders are cancelled."),
    "depth_max": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Maximum order-book depth used by the strategy."),
    "time_step": FieldDefinition(int, 1, None, ApplicationMode.HOT, "Seconds between quoting rounds."),
    "demo_inv_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting inventory for a new Demo session."),
    "cash_initial": FieldDefinition(float, 0, None, ApplicationMode.NEW_SESSION, "Starting quote cash for a new Demo session."),
    "commission_perc": FieldDefinition(float, 0, 1, ApplicationMode.NEW_SESSION, "Simulated fee fraction for a new Demo session."),
    "exp_ret_min": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Minimum expected round-trip NET return; Algorithm 1 quotes when the fee-adjusted return on the submitted buy price exceeds this value."),
    "buy_locked_cash_limit_enabled": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Reject new BUY orders that would push locked cash past the ratio below."),
    "buy_locked_cash_limit_ratio": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Max share of total quote cash (free + already locked in BUY orders) locked in resting BUY orders, 0-1."),
    "sell_locked_inventory_limit_enabled": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Reject new SELL orders that would push locked inventory past the ratio below."),
    "sell_locked_inventory_limit_ratio": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Max share of total base inventory (free + already locked in SELL orders) locked in resting SELL orders, 0-1."),
    # ---- Algorithm 1 v2 quote selection (strategy/algo1.py) ----
    "grid_min_perc": FieldDefinition(float, 0.00001, 0.5, ApplicationMode.HOT, "Nearest candidate distance from the quote centre, as a fraction of price (0.0005 = 0.05%)."),
    "grid_ratio": FieldDefinition(float, 1.05, 10, ApplicationMode.HOT, "Multiplier between consecutive candidate distances (1.4 = each step 40% farther out)."),
    "grid_max_perc": FieldDefinition(float, 0.00001, 0.5, ApplicationMode.HOT, "Farthest candidate distance from the quote centre, as a fraction of price (0.02 = 2%)."),
    "skew_perc": FieldDefinition(float, 0, 0.1, ApplicationMode.HOT, "How far the inventory moves the quote centre at the band edges, as a fraction of price: centre = mid * (1 - skew * (2x - 1)). 0 = no skew."),
    "one_sided_lo": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Inventory position (0-1) at or below which only BUY orders are quoted."),
    "one_sided_hi": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Inventory position (0-1) at or above which only SELL orders are quoted."),
    "cost_basis_reference": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Judge a lone SELL against the average price we paid for the inventory (when known) instead of the current quote centre."),
    "ev_min": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Expected-value floor per order lifetime (fraction of capital); pairs below it are not quoted even if they clear the profit bar."),
    "adverse_selection_perc": FieldDefinition(float, 0, 0.1, ApplicationMode.HOT, "Average loss charged in the expected value when only one leg fills, as a fraction of price (measured from the price move after fills; 0 at first)."),
    "hysteresis_ticks": FieldDefinition(int, 0, 10000, ApplicationMode.HOT, "Keep a resting order while the new price is within this many ticks of it (or within requote_band_perc, whichever band is wider) and it is younger than refresh_seconds. 0 = ticks play no part."),
    "refresh_seconds": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Age after which a resting order is re-quoted even when its price barely moved."),
    "requote_band_perc": FieldDefinition(float, 0, 0.1, ApplicationMode.HOT, "Requote band as a fraction of price (0.0003 = 0.03%). A resting order of ours is kept while the new price is within the band; a new price outside it cancels the resting order and replaces it, so at most one order rests per side. 0 = ticks only."),
    "trend_window_seconds": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Trend pause window: the mid's move is measured over this many seconds. 0 = no trend pause."),
    "trend_pause_perc": FieldDefinition(float, 0, 0.5, ApplicationMode.HOT, "Trend pause: when the mid fell by more than this fraction over the window, no buy is placed and resting buys are cancelled; when it rose by more than this, the same for sells. 0 = no trend pause."),
    "fill_table_enabled": FieldDefinition(bool, None, None, ApplicationMode.HOT, "Use the measured fill-probability table (fill_table_<pair>.json) when it is fresh; off = formula only."),
    "fill_table_max_age_hours": FieldDefinition(float, 0, None, ApplicationMode.HOT, "A fill table older than this is ignored and the formula is used."),
    "fill_table_min_samples": FieldDefinition(int, 0, None, ApplicationMode.HOT, "Table buckets with fewer observations than this fall back to the formula."),
    "fill_kappa": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Fallback fill formula: decay of the fill rate per unit of relative distance (400 = e-fold every 0.25%)."),
    "fill_lambda_tau": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Fallback fill formula: expected fills at zero distance during one order lifetime."),
    "fill_queue_volume_ref": FieldDefinition(float, 0, None, ApplicationMode.HOT, "Queue correction: fill probability is multiplied by exp(-volume ahead / this). 0 = no correction."),
    # ---- Algorithm 3 (strategy/algo3.py) ----
    "algo3_depth_k": FieldDefinition(int, 1, 50, ApplicationMode.HOT, "Book levels on each side that enter the imbalance."),
    "algo3_decay_rho": FieldDefinition(float, 0.05, 1.0, ApplicationMode.HOT, "Weight of each deeper level in the imbalance, rho^i (1 = every level counts the same)."),
    "algo3_beta": FieldDefinition(float, 0, 0.05, ApplicationMode.HOT, "Lean of the quote centre per unit of imbalance, as a fraction of price (0 = microprice centre only, until beta is estimated from data)."),
    "algo3_half_spread": FieldDefinition(float, 0, 0.5, ApplicationMode.HOT, "Half-spread around the centre, as a fraction of price. 0 = the smallest half-spread that clears the fee bar, plus one tick."),
    "algo3_min_volume_orders": FieldDefinition(float, 0, None, ApplicationMode.HOT, "A top of book thinner than this many order sizes carries no signal: imbalance 0, centre at the mid. 0 = always trust the book."),
    "algo3_imbalance_clip": FieldDefinition(float, 0, 1, ApplicationMode.HOT, "Largest imbalance the lean may use (0.8 = one spoofed level cannot push the lean to its maximum)."),
})

ALLOWED_FIELDS = frozenset(FIELD_DEFINITIONS)

# Old overlays may contain these fields from prior Demo releases. They are
# discarded only while loading existing files; new writes may never add them.
LEGACY_REMOVED_FIELDS = frozenset({
    "max_rounds",
    "demo_level1_fast_fill_enabled",
    "demo_level1_fill_delay_seconds",
    "demo_level1_fill_fraction",
    "no_quote_alert_rounds",
    "no_quote_alert_seconds",
    "no_quote_alert_cooldown_seconds",
    "no_quote_recovery_alert",
    "algo1_diagnostics_enabled",
    "demo_queue_enabled",
    "demo_max_fill_per_observation_orders",
    "demo_adverse_fill_enabled",
    "demo_adverse_fill_probability",
    "demo_non_adverse_fill_probability",
})

FORBIDDEN_FIELDS = frozenset({
    "pair", "currency_1", "currency_2", "algorithm_id", "token",
    "api_key", "secret_key", "url_send_order", "url_wallet_balance", "session", "mode",
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
    if {"grid_min_perc", "grid_max_perc"} <= values.keys() and values["grid_min_perc"] > values["grid_max_perc"]:
        raise OverlayValidationError("grid_min_perc must be <= grid_max_perc")
    if {"one_sided_lo", "one_sided_hi"} <= values.keys() and values["one_sided_lo"] >= values["one_sided_hi"]:
        raise OverlayValidationError("one_sided_lo must be < one_sided_hi")
    if (
        "demo_max_fill_per_observation_orders" in values
        and values["demo_max_fill_per_observation_orders"] <= 0
    ):
        raise OverlayValidationError("demo_max_fill_per_observation_orders must be > 0")


def _validate_field(key: str, raw_value: Any, definition: FieldDefinition) -> Any:
    if definition.value_type is bool:
        if not isinstance(raw_value, bool):
            raise OverlayValidationError(f"{key} must be a boolean")
        return raw_value
    if definition.value_type is str:
        if not isinstance(raw_value, str):
            raise OverlayValidationError(f"{key} must be a string")
        if definition.choices is not None and raw_value not in definition.choices:
            raise OverlayValidationError(f"{key} must be one of: {', '.join(definition.choices)}")
        return raw_value
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


def runtime_snapshot_path(state_dir: PathLike, pair: str, lane: Optional[str] = None) -> Path:
    """The worker's published runtime truth; a demo lane (one algorithm of a
    pair's worker) publishes its own file, runtime_config_<pair>_<lane>.json."""
    if lane:
        return Path(state_dir) / f"runtime_config_{pair}_{lane}.json"
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
    desired: Mapping[str, Any],
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
    applied = snapshot["applied"]
    pending = snapshot["pending_new_session"]
    desired_fields = set(desired)
    if set(applied) != desired_fields or not set(pending) <= desired_fields:
        return False
    try:
        for field, value in applied.items():
            definition = FIELD_DEFINITIONS[field]
            _validate_field(field, value, definition)
        _validate_desired_values(applied)
        for field, value in pending.items():
            definition = FIELD_DEFINITIONS[field]
            if definition.mode is not ApplicationMode.NEW_SESSION:
                return False
            _validate_field(field, value, definition)
    except (KeyError, OverlayValidationError, TypeError):
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

    if not _snapshot_is_current_shape(
        pair, snapshot, current_session_id, desired_values,
    ):
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
    encoded = json.dumps(
        document, sort_keys=True, indent=2, allow_nan=False,
    ).encode()
    _atomic_write_bytes(path, encoded)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("wb") as tmp_file:
        tmp_file.write(content)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, path)


def _restore_file(path: Path, existed: bool, content: Optional[bytes]) -> None:
    if existed:
        assert content is not None
        _atomic_write_bytes(path, content)
    elif path.exists():
        path.unlink()


def read_runtime_snapshot(
    state_dir: PathLike, pair: str, lane: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Read a worker-owned runtime snapshot, or ``None`` when absent."""
    path = runtime_snapshot_path(state_dir, pair, lane)
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
    state_dir: PathLike, pair: str, document: Dict[str, Any], lane: Optional[str] = None
) -> Dict[str, Any]:
    """Atomically publish a worker-owned runtime snapshot."""
    if not isinstance(document, dict):
        raise OverlayValidationError("runtime snapshot must be a dict")
    if document.get("pair") != pair:
        raise OverlayValidationError("runtime snapshot pair does not match target pair")
    _atomic_write(runtime_snapshot_path(state_dir, pair, lane), document)
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
    overlay_existed = overlay_file.exists()
    previous_existed = previous_file.exists()
    overlay_content = overlay_file.read_bytes() if overlay_existed else None
    previous_content = previous_file.read_bytes() if previous_existed else None
    preserved = (
        load_overrides(overlay_file, pair=pair, profiles=profiles)
        if overlay_existed else None
    )
    try:
        if preserved is not None:
            _atomic_write(previous_file, preserved)
        _atomic_write(overlay_file, validated)
    except Exception:
        _restore_file(overlay_file, overlay_existed, overlay_content)
        _restore_file(previous_file, previous_existed, previous_content)
        raise
    return validated


def has_previous(path: PathLike) -> bool:
    overlay_file = Path(path)
    return overlay_file.with_name(overlay_file.name + ".previous").exists()


def rollback_overrides(
    path: PathLike, *, pair: Optional[str] = None,
    profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Restore the overlay from its `.previous` slot."""
    overlay_file = Path(path)
    previous_file = overlay_file.with_name(overlay_file.name + ".previous")
    if not previous_file.exists():
        raise OverlayValidationError(f"no previous overlay to restore: {path}")
    validated = load_overrides(previous_file, pair=pair, profiles=profiles)
    if pair is not None and profiles is not None:
        desired = {
            key: profiles[pair][key] for key in editable_schema(pair, profiles)
        }
        desired.update(validated)
        validate_overrides(desired, pair=pair, profiles=profiles)
        _validate_desired_values(desired)
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
        lane: Optional[str] = None,
    ) -> None:
        """``lane``: the algorithm key of a demo lane. A lane publishes its
        snapshot to runtime_config_<pair>_<lane>.json and keeps its own
        ``algorithm`` (the overlay's ``algorithm`` field is reported as
        applied but never changes the lane's strategy)."""
        self.cfg = cfg
        self.ledger = ledger
        self.pair = pair
        self.state_dir = Path(state_dir)
        self.profiles = profiles
        self.lane = lane
        self._desired_digest: Optional[str] = None
        self._session_values = {
            "demo_inv_initial": float(ledger.initial_inventory),
            "cash_initial": float(ledger.initial_cash),
            "commission_perc": float(ledger.initial_commission_perc),
        }

    def _applied_values(self, desired: Mapping[str, Any]) -> Dict[str, Any]:
        schema = editable_schema(self.pair, self.profiles)
        applied: Dict[str, Any] = {}
        for field, definition in schema.items():
            if definition.mode is ApplicationMode.NEW_SESSION:
                applied[field] = self._session_values[field]
            elif field == "algorithm" and self.lane and field in desired:
                applied[field] = desired[field]      # a lane's algorithm is pinned; the field is live-only
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
            previous = read_runtime_snapshot(self.state_dir, self.pair, self.lane)
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
            write_runtime_snapshot(self.state_dir, self.pair, snapshot, self.lane)
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
                current_snapshot = read_runtime_snapshot(self.state_dir, self.pair, self.lane)
            except OverlayLoadError:
                current_snapshot = None
            if current_snapshot is not None and current_snapshot.get("status") == "applied":
                return RuntimeReloadResult(ok=True, snapshot=current_snapshot)

        schema = editable_schema(self.pair, self.profiles)
        hot_values = {
            field: desired[field]
            for field, definition in schema.items()
            if definition.mode is ApplicationMode.HOT
            and not (field == "algorithm" and self.lane)      # a lane's algorithm is pinned
        }
        changed_fields = tuple(
            field for field, value in hot_values.items()
            if getattr(self.cfg, field) != value
        )
        previous_hot_values = {
            field: getattr(self.cfg, field) for field in hot_values
        }
        ledger_runtime_fields = (
            "queue_enabled",
            "max_fill_per_observation",
            "adverse_fill_enabled",
            "adverse_fill_probability",
            "non_adverse_fill_probability",
        )
        previous_ledger_values = {
            field: getattr(self.ledger, field)
            for field in ledger_runtime_fields
            if hasattr(self.ledger, field)
        }
        for field, value in hot_values.items():
            setattr(self.cfg, field, value)
        if hasattr(self.ledger, "queue_enabled") and hasattr(self.cfg, "demo_queue_enabled"):
            self.ledger.queue_enabled = bool(self.cfg.demo_queue_enabled)
        if (
            hasattr(self.ledger, "max_fill_per_observation")
            and hasattr(self.cfg, "demo_max_fill_per_observation_orders")
        ):
            self.ledger.max_fill_per_observation = (
                float(self.cfg.order_size)
                * float(self.cfg.demo_max_fill_per_observation_orders)
            )
        if (
            hasattr(self.ledger, "adverse_fill_enabled")
            and hasattr(self.cfg, "demo_adverse_fill_enabled")
        ):
            self.ledger.adverse_fill_enabled = bool(self.cfg.demo_adverse_fill_enabled)
        if (
            hasattr(self.ledger, "adverse_fill_probability")
            and hasattr(self.cfg, "demo_adverse_fill_probability")
        ):
            self.ledger.adverse_fill_probability = float(
                self.cfg.demo_adverse_fill_probability
            )
        if (
            hasattr(self.ledger, "non_adverse_fill_probability")
            and hasattr(self.cfg, "demo_non_adverse_fill_probability")
        ):
            self.ledger.non_adverse_fill_probability = float(
                self.cfg.demo_non_adverse_fill_probability
            )

        snapshot = self._snapshot(
            desired,
            digest=digest,
            status="applied",
            error=None,
        )
        try:
            write_runtime_snapshot(self.state_dir, self.pair, snapshot, self.lane)
        except (OSError, OverlayValidationError) as error:
            for field, value in previous_hot_values.items():
                setattr(self.cfg, field, value)
            for field, value in previous_ledger_values.items():
                setattr(self.ledger, field, value)
            return self._publish_error(error)
        self._desired_digest = digest
        return RuntimeReloadResult(
            ok=True,
            changed_fields=changed_fields,
            snapshot=snapshot,
        )
