"""Data schemas for the Validation Module.

Defines the expected data structures for trade records, portfolio snapshots,
and validation results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
import pandas as pd


@dataclass
class TradeRecord:
    """Schema for a single trade record."""
    trade_id: str
    timestamp: datetime
    side: str  # "BUY" or "SELL"
    price: float
    quantity: float
    pnl: float
    fee: float
    session_id: Optional[str] = None
    symbol: Optional[str] = None


@dataclass
class PortfolioSnapshot:
    """Schema for a portfolio snapshot at a point in time."""
    timestamp: datetime
    cash: float
    inventory: float
    equity: float
    session_id: Optional[str] = None


@dataclass
class ValidationMetrics:
    """Complete set of validation metrics."""
    # Performance
    total_return: float
    
    # Risk-adjusted
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    
    # Risk
    max_drawdown: float
    calmar_ratio: Optional[float]
    
    # Trading
    win_rate: float
    profit_factor: Optional[float]
    expected_value: float
    avg_trade_pnl: float
    
    # Inventory
    average_inventory: float
    inventory_volatility: float
    max_inventory: float
    average_holding_time: Optional[float]


def trades_to_dataframe(trades: List[TradeRecord]) -> pd.DataFrame:
    """Convert list of TradeRecord to DataFrame."""
    if not trades:
        return pd.DataFrame(columns=[
            "trade_id", "timestamp", "side", "price", "quantity",
            "pnl", "fee", "session_id", "symbol"
        ])
    return pd.DataFrame([{
        "trade_id": t.trade_id,
        "timestamp": t.timestamp,
        "side": t.side,
        "price": t.price,
        "quantity": t.quantity,
        "pnl": t.pnl,
        "fee": t.fee,
        "session_id": t.session_id,
        "symbol": t.symbol,
    } for t in trades])


def snapshots_to_dataframe(snapshots: List[PortfolioSnapshot]) -> pd.DataFrame:
    """Convert list of PortfolioSnapshot to DataFrame."""
    if not snapshots:
        return pd.DataFrame(columns=[
            "timestamp", "cash", "inventory", "equity", "session_id"
        ])
    return pd.DataFrame([{
        "timestamp": s.timestamp,
        "cash": s.cash,
        "inventory": s.inventory,
        "equity": s.equity,
        "session_id": s.session_id,
    } for s in snapshots])


def extract_trades_from_csv(df: pd.DataFrame) -> List[TradeRecord]:
    """Extract trade records from the dashboard CSV format.
    
    The CSV has one row per round with Buy/Sell order details side by side.
    We need to explode this into individual trade records.
    """
    trades = []
    
    for _, row in df.iterrows():
        session_id = row.get("Session_id")
        timestamp = row.get("Timestamp")
        if pd.isna(timestamp):
            continue
            
        # Process Buy orders
        buy_order_id = row.get("Buy Order ID")
        buy_status = row.get("Buy Status")
        buy_matched = row.get("Buy Matched Amount")
        buy_price = row.get("Buy Limit Price")
        buy_filled = row.get("Buy Processed Matched Amount", buy_matched)
        buy_fee = row.get("Round Fee", 0)
        
        if pd.notna(buy_order_id) and pd.notna(buy_filled) and buy_filled > 0:
            # Calculate PnL for buy - we need the sell price to compute
            # For now, use net return from the row if available
            pnl = row.get("Net Return", 0) if pd.notna(row.get("Net Return")) else 0
            
            trades.append(TradeRecord(
                trade_id=str(buy_order_id),
                timestamp=pd.to_datetime(timestamp),
                side="BUY",
                price=float(buy_price) if pd.notna(buy_price) else 0.0,
                quantity=float(buy_filled),
                pnl=float(pnl),
                fee=float(buy_fee) if pd.notna(buy_fee) else 0.0,
                session_id=str(session_id) if pd.notna(session_id) else None,
                symbol=row.get("Crypto"),
            ))
        
        # Process Sell orders
        sell_order_id = row.get("Sell Order ID")
        sell_status = row.get("Sell Status")
        sell_matched = row.get("Sell Matched Amount")
        sell_price = row.get("Sell Limit Price")
        sell_filled = row.get("Sell Processed Matched Amount", sell_matched)
        sell_fee = row.get("Round Fee", 0)
        
        if pd.notna(sell_order_id) and pd.notna(sell_filled) and sell_filled > 0:
            pnl = row.get("Net Return", 0) if pd.notna(row.get("Net Return")) else 0
            
            trades.append(TradeRecord(
                trade_id=str(sell_order_id),
                timestamp=pd.to_datetime(timestamp),
                side="SELL",
                price=float(sell_price) if pd.notna(sell_price) else 0.0,
                quantity=float(sell_filled),
                pnl=float(pnl),
                fee=float(sell_fee) if pd.notna(sell_fee) else 0.0,
                session_id=str(session_id) if pd.notna(session_id) else None,
                symbol=row.get("Crypto"),
            ))
    
    return trades


def extract_snapshots_from_csv(df: pd.DataFrame) -> List[PortfolioSnapshot]:
    """Extract portfolio snapshots from the dashboard CSV format."""
    snapshots = []
    
    for _, row in df.iterrows():
        timestamp = row.get("Timestamp")
        session_id = row.get("Session_id")
        
        if pd.isna(timestamp):
            continue
            
        equity = row.get("Portfolio Wealth")
        cash = row.get("cash_current")
        inventory = row.get("inv_current")
        
        if pd.notna(equity) and pd.notna(cash) and pd.notna(inventory):
            snapshots.append(PortfolioSnapshot(
                timestamp=pd.to_datetime(timestamp),
                cash=float(cash),
                inventory=float(inventory),
                equity=float(equity),
                session_id=str(session_id) if pd.notna(session_id) else None,
            ))
    
    return snapshots