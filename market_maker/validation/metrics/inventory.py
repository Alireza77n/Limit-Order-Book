"""Inventory Risk Metrics calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_inventory_metrics(
    snapshots: pd.DataFrame,
    inventory_column: str = "inventory",
    timestamp_column: str = "timestamp"
) -> dict:
    """Calculate comprehensive inventory risk metrics.
    
    Args:
        snapshots: DataFrame with portfolio snapshots over time
        inventory_column: Column name for inventory/position
        timestamp_column: Column name for timestamps
        
    Returns:
        Dictionary with inventory metrics
    """
    if snapshots.empty or inventory_column not in snapshots.columns:
        return {
            "average_inventory": 0.0,
            "inventory_volatility": 0.0,
            "max_inventory": 0.0,
            "average_holding_time": None,
        }
    
    inventory = snapshots[inventory_column].dropna()
    
    if inventory.empty:
        return {
            "average_inventory": 0.0,
            "inventory_volatility": 0.0,
            "max_inventory": 0.0,
            "average_holding_time": None,
        }
    
    # Average absolute inventory
    avg_inventory = float(inventory.abs().mean())
    
    # Inventory volatility (std of inventory)
    inv_volatility = float(inventory.std(ddof=1))
    
    # Maximum absolute inventory exposure
    max_inventory = float(inventory.abs().max())
    
    # Average holding time - estimate from inventory changes
    holding_time = _estimate_holding_time(snapshots, inventory_column, timestamp_column)
    
    return {
        "average_inventory": avg_inventory,
        "inventory_volatility": inv_volatility,
        "max_inventory": max_inventory,
        "average_holding_time": holding_time,
    }


def _estimate_holding_time(
    snapshots: pd.DataFrame,
    inventory_column: str,
    timestamp_column: str
) -> float:
    """Estimate average inventory holding time in seconds.
    
    This is a simplified estimation based on periods where inventory != 0.
    For a more accurate measure, we'd need order-level data with entry/exit times.
    """
    if snapshots.empty or len(snapshots) < 2:
        return None
    
    # Ensure sorted by timestamp
    df = snapshots.sort_values(timestamp_column).copy()
    inventory = df[inventory_column]
    timestamps = df[timestamp_column]
    
    # Find periods where we have non-zero inventory
    has_inventory = inventory != 0
    
    if not has_inventory.any():
        return 0.0
    
    # Calculate time differences
    time_diffs = timestamps.diff().dt.total_seconds().fillna(0)
    
    # Sum time where inventory is non-zero
    holding_periods = time_diffs[has_inventory].sum()
    
    # Count inventory "episodes" (transitions from 0 to non-zero)
    inventory_changes = (inventory != 0) & (inventory.shift(1) == 0)
    num_episodes = inventory_changes.sum()
    
    if num_episodes == 0:
        return float(holding_periods)
    
    return float(holding_periods / num_episodes)


def calculate_inventory_turnover(
    snapshots: pd.DataFrame,
    inventory_column: str = "inventory",
    timestamp_column: str = "timestamp"
) -> float:
    """Calculate inventory turnover rate.
    
    Turnover = Sum of |inventory changes| / Average |inventory|
    
    Args:
        snapshots: DataFrame with portfolio snapshots
        inventory_column: Column name for inventory
        timestamp_column: Column name for timestamps
        
    Returns:
        Inventory turnover (dimensionless)
    """
    if snapshots.empty or inventory_column not in snapshots.columns:
        return 0.0
    
    df = snapshots.sort_values(timestamp_column)
    inventory = df[inventory_column].dropna()
    
    if len(inventory) < 2:
        return 0.0
    
    # Sum of absolute inventory changes
    inventory_changes = inventory.diff().abs().sum()
    
    # Average absolute inventory
    avg_abs_inventory = inventory.abs().mean()
    
    if avg_abs_inventory == 0:
        return 0.0
    
    return float(inventory_changes / avg_abs_inventory)