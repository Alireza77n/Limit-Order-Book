"""Sortino Ratio metric calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_downside_deviation(
    returns: pd.Series,
    target_return: float = 0.0
) -> float:
    """Calculate downside deviation (semi-standard deviation).
    
    Args:
        returns: Series of period returns
        target_return: Minimum acceptable return (MAR)
        
    Returns:
        Downside deviation
    """
    if returns.empty:
        return np.nan
    
    # Only consider returns below target
    downside_returns = returns[returns < target_return] - target_return
    
    if len(downside_returns) == 0:
        return 0.0
    
    return float(np.sqrt(np.mean(downside_returns ** 2)))


def calculate_sortino_ratio(
    returns: pd.Series,
    target_return: float = 0.0,
    periods_per_year: int = 252 * 24 * 60
) -> float:
    """Calculate Sortino ratio from returns series.
    
    Args:
        returns: Series of period returns
        target_return: Minimum acceptable return per period (decimal)
        periods_per_year: Number of periods in a year for annualization
        
    Returns:
        Annualized Sortino ratio
    """
    if returns.empty or len(returns) < 2:
        return np.nan
    
    # Convert annual target to period target
    target_period = (1 + target_return) ** (1 / periods_per_year) - 1
    
    mean_return = returns.mean()
    downside_dev = calculate_downside_deviation(returns, target_period)
    
    if downside_dev == 0:
        return np.nan
    
    # Annualize
    excess_return = mean_return - target_period
    sortino = excess_return / downside_dev * np.sqrt(periods_per_year)
    
    return float(sortino)


def calculate_sortino_from_equity(
    equity_curve: pd.Series,
    target_return: float = 0.0,
    periods_per_year: int = 252 * 24 * 60
) -> float:
    """Calculate Sortino ratio directly from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        target_return: Annual minimum acceptable return (decimal)
        periods_per_year: Number of periods in a year for annualization
        
    Returns:
        Annualized Sortino ratio
    """
    returns = equity_curve.pct_change().dropna()
    return calculate_sortino_ratio(returns, target_return, periods_per_year)