"""Unit tests for the Validation Module."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from validation.metrics.returns import calculate_total_return, calculate_annualized_return, calculate_period_returns
from validation.metrics.sharpe import calculate_sharpe_ratio, calculate_sharpe_from_equity
from validation.metrics.sortino import calculate_sortino_ratio, calculate_sortino_from_equity, calculate_downside_deviation
from validation.metrics.drawdown import calculate_max_drawdown, calculate_drawdown_curve, calculate_drawdown_duration
from validation.metrics.calmar import calculate_calmar_ratio, calculate_calmar_from_return_and_dd
from validation.metrics.win_rate import calculate_win_rate, calculate_loss_rate, calculate_breakeven_rate
from validation.metrics.profit_factor import calculate_profit_factor, calculate_gross_profit, calculate_gross_loss
from validation.metrics.expected_value import calculate_expected_value, calculate_average_trade_pnl, calculate_average_win, calculate_average_loss
from validation.metrics.inventory import calculate_inventory_metrics, calculate_inventory_turnover
from validation.validation_engine import ValidationEngine, create_validation_engine
from validation.schemas import extract_trades_from_csv, extract_snapshots_from_csv, trades_to_dataframe, snapshots_to_dataframe


class TestReturns:
    """Tests for return metrics."""
    
    def test_total_return_positive(self):
        equity = pd.Series([100, 110, 115, 120])
        result = calculate_total_return(equity)
        assert result == 0.2  # (120 - 100) / 100
    
    def test_total_return_negative(self):
        equity = pd.Series([100, 95, 90, 85])
        result = calculate_total_return(equity)
        assert result == -0.15  # (85 - 100) / 100
    
    def test_total_return_empty(self):
        equity = pd.Series(dtype=float)
        result = calculate_total_return(equity)
        assert result == 0.0
    
    def test_total_return_single_value(self):
        equity = pd.Series([100])
        result = calculate_total_return(equity)
        assert result == 0.0
    
    def test_total_return_zero_initial(self):
        equity = pd.Series([0, 100])
        result = calculate_total_return(equity)
        assert result == 0.0
    
    def test_annualized_return(self):
        # 10% return over 10 periods, 252 periods/year
        equity = pd.Series([100] + [110] * 10)
        result = calculate_annualized_return(equity, periods_per_year=252)
        # (1.1)^(252/10) - 1
        expected = (1.1) ** (252 / 10) - 1
        assert abs(result - expected) < 0.001


class TestSharpeRatio:
    """Tests for Sharpe ratio."""
    
    def test_sharpe_ratio_positive(self):
        returns = pd.Series([0.01, 0.02, 0.015, 0.01, 0.02])
        result = calculate_sharpe_ratio(returns, risk_free_rate=0.0, periods_per_year=252)
        assert result > 0
    
    def test_sharpe_ratio_negative(self):
        returns = pd.Series([-0.01, -0.02, -0.015])
        result = calculate_sharpe_ratio(returns, risk_free_rate=0.0, periods_per_year=252)
        assert result < 0
    
    def test_sharpe_ratio_zero_vol(self):
        returns = pd.Series([0.01, 0.01, 0.01, 0.01])
        result = calculate_sharpe_ratio(returns, risk_free_rate=0.0, periods_per_year=252)
        assert np.isnan(result)
    
    def test_sharpe_ratio_empty(self):
        returns = pd.Series(dtype=float)
        result = calculate_sharpe_ratio(returns)
        assert np.isnan(result)


class TestSortinoRatio:
    """Tests for Sortino ratio."""
    
    def test_downside_deviation(self):
        returns = pd.Series([0.01, -0.02, 0.015, -0.01, 0.02])
        result = calculate_downside_deviation(returns, target_return=0.0)
        # Only negative returns: -0.02, -0.01
        # Mean of squared: (0.0004 + 0.0001) / 2 = 0.00025
        # sqrt = 0.01581
        assert abs(result - 0.01581) < 0.001
    
    def test_downside_deviation_no_downside(self):
        returns = pd.Series([0.01, 0.02, 0.015, 0.01])
        result = calculate_downside_deviation(returns, target_return=0.0)
        assert result == 0.0
    
    def test_sortino_ratio(self):
        returns = pd.Series([0.01, -0.005, 0.015, -0.01, 0.02])
        result = calculate_sortino_ratio(returns, target_return=0.0, periods_per_year=252)
        assert not np.isnan(result)
    
    def test_sortino_ratio_no_downside(self):
        returns = pd.Series([0.01, 0.02, 0.015])
        result = calculate_sortino_ratio(returns, target_return=0.0, periods_per_year=252)
        assert np.isnan(result)


class TestDrawdown:
    """Tests for drawdown metrics."""
    
    def test_drawdown_curve(self):
        equity = pd.Series([100, 110, 105, 120, 115])
        dd = calculate_drawdown_curve(equity)
        # Peak at 100: dd=0
        # Peak at 110: dd=0
        # At 105: (110-105)/110 = 0.04545
        # Peak at 120: dd=0
        # At 115: (120-115)/120 = 0.04167
        assert dd.iloc[0] == 0.0
        assert dd.iloc[1] == 0.0
        assert abs(dd.iloc[2] - 0.04545) < 0.001
        assert dd.iloc[3] == 0.0
        assert abs(dd.iloc[4] - 0.04167) < 0.001
    
    def test_max_drawdown(self):
        equity = pd.Series([100, 110, 105, 120, 115, 100])
        # Max peak 120, min after peak 100 -> (120-100)/120 = 0.1667
        result = calculate_max_drawdown(equity)
        assert abs(result - 0.1667) < 0.001
    
    def test_max_drawdown_empty(self):
        equity = pd.Series(dtype=float)
        result = calculate_max_drawdown(equity)
        assert result == 0.0


class TestCalmarRatio:
    """Tests for Calmar ratio."""
    
    def test_calmar_ratio(self):
        annual_return = 0.20
        max_dd = 0.10
        result = calculate_calmar_from_return_and_dd(annual_return, max_dd)
        assert result == 2.0
    
    def test_calmar_ratio_zero_dd(self):
        result = calculate_calmar_from_return_and_dd(0.20, 0.0)
        assert np.isnan(result)


class TestWinRate:
    """Tests for win rate metrics."""
    
    def test_win_rate_normal(self):
        trades = pd.DataFrame({"pnl": [10, -5, 20, -10, 15]})
        result = calculate_win_rate(trades)
        assert result == 0.6  # 3 wins out of 5
    
    def test_win_rate_all_wins(self):
        trades = pd.DataFrame({"pnl": [10, 20, 15]})
        result = calculate_win_rate(trades)
        assert result == 1.0
    
    def test_win_rate_all_losses(self):
        trades = pd.DataFrame({"pnl": [-10, -20, -15]})
        result = calculate_win_rate(trades)
        assert result == 0.0
    
    def test_win_rate_empty(self):
        trades = pd.DataFrame({"pnl": []})
        result = calculate_win_rate(trades)
        assert result == 0.0
    
    def test_win_rate_with_breakeven(self):
        trades = pd.DataFrame({"pnl": [10, 0, -5, 0, 20]})
        result = calculate_win_rate(trades)
        assert result == 0.4  # 2 wins out of 5


class TestProfitFactor:
    """Tests for profit factor."""
    
    def test_profit_factor_normal(self):
        trades = pd.DataFrame({"pnl": [100, -50, 200, -100]})
        result = calculate_profit_factor(trades)
        assert result == 2.0  # (100+200) / (50+100) = 300/150 = 2
    
    def test_profit_factor_no_losses(self):
        trades = pd.DataFrame({"pnl": [100, 50, 200]})
        result = calculate_profit_factor(trades)
        assert result == float('inf')
    
    def test_profit_factor_no_wins(self):
        trades = pd.DataFrame({"pnl": [-100, -50, -200]})
        result = calculate_profit_factor(trades)
        assert np.isnan(result)
    
    def test_profit_factor_empty(self):
        trades = pd.DataFrame({"pnl": []})
        result = calculate_profit_factor(trades)
        assert np.isnan(result)


class TestExpectedValue:
    """Tests for expected value metrics."""
    
    def test_expected_value(self):
        trades = pd.DataFrame({"pnl": [10, -5, 20, -10, 15, -8]})
        # 3 wins, 3 losses
        # Win prob = 0.5, avg win = 15
        # Loss prob = 0.5, avg loss = 7.67
        # EV = 0.5 * 15 - 0.5 * 7.67 = 3.67
        result = calculate_expected_value(trades)
        assert abs(result - 3.6667) < 0.01
    
    def test_average_trade_pnl(self):
        trades = pd.DataFrame({"pnl": [10, -5, 20, -10]})
        result = calculate_average_trade_pnl(trades)
        assert result == 3.75  # (10-5+20-10)/4 = 15/4 = 3.75
    
    def test_average_win(self):
        trades = pd.DataFrame({"pnl": [10, -5, 20, -10, 15]})
        result = calculate_average_win(trades)
        assert result == 15.0  # (10+20+15)/3
    
    def test_average_loss(self):
        trades = pd.DataFrame({"pnl": [10, -5, 20, -10, 15]})
        result = calculate_average_loss(trades)
        assert result == 7.5  # (5+10)/2


class TestInventoryMetrics:
    """Tests for inventory metrics."""
    
    def test_inventory_metrics(self):
        snapshots = pd.DataFrame({
            "timestamp": pd.date_range("2024-01-01", periods=5, freq="1min"),
            "inventory": [0.0, 0.5, 0.3, 0.8, 0.2],
            "equity": [100, 101, 102, 103, 104],
            "cash": [100, 99, 99.5, 98, 98.5],
        })
        result = calculate_inventory_metrics(snapshots)
        assert result["average_inventory"] == 0.36  # mean of abs values
        assert result["max_inventory"] == 0.8
        assert result["inventory_volatility"] > 0
    
    def test_inventory_metrics_empty(self):
        snapshots = pd.DataFrame({"inventory": [], "equity": [], "cash": [], "timestamp": []})
        result = calculate_inventory_metrics(snapshots)
        assert result["average_inventory"] == 0.0
        assert result["max_inventory"] == 0.0


class TestValidationEngine:
    """Tests for the ValidationEngine."""
    
    def test_validate_empty_data(self):
        engine = create_validation_engine()
        trades = pd.DataFrame({"pnl": []})
        snapshots = pd.DataFrame({"equity": [], "inventory": [], "cash": [], "timestamp": []})
        result = engine.validate(trades, snapshots)
        assert result.metrics.total_return == 0.0
        assert result.metrics.win_rate == 0.0
    
    def test_validate_with_data(self):
        engine = create_validation_engine()
        trades = pd.DataFrame({"pnl": [10, -5, 20, -10, 15]})
        snapshots = pd.DataFrame({
            "timestamp": pd.date_range("2024-01-01", periods=5, freq="1min"),
            "equity": [100, 110, 105, 125, 115],
            "inventory": [0, 0.5, 0.3, 0.8, 0.2],
            "cash": [100, 105, 103, 120, 113],
        })
        result = engine.validate(trades, snapshots)
        assert result.metrics.total_return == 0.15
        assert result.metrics.win_rate == 0.6
        assert result.metrics.max_drawdown > 0


class TestSchemaExtraction:
    """Tests for schema extraction from CSV."""
    
    def test_extract_trades_from_csv(self):
        # Create mock CSV data
        df = pd.DataFrame({
            "Session_id": ["s1", "s1"],
            "Timestamp": ["2024-01-01 10:00:00", "2024-01-01 10:01:00"],
            "Buy Order ID": ["buy1", "buy2"],
            "Buy Status": ["filled", "filled"],
            "Buy Matched Amount": [0.1, 0.2],
            "Buy Limit Price": [50000, 51000],
            "Buy Processed Matched Amount": [0.1, 0.2],
            "Sell Order ID": [None, "sell1"],
            "Sell Status": [None, "filled"],
            "Sell Matched Amount": [None, 0.15],
            "Sell Limit Price": [None, 52000],
            "Sell Processed Matched Amount": [None, 0.15],
            "Round Fee": [10, 15],
            "Net Return": [5, 8],
            "Crypto": ["BTC", "BTC"],
        })
        trades = extract_trades_from_csv(df)
        assert len(trades) == 3  # 2 buys + 1 sell
        assert trades[0].side == "BUY"
        assert trades[1].side == "BUY"
        assert trades[2].side == "SELL"
    
    def test_extract_snapshots_from_csv(self):
        df = pd.DataFrame({
            "Session_id": ["s1", "s1"],
            "Timestamp": ["2024-01-01 10:00:00", "2024-01-01 10:01:00"],
            "Portfolio Wealth": [100000, 101000],
            "cash_current": [50000, 50500],
            "inv_current": [1.0, 0.9],
        })
        snapshots = extract_snapshots_from_csv(df)
        assert len(snapshots) == 2
        assert snapshots[0].equity == 100000
        assert snapshots[1].inventory == 0.9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
