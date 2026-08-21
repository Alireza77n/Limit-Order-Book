"""Expected Value / Average Trade metric calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_expected_value(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate expected value per trade using probability-weighted outcomes.
    
    EV = Win Probability * Average Win - Loss Probability * Average Loss
    
    Args:
        trades: DataFrame with trade records
        pnl_column: Column name containing PnL per trade
        
    Returns:
        Expected value per trade
    """
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    total_trades = len(trades)
    if total_trades == 0:
        return 0.0
    
    wins = trades[trades[pnl_column] > 0]
    losses = trades[trades[pnl_column] < 0]
    
    win_prob = len(wins) / total_trades
    loss_prob = len(losses) / total_trades
    
    avg_win = wins[pnl_column].mean() if len(wins) > 0 else 0.0
    avg_loss = abs(losses[pnl_column].mean()) if len(losses) > 0 else 0.0
    
    return float(win_prob * avg_win - loss_prob * avg_loss)


def calculate_average_trade_pnl(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate simple average PnL per trade.
    
    Average Trade = Total PnL / Number of Trades
    
    Args:
        trades: DataFrame with trade records
        pnl_column: Column name containing PnL per trade
        
    Returns:
        Average PnL per trade
    """
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    total_pnl = trades[pnl_column].sum()
    total_trades = len(trades)
    
    if total_trades == 0:
        return 0.0
    
    return float(total_pnl / total_trades)


def calculate_average_win(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate average winning trade PnL."""
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    wins = trades[trades[pnl_column] > 0]
    if wins.empty:
        return 0.0
    
    return float(wins[pnl_column].mean())


def calculate_average_loss(trades: pd.DataFrame, pnl_column: str = "pnl") -> float:
    """Calculate average losing trade PnL (positive value)."""
    if trades.empty or pnl_column not in trades.columns:
        return 0.0
    
    losses = trades[trades[pnl_column] < 0]
    if losses.empty:
        return 0.0
    
    return float(abs(losses[pnl_column].mean()))