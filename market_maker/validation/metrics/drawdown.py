"""Maximum Drawdown and Drawdown Curve metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_drawdown_curve(equity_curve: pd.Series) -> pd.Series:
    """Calculate drawdown series from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Series of drawdown values (as decimals, e.g., -0.1 for -10%)
    """
    if equity_curve.empty:
        return pd.Series(dtype=float)
    
    # Calculate running maximum (peak)
    running_max = equity_curve.expanding().max()
    
    # Drawdown = (peak - current) / peak
    drawdown = (running_max - equity_curve) / running_max
    
    return drawdown


def calculate_max_drawdown(equity_curve: pd.Series) -> float:
    """Calculate maximum drawdown from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Maximum drawdown as positive decimal (e.g., 0.082 for 8.2%)
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return 0.0
    
    drawdown_curve = calculate_drawdown_curve(equity_curve)
    return float(drawdown_curve.max())


def calculate_drawdown_duration(equity_curve: pd.Series) -> pd.Series:
    """Calculate drawdown duration (periods since last peak).
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Series of drawdown durations in periods
    """
    if equity_curve.empty:
        return pd.Series(dtype=int)
    
    running_max = equity_curve.expanding().max()
    is_peak = equity_curve == running_max
    
    # Calculate periods since last peak
    duration = (~is_peak).astype(int)
    # Reset counter at each new peak
    duration = duration.groupby((is_peak).cumsum()).cumsum()
    
    return duration


def calculate_max_drawdown_duration(equity_curve: pd.Series) -> int:
    """Calculate maximum drawdown duration.
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Maximum number of periods in drawdown
    """
    duration = calculate_drawdown_duration(equity_curve)
    if duration.empty:
        return 0
    return int(duration.max())


def calculate_average_drawdown(equity_curve: pd.Series) -> float:
    """Calculate average drawdown (excluding zero drawdown periods).
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Average drawdown as decimal
    """
    drawdown = calculate_drawdown_curve(equity_curve)
    non_zero = drawdown[drawdown > 0]
    
    if non_zero.empty:
        return 0.0
    
    return float(non_zero.mean())