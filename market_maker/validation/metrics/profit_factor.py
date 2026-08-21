"""Profit Factor metric calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_profit_factor(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate profit factor from trades DataFrame.
    
    Profit Factor = Gross Profit / |Gross Loss|
    
    Args:
        trades: DataFrame with trade records
        pnl_column: Column name containing PnL per trade
        
    Returns:
        Profit factor (float('inf') if no losing trades, NaN if no trades or no wins)
    """
    if trades.empty or pnl_column not in trades.columns:
        return np.nan
    
    gross_profit = trades[trades[pnl_column] > 0][pnl_column].sum()
    gross_loss = trades[trades[pnl_column] < 0][pnl_column].sum()
    
    if gross_profit == 0:
        return np.nan
    
    if gross_loss == 0:
        return float('inf')
    
    return float(gross_profit / abs(gross_loss))


def calculate_gross_profit(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate gross profit from trades DataFrame."""
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    return float(trades[trades[pnl_column] > 0][pnl_column].sum())


def calculate_gross_loss(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate gross loss from trades DataFrame (positive value)."""
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    return float(abs(trades[trades[pnl_column] < 0][pnl_column].sum()))