"""Isolated live-forward configuration helpers.

Live settings are kept separately from paper-trading overlays so a dashboard
change cannot alter an existing Demo session.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from demo_config import (
    OverlayLoadError,
    OverlayValidationError,
    _atomic_write,
    editable_schema,
    validate_overrides,
)


FORWARD_FIELDS = frozenset({
    "order_size", "inventory_sizing_enabled", "inventory_sizing_strength",
    "inventory_sizing_min_fraction",
    "inv_max", "inv_min", "cancel_after_minutes", "depth_max",
    "time_step", "commission_perc", "exp_ret_min",
    "buy_locked_cash_limit_enabled", "buy_locked_cash_limit_ratio",
    "sell_locked_inventory_limit_enabled", "sell_locked_inventory_limit_ratio",
    # Algorithm 1 v2 quote selection (same definitions as the Demo overlay).
    "grid_min_perc", "grid_ratio", "grid_max_perc", "skew_perc",
    "one_sided_lo", "one_sided_hi", "cost_basis_reference",
    "ev_min", "adverse_selection_perc", "hysteresis_ticks", "refresh_seconds",
    "requote_band_perc", "trend_window_seconds", "trend_pause_perc",
    "fill_table_enabled", "fill_table_max_age_hours", "fill_table_min_samples",
    "fill_kappa", "fill_lambda_tau", "fill_queue_volume_ref",
})

# The authenticated Nobitex account reports a 0.25% IRT fee for both maker
# and taker orders.  A forward run has a buy and a sell leg, so never allow a
# live configuration to understate either leg's commission.  Demo profiles
# intentionally remain independently configurable.
FORWARD_MIN_COMMISSION_PERC = 0.0025


def config_dir() -> Path:
    return Path(os.environ.get("LOB_FORWARD_CONFIG_DIR", "runtime/forward/config"))


def config_path(pair: str, directory=None) -> Path:
    return Path(directory or config_dir()) / f"forward_config_{pair}.json"


def desired_config(pair, profiles, directory=None):
    schema = editable_schema(pair, profiles)
    base = {key: profiles[pair][key] for key in schema if key in FORWARD_FIELDS}
    # Conservative live default: the target starts at the fee when nothing
    # has been saved yet; a value saved from the Live Config page wins.
    base["exp_ret_min"] = max(float(base.get("exp_ret_min", 0.0)), FORWARD_MIN_COMMISSION_PERC)
    path = config_path(pair, directory)
    if path.exists():
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OverlayLoadError(f"unable to read forward config {path}") from exc
        if not isinstance(values, dict):
            raise OverlayLoadError("forward config must be a JSON object")
        loaded = validate_overrides(values, pair=pair, profiles=profiles)
        base.update({key: value for key, value in loaded.items() if key in FORWARD_FIELDS})
    base["commission_perc"] = max(
        float(base.get("commission_perc", 0.0)), FORWARD_MIN_COMMISSION_PERC
    )
    base["buy_locked_cash_limit_enabled"] = bool(base.get("buy_locked_cash_limit_enabled"))
    base["sell_locked_inventory_limit_enabled"] = bool(base.get("sell_locked_inventory_limit_enabled"))
    base["buy_locked_cash_limit_ratio"] = min(float(base.get("buy_locked_cash_limit_ratio", 0.25)), 0.25)
    base["sell_locked_inventory_limit_ratio"] = min(float(base.get("sell_locked_inventory_limit_ratio", 0.25)), 0.25)
    # Real runs default to conservative exposure limits even though the
    # historical Demo/CLI profiles keep these switches off.
    base["buy_locked_cash_limit_enabled"] = True
    base["sell_locked_inventory_limit_enabled"] = True
    return validate_overrides(base, pair=pair, profiles=profiles)


def validate_forward_config(values, pair, profiles, directory=None):
    validated = {
        key: value
        for key, value in validate_overrides(values, pair=pair, profiles=profiles).items()
        if key in FORWARD_FIELDS
    }
    merged = desired_config(pair, profiles, directory)
    merged.update(validated)
    if merged["order_size"] <= 0:
        raise OverlayValidationError("order_size must be greater than zero")
    if merged["inv_min"] >= merged["inv_max"]:
        raise OverlayValidationError("inv_min must be less than inv_max")
    if merged.get("grid_min_perc", 0.0) > merged.get("grid_max_perc", 1.0):
        raise OverlayValidationError("grid_min_perc must be at most grid_max_perc")
    if merged.get("one_sided_lo", 0.0) >= merged.get("one_sided_hi", 1.0):
        raise OverlayValidationError("one_sided_lo must be less than one_sided_hi")
    if merged["commission_perc"] < FORWARD_MIN_COMMISSION_PERC:
        raise OverlayValidationError(
            "live forward testing requires commission_perc of at least 0.0025"
        )
    # The required net return is policy, not the exchange fee: any
    # non-negative target is allowed live (validate_overrides already rejects
    # negatives; the fee floor above still makes every submitted pair clear
    # both commissions).
    if not merged["buy_locked_cash_limit_enabled"]:
        raise OverlayValidationError("live forward testing requires the BUY exposure limit")
    if not merged["sell_locked_inventory_limit_enabled"]:
        raise OverlayValidationError("live forward testing requires the SELL exposure limit")
    if merged["buy_locked_cash_limit_ratio"] > 0.25 or merged["sell_locked_inventory_limit_ratio"] > 0.25:
        raise OverlayValidationError("live exposure limits must be at most 0.25")
    return merged


def save_config(values, pair, profiles, directory=None):
    validated = validate_overrides(values, pair=pair, profiles=profiles)
    merged = desired_config(pair, profiles, directory)
    merged.update(validated)
    merged = validate_forward_config(merged, pair, profiles, directory)
    path = config_path(pair, directory)
    previous = path.with_name(path.name + ".previous")
    if path.exists():
        previous.write_bytes(path.read_bytes())
    _atomic_write(path, merged)
    return merged
