"""Per-pair Demo-mode configuration overlay.

Lets an admin tweak a small, closed set of Demo-only knobs (order size,
inventory limits, fill simulation, alert thresholds, ...) without touching
`config.TRADING_PROFILES` or any identity/secret field. `run.py:make_cfg`
merges the overlay on top of the profile defaults only when `mode == "demo"`
-- Real Mode and `strategy/algo1.py` never see this file.

Storage is a single JSON file per pair (`demo_config_<pair>.json`), written
atomically (tmp + os.replace, matching `session.py`'s pattern). Before each
overwrite the previous overlay is preserved as `<path>.previous` -- a single
undo slot, not a history stack (see spec Adjustment 4).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Union

PathLike = Union[str, os.PathLike]


class OverlayValidationError(ValueError):
    pass


# field -> (python type, min, max) for numeric fields; bools use (bool,).
# None for min/max means unbounded on that side.
FIELD_SPECS: Dict[str, tuple] = {
    "order_size": (float, 0, None),
    "inv_max": (float, 0, None),
    "inv_min": (float, 0, None),
    "demo_inv_initial": (float, 0, None),
    "cash_initial": (float, 0, None),
    "commission_perc": (float, 0, 1),
    "cancel_after_minutes": (float, 0, None),
    "depth_max": (int, 1, None),
    "time_step": (int, 1, None),
    "max_rounds": (int, 1, None),
    "demo_level1_fast_fill_enabled": (bool,),
    "demo_level1_fill_delay_seconds": (float, 0, None),
    "demo_level1_fill_fraction": (float, 0, 1),
    "demo_queue_enabled": (bool,),
    "no_quote_alert_rounds": (int, 0, None),
    "no_quote_alert_seconds": (float, 0, None),
    "no_quote_alert_cooldown_seconds": (float, 0, None),
    "no_quote_recovery_alert": (bool,),
    "algo1_diagnostics_enabled": (bool,),
}

ALLOWED_FIELDS = frozenset(FIELD_SPECS)

# Identity/secret fields: never editable or displayed, even if someone tries
# to smuggle them into an overlay payload.
FORBIDDEN_FIELDS = frozenset({
    "pair", "currency_1", "currency_2", "algorithm_id", "token",
    "url_send_order", "url_wallet_balance", "session", "mode",
})


def validate_overrides(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and type-coerce an overlay payload. Raises
    OverlayValidationError (fail-closed) on any unknown key, forbidden key,
    wrong type, or out-of-range value. Returns a new dict with coerced
    values -- never mutates the input."""
    if not isinstance(overrides, dict):
        raise OverlayValidationError("overrides must be a dict")
    validated: Dict[str, Any] = {}
    for key, raw_value in overrides.items():
        if key in FORBIDDEN_FIELDS:
            raise OverlayValidationError(f"field is not configurable: {key}")
        if key not in FIELD_SPECS:
            raise OverlayValidationError(f"unknown field: {key}")
        spec = FIELD_SPECS[key]
        expected_type = spec[0]
        if expected_type is bool:
            if not isinstance(raw_value, bool):
                raise OverlayValidationError(f"{key} must be a boolean")
            validated[key] = raw_value
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise OverlayValidationError(f"{key} must be numeric")
        value = expected_type(raw_value)
        _, low, high = spec
        if low is not None and value < low:
            raise OverlayValidationError(f"{key} must be >= {low}")
        if high is not None and value > high:
            raise OverlayValidationError(f"{key} must be <= {high}")
        validated[key] = value
    return validated


def overlay_path(state_dir: PathLike, pair: str) -> Path:
    return Path(state_dir) / f"demo_config_{pair}.json"


def load_overrides(path: PathLike) -> Dict[str, Any]:
    overlay_file = Path(path)
    if not overlay_file.exists():
        return {}
    try:
        document = json.loads(overlay_file.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return validate_overrides(document)


def _atomic_write(path: Path, document: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w") as tmp_file:
        json.dump(document, tmp_file, sort_keys=True, indent=2)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, path)


def write_overrides(path: PathLike, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Validate `overrides`, preserve the current file (if any) as
    `<path>.previous`, then atomically write the new overlay. Returns the
    validated overrides that were written."""
    validated = validate_overrides(overrides)
    overlay_file = Path(path)
    previous_file = overlay_file.with_name(overlay_file.name + ".previous")
    if overlay_file.exists():
        _atomic_write(previous_file, json.loads(overlay_file.read_text()))
    _atomic_write(overlay_file, validated)
    return validated


def has_previous(path: PathLike) -> bool:
    overlay_file = Path(path)
    return overlay_file.with_name(overlay_file.name + ".previous").exists()


def rollback_overrides(path: PathLike) -> Dict[str, Any]:
    """Restore the overlay from its `.previous` slot. Raises
    OverlayValidationError if there is nothing to roll back to."""
    overlay_file = Path(path)
    previous_file = overlay_file.with_name(overlay_file.name + ".previous")
    if not previous_file.exists():
        raise OverlayValidationError(f"no previous overlay to restore: {path}")
    document = json.loads(previous_file.read_text())
    validated = validate_overrides(document)
    _atomic_write(overlay_file, validated)
    return validated


def apply_overrides(cfg: Any, overrides: Dict[str, Any]) -> Any:
    """Apply a validated overlay dict onto an MMConfig instance in place,
    returning it for convenience. Only ever called by make_cfg when
    mode == 'demo'."""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
