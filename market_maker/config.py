import math
import os
import time
import requests
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


@dataclass
class MMConfig:
    # Trading / market params
    commission_perc: float = 0.0025
    cancel_after_minutes: float = 10.0
    exp_ret_min: Optional[float] = None
    algorithm_id: str = "default"           # if None, equals commission_perc
    depth_max: int = 15
    inv_max: float = 0.000365215
    inv_min: float = 0.0
    pair: str = "BTCIRT"                           # e.g., "BTCIRT" or "BTCUSDT"
    currency_1: str = "btc"                        # base currency code for wallet endpoint
    currency_2: str = "rls"                        # quote currency (e.g., "rls" or "usdt")
    order_size: Optional[float] = 0.000015             # if None, inv_max/100
    inv_initial: float = None
    cash_initial: float = None

    # Demo/paper-trading starting inventory (run.py's make_cfg applies this
    # to inv_initial when --mode demo, instead of the real profile's
    # inherited position). Change this to start demo runs somewhere other
    # than flat, without touching run.py.
    demo_inv_initial: float = 0.0
    demo_level1_fast_fill_enabled: bool = True
    demo_level1_fill_delay_seconds: float = 0.0
    demo_level1_fill_fraction: float = 1.0
    demo_queue_enabled: bool = False
    demo_state_path: Optional[str] = None
    demo_fee_state_path: Optional[str] = None
    demo_archive_path: Optional[str] = None
    demo_hot_terminal_order_limit: int = 1_000
    demo_archive_max_bytes: int = 10_000_000
    demo_result_db_path: Optional[str] = None
    demo_csv_export_max_rows: int = 10_000
    demo_result_retain_rows: int = 100_000
    no_quote_alert_rounds: int = 30
    no_quote_alert_seconds: float = 300.0
    no_quote_alert_cooldown_seconds: float = 1_800.0
    no_quote_recovery_alert: bool = True
    algo1_diagnostics_enabled: bool = False
    algo1_diagnostics_path: Optional[str] = None


    # Loop / time controls
    time_step: int = 60                            # seconds between rounds
    time_horizon_days: int = 1                     # until midnight of +N days
    max_rounds: Optional[int] = None
    continuous: bool = False
    # None = run to horizon
    output_csv: str =None   # <-- NEW
    
    session: Any = None
    mode: str = "real"


    # API auth endpoints (required to read balances / place orders)
    # Token comes from the environment (.env) -- see .env.example. No secret
    # is hardcoded here; if NOBITEX_API_TOKEN is unset this is None, and the
    # existing "endpoints not configured" checks in NobitexExchangeClient
    # degrade gracefully instead of crashing.
    token: Optional[str] = field(default_factory=lambda: os.environ.get("NOBITEX_API_TOKEN"))
    url_send_order: Optional[str] = "https://apiv2.nobitex.ir/market/orders/add"
    url_wallet_balance: Optional[str] = "https://apiv2.nobitex.ir/users/wallets/balance"


# Where run() writes its CSV output -- anchored to this file's own
# directory (not the process's CWD) so run.py and dashboard.py agree on
# the same location regardless of where each is launched from.
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def demo_csv_path(real_path: str) -> str:
    """Insert a _demo suffix before the extension so demo runs never
    overwrite/mix into a real-mode CSV (e.g. btc_track.csv -> btc_track_demo.csv)."""
    if "." in real_path.rsplit("/", 1)[-1]:
        base, ext = real_path.rsplit(".", 1)
        return f"{base}_demo.{ext}"
    return f"{real_path}_demo"


# One entry per currency pair this project can run. run.py's make_cfg()
# builds an MMConfig from one of these plus a fresh session id; dashboard.py
# reads the same dict directly so both always agree on file paths -- change
# a pair's parameters (including its demo starting inventory) here, in one
# place, rather than hunting through run.py or dashboard.py.
TRADING_PROFILES: Dict[str, Dict[str, Any]] = {
    "btc": dict(
        commission_perc=0.0010,
        cancel_after_minutes=10,
        algorithm_id="1btc",
        depth_max=10,
        inv_max=0.000472,
        inv_min=0.0,
        pair="BTCIRT",
        currency_1="btc",
        currency_2="rls",
        time_step=60,
        time_horizon_days=1,
        max_rounds=500,
        inv_initial=0.0001888,
        order_size=0.000015,
        cash_initial=30000000,
        demo_inv_initial=0.0,   # BTC demo starting inventory -- edit here to change it
        url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
        url_send_order="https://apiv2.nobitex.ir/market/orders/add",
        output_csv=os.path.join(RESULTS_DIR, "btc_track.csv"),
    ),
    "usdt": dict(
        commission_perc=0.0010,
        cancel_after_minutes=10,
        algorithm_id="1usdt",
        depth_max=10,
        inv_max=50,
        inv_min=0.0,
        pair="USDTIRT",
        currency_1="usdt",
        currency_2="rls",
        time_step=10,
        time_horizon_days=3,
        max_rounds=50000,
        inv_initial=27.16,
        order_size=1,
        cash_initial=30000000,
        demo_inv_initial=0.0,   # USDT demo starting inventory -- edit here to change it
        url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
        url_send_order="https://apiv2.nobitex.ir/market/orders/add",
        output_csv=os.path.join(RESULTS_DIR, "usdt_track.csv"),
    ),
}
