"""Total Return metric calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_total_return(equity_curve: pd.Series) -> float:
    """Calculate total return from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Total return as decimal (e.g., 0.154 for 15.4%)
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return 0.0
    
    initial_equity = equity_curve.iloc[0]
    final_equity = equity_curve.iloc[-1]
    
    if initial_equity == 0:
        return 0.0
    
    return (final_equity - initial_equity) / initial_equity


def calculate_period_returns(equity_curve: pd.Series) -> pd.Series:
    """Calculate period-over-period returns from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        
    Returns:
        Series of period returns
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return pd.Series(dtype=float)
    
    return equity_curve.pct_change().dropna()


def calculate_annualized_return(equity_curve: pd.Series, periods_per_year: int = 252 * 24 * 60) -> float:
    """Calculate annualized return from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        periods_per_year: Number of periods in a year (default: minute-level for crypto)
        
    Returns:
        Annualized return as decimal
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return 0.0
    
    total_return = calculate_total_return(equity_curve)
    n_periods = len(equity_curve) - 1
    
    if n_periods <= 0:
        return 0.0
    
    # Cap the annualization factor to avoid overflow
    # For very high frequency data, the annualization can explode
    # Cap at reasonable maximum (e.g., 100x leverage on annualization)
    max_ann_factor = 100.0
    ann_factor = periods_per_year / n_periods
    if ann_factor > max_ann_factor:
        ann_factor = max_ann_factor
    
    if total_return <= -1:
        return -1.0
    
    return (1 + total_return) ** ann_factor - 1