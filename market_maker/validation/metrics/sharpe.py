"""Sharpe Ratio metric calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_sharpe_ratio(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252 * 24 * 60
) -> float:
    """Calculate Sharpe ratio from returns series.
    
    Args:
        returns: Series of period returns
        risk_free_rate: Annual risk-free rate (decimal)
        periods_per_year: Number of periods in a year for annualization
        
    Returns:
        Annualized Sharpe ratio
    """
    if returns.empty or len(returns) < 2:
        return np.nan
    
    # Convert annual risk-free rate to period rate
    rf_period = (1 + risk_free_rate) ** (1 / periods_per_year) - 1
    
    excess_returns = returns - rf_period
    
    mean_excess = excess_returns.mean()
    std_excess = excess_returns.std(ddof=1)
    
    if std_excess == 0:
        return np.nan
    
    # Annualize
    sharpe = mean_excess / std_excess * np.sqrt(periods_per_year)
    
    return float(sharpe)


def calculate_sharpe_from_equity(
    equity_curve: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252 * 24 * 60
) -> float:
    """Calculate Sharpe ratio directly from equity curve.
    
    Args:
        equity_curve: Series of equity values over time
        risk_free_rate: Annual risk-free rate (decimal)
        periods_per_year: Number of periods in a year for annualization
        
    Returns:
        Annualized Sharpe ratio
    """
    returns = equity_curve.pct_change().dropna()
    return calculate_sharpe_ratio(returns, risk_free_rate, periods_per_year)