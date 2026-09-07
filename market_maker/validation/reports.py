"""Report generation for validation results."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

import pandas as pd

from validation.validation_engine import ValidationResult


def generate_validation_report(result: ValidationResult) -> Dict[str, Any]:
    """Generate a comprehensive validation report dictionary.
    
    Args:
        result: ValidationResult from ValidationEngine
        
    Returns:
        Dictionary with all metrics and metadata
    """
    metrics = result.metrics
    
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "summary": {
            "total_return_pct": round(metrics.total_return * 100, 2),
            "sharpe_ratio": round(metrics.sharpe_ratio, 3) if metrics.sharpe_ratio else None,
            "sortino_ratio": round(metrics.sortino_ratio, 3) if metrics.sortino_ratio else None,
            "max_drawdown_pct": round(metrics.max_drawdown * 100, 2),
            "calmar_ratio": round(metrics.calmar_ratio, 3) if metrics.calmar_ratio else None,
            "win_rate_pct": round(metrics.win_rate * 100, 2),
            "profit_factor": round(metrics.profit_factor, 3) if metrics.profit_factor else None,
            "expected_value": round(metrics.expected_value, 4),
            "avg_trade_pnl": round(metrics.avg_trade_pnl, 4),
            "avg_inventory": round(metrics.average_inventory, 6),
            "max_inventory": round(metrics.max_inventory, 6),
            "inventory_volatility": round(metrics.inventory_volatility, 6),
            "average_holding_time_seconds": round(metrics.average_holding_time, 2)
            if metrics.average_holding_time is not None else None,
        },
        "details": {
            "performance": {
                "total_return": metrics.total_return,
            },
            "risk_adjusted": {
                "sharpe_ratio": metrics.sharpe_ratio,
                "sortino_ratio": metrics.sortino_ratio,
            },
            "risk": {
                "max_drawdown": metrics.max_drawdown,
                "calmar_ratio": metrics.calmar_ratio,
            },
            "trading": {
                "win_rate": metrics.win_rate,
                "profit_factor": metrics.profit_factor,
                "expected_value": metrics.expected_value,
                "avg_trade_pnl": metrics.avg_trade_pnl,
            },
            "inventory": {
                "average_inventory": metrics.average_inventory,
                "inventory_volatility": metrics.inventory_volatility,
                "max_inventory": metrics.max_inventory,
                "average_holding_time_seconds": metrics.average_holding_time,
            },
        },
        "charts": {
            "equity_curve": _series_to_chart_data(result.equity_curve, "Equity"),
            "drawdown_curve": _series_to_chart_data(result.drawdown_curve, "Drawdown"),
            "inventory_curve": _series_to_chart_data(
                result.snapshots_df.set_index("timestamp")["inventory"] 
                if not result.snapshots_df.empty and "inventory" in result.snapshots_df.columns 
                else pd.Series(dtype=float),
                "Inventory"
            ),
        },
    }


def _series_to_chart_data(series: pd.Series, name: str) -> Dict[str, Any]:
    """Convert a pandas Series to chart-friendly format."""
    if series.empty:
        return {"name": name, "x": [], "y": []}
    
    return {
        "name": name,
        "x": series.index.astype(str).tolist(),
        "y": series.values.tolist(),
    }


def export_report_json(result: ValidationResult, filepath: str) -> None:
    """Export validation report to JSON file.
    
    Args:
        result: ValidationResult from ValidationEngine
        filepath: Output file path
    """
    report = generate_validation_report(result)
    with open(filepath, "w") as f:
        json.dump(report, f, indent=2, default=str)


def format_metric_card(metric_name: str, value: float, format_type: str = "pct") -> Dict[str, str]:
    """Format a metric for dashboard card display.
    
    Args:
        metric_name: Name of the metric
        value: Raw metric value
        format_type: "pct" for percentage, "ratio" for decimal, "raw" for raw
        
    Returns:
        Dictionary with formatted label and value
    """
    if format_type == "pct":
        formatted = f"{value * 100:+.2f}%"
    elif format_type == "ratio":
        formatted = f"{value:.2f}" if value is not None else "N/A"
    else:
        formatted = f"{value:.4f}"
    
    return {
        "label": metric_name,
        "value": formatted,
    }