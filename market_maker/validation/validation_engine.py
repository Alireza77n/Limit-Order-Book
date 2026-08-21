"""Validation Engine - Main orchestrator for all validation metrics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from validation.schemas import (
    TradeRecord,
    PortfolioSnapshot,
    ValidationMetrics,
    extract_trades_from_csv,
    extract_snapshots_from_csv,
    snapshots_to_dataframe,
    trades_to_dataframe,
)
from validation.metrics.returns import calculate_total_return, calculate_annualized_return
from validation.metrics.sharpe import calculate_sharpe_from_equity
from validation.metrics.sortino import calculate_sortino_from_equity
from validation.metrics.drawdown import calculate_max_drawdown, calculate_drawdown_curve
from validation.metrics.calmar import calculate_calmar_ratio
from validation.metrics.win_rate import calculate_win_rate
from validation.metrics.profit_factor import calculate_profit_factor
from validation.metrics.expected_value import calculate_expected_value, calculate_average_trade_pnl
from validation.metrics.inventory import calculate_inventory_metrics

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Complete validation result with all metrics and supporting data."""
    metrics: ValidationMetrics
    equity_curve: pd.Series
    drawdown_curve: pd.Series
    trades_df: pd.DataFrame
    snapshots_df: pd.DataFrame
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API serialization."""
        return {
            "performance": {
                "total_return": self.metrics.total_return,
            },
            "risk": {
                "sharpe": self.metrics.sharpe_ratio,
                "sortino": self.metrics.sortino_ratio,
                "max_drawdown": self.metrics.max_drawdown,
            },
            "trading": {
                "win_rate": self.metrics.win_rate,
                "profit_factor": self.metrics.profit_factor,
                "expected_value": self.metrics.expected_value,
            },
            "inventory": {
                "average_inventory": self.metrics.average_inventory,
                "inventory_volatility": self.metrics.inventory_volatility,
                "max_inventory": self.metrics.max_inventory,
                "average_holding_time": self.metrics.average_holding_time,
            },
            "calmar_ratio": self.metrics.calmar_ratio,
            "avg_trade_pnl": self.metrics.avg_trade_pnl,
        }


class ValidationEngine:
    """Main validation engine for computing all metrics."""
    
    def __init__(
        self,
        risk_free_rate: float = 0.0,
        target_return: float = 0.0,
        periods_per_year: int = 252 * 24 * 60,  # Minute-level for crypto
    ):
        """Initialize validation engine.
        
        Args:
            risk_free_rate: Annual risk-free rate (decimal)
            target_return: Annual target return for Sortino (decimal)
            periods_per_year: Periods per year for annualization
        """
        self.risk_free_rate = risk_free_rate
        self.target_return = target_return
        self.periods_per_year = periods_per_year
    
    def validate_from_csv(self, df: pd.DataFrame) -> ValidationResult:
        """Run validation on data from dashboard CSV format.
        
        Args:
            df: DataFrame from dashboard CSV (loaded via dashboard.load_track)
            
        Returns:
            ValidationResult with all computed metrics
        """
        # Extract trades and snapshots
        trades = extract_trades_from_csv(df)
        snapshots = extract_snapshots_from_csv(df)
        
        trades_df = trades_to_dataframe(trades)
        snapshots_df = snapshots_to_dataframe(snapshots)
        
        return self.validate(trades_df, snapshots_df)
    
    def validate(
        self,
        trades_df: pd.DataFrame,
        snapshots_df: pd.DataFrame
    ) -> ValidationResult:
        """Run validation on pre-processed trade and snapshot DataFrames.
        
        Args:
            trades_df: DataFrame with trade records (must have 'pnl' column)
            snapshots_df: DataFrame with portfolio snapshots (must have 'equity', 'inventory', 'timestamp')
            
        Returns:
            ValidationResult with all computed metrics
        """
        # Build equity curve from snapshots
        if snapshots_df.empty or "equity" not in snapshots_df.columns:
            logger.warning("No equity data available for validation")
            equity_curve = pd.Series(dtype=float)
        else:
            equity_curve = snapshots_df.sort_values("timestamp").set_index("timestamp")["equity"]
        
        # Calculate drawdown curve
        drawdown_curve = calculate_drawdown_curve(equity_curve)
        
        # Performance metrics
        total_return = calculate_total_return(equity_curve)
        
        # Risk-adjusted metrics
        sharpe = calculate_sharpe_from_equity(
            equity_curve, self.risk_free_rate, self.periods_per_year
        )
        sortino = calculate_sortino_from_equity(
            equity_curve, self.target_return, self.periods_per_year
        )
        
        # Risk metrics
        max_dd = calculate_max_drawdown(equity_curve)
        calmar = calculate_calmar_ratio(equity_curve, self.periods_per_year)
        
        # Trading metrics
        win_rate = calculate_win_rate(trades_df)
        profit_factor = calculate_profit_factor(trades_df)
        expected_value = calculate_expected_value(trades_df)
        avg_trade_pnl = calculate_average_trade_pnl(trades_df)
        
        # Inventory metrics
        inv_metrics = calculate_inventory_metrics(snapshots_df)
        
        metrics = ValidationMetrics(
            total_return=total_return,
            sharpe_ratio=sharpe if not np.isnan(sharpe) else None,
            sortino_ratio=sortino if not np.isnan(sortino) else None,
            max_drawdown=max_dd,
            calmar_ratio=calmar if not np.isnan(calmar) else None,
            win_rate=win_rate,
            profit_factor=profit_factor if not np.isinf(profit_factor) else None,
            expected_value=expected_value,
            avg_trade_pnl=avg_trade_pnl,
            average_inventory=inv_metrics["average_inventory"],
            inventory_volatility=inv_metrics["inventory_volatility"],
            max_inventory=inv_metrics["max_inventory"],
            average_holding_time=inv_metrics["average_holding_time"],
        )
        
        return ValidationResult(
            metrics=metrics,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            trades_df=trades_df,
            snapshots_df=snapshots_df,
        )


def create_validation_engine(
    risk_free_rate: float = 0.0,
    target_return: float = 0.0,
    periods_per_year: int = 252 * 24 * 60,
) -> ValidationEngine:
    """Factory function to create a ValidationEngine instance."""
    return ValidationEngine(
        risk_free_rate=risk_free_rate,
        target_return=target_return,
        periods_per_year=periods_per_year,
    )