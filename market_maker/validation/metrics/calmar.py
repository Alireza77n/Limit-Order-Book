"""Calmar Ratio metric calculation."""

from __future__ import annotations

import numpy as np

from validation.metrics.returns import calculate_annualized_return
from validation.metrics.drawdown import calculate_max_drawdown


def calculate_calmar_ratio(
    equity_curve: pd.Series,
    periods_per_year: int = 252 * 24 * 60
) -> float:
    """Calculate Calmar ratio from equity curve.
    
    Calmar Ratio = Annualized Return / Maximum Drawdown
    
    Args:
        equity_curve: Series of equity values over time
        periods_per_year: Number of periods in a year for annualization
        
    Returns:
        Calmar ratio
    """
    try:
        import pandas as pd
    except ImportError:
        return np.nan
    
    if equity_curve.empty or len(equity_curve) < 2:
        return np.nan
    
    annual_return = calculate_annualized_return(equity_curve, periods_per_year)
    max_dd = calculate_max_drawdown(equity_curve)
    
    if max_dd == 0:
        return np.nan
    
    return float(annual_return / max_dd)


def calculate_calmar_from_return_and_dd(
    annual_return: float,
    max_drawdown: float
) -> float:
    """Calculate Calmar ratio from pre-computed values.
    
    Args:
        annual_return: Annualized return (decimal)
        max_drawdown: Maximum drawdown (positive decimal)
        
    Returns:
        Calmar ratio
    """
    import numpy as np
    
    if max_drawdown == 0:
        return np.nan
    
    return float(annual_return / max_drawdown)