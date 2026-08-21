"""Win Rate metric calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_win_rate(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate win rate from trades DataFrame.
    
    Win Rate = Winning Trades / Total Trades
    
    Args:
        trades: DataFrame with trade records
        pnl_column: Column name containing PnL per trade
        
    Returns:
        Win rate as decimal (e.g., 0.63 for 63%)
    """
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    total_trades = len(trades)
    winning_trades = len(trades[trades[pnl_column] > 0])
    
    if total_trades == 0:
        return 0.0
    
    return float(winning_trades / total_trades)


def calculate_loss_rate(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate loss rate from trades DataFrame.
    
    Loss Rate = Losing Trades / Total Trades
    
    Args:
        trades: DataFrame with trade records
        pnl_column: Column name containing PnL per trade
        
    Returns:
        Loss rate as decimal
    """
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    total_trades = len(trades)
    losing_trades = len(trades[trades[pnl_column] < 0])
    
    if total_trades == 0:
        return 0.0
    
    return float(losing_trades / total_trades)


def calculate_breakeven_rate(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate breakeven rate from trades DataFrame.
    
    Breakeven Rate = Zero PnL Trades / Total Trades
    
    Args:
        trades: DataFrame with trade records
        pnl_column: Column name containing PnL per trade
        
    Returns:
        Breakeven rate as decimal
    """
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    total_trades = len(trades)
    breakeven_trades = len(trades[trades[pnl_column] == 0])
    
    if total_trades == 0:
        return 0.0
    
    return float(breakeven_trades / total_trades)