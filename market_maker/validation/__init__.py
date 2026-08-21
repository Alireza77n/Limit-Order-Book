"""Validation Module - Quantitative trading metrics for market making algorithms.

This module provides industry-standard validation metrics for evaluating
trading algorithm performance, including:

- Performance Metrics: Total Return
- Risk-Adjusted Metrics: Sharpe Ratio, Sortino Ratio
- Risk Metrics: Maximum Drawdown, Calmar Ratio
- Trading Metrics: Win Rate, Profit Factor, Expected Value
- Inventory Metrics: Average Inventory, Volatility, Max Exposure, Holding Time

Usage:
    from market_maker.validation import ValidationEngine, create_validation_engine
    from market_maker.dashboard import load_track
    
    # Load data from existing dashboard CSV
    df = load_track("path/to/track.csv")
    
    # Run validation
    engine = create_validation_engine()
    result = engine.validate_from_csv(df)
    
    # Access metrics
    print(f"Total Return: {result.metrics.total_return:.2%}")
    print(f"Sharpe Ratio: {result.metrics.sharpe_ratio:.2f}")
    print(f"Max Drawdown: {result.metrics.max_drawdown:.2%}")
"""

from __future__ import annotations

from validation.validation_engine import ValidationEngine, ValidationResult, create_validation_engine
from validation.schemas import TradeRecord, PortfolioSnapshot, ValidationMetrics
from validation.reports import generate_validation_report, export_report_json, format_metric_card

__all__ = [
    "ValidationEngine",
    "ValidationResult",
    "create_validation_engine",
    "TradeRecord",
    "PortfolioSnapshot",
    "ValidationMetrics",
    "generate_validation_report",
    "export_report_json",
    "format_metric_card",
]

__version__ = "1.0.0"