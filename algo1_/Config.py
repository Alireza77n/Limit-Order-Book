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
 

    # Loop / time controls
    time_step: int = 60                            # seconds between rounds
    time_horizon_days: int = 1                     # until midnight of +N days
    max_rounds: Optional[int] = None
    # None = run to horizon
    output_csv: str =None   # <-- NEW
    
    session: float = None


    # API auth endpoints (required to read balances / place orders)
    # Token comes from the environment (.env) -- see .env.example. No secret
    # is hardcoded here; if NOBITEX_API_TOKEN is unset this is None, and the
    # existing "endpoints not configured" checks in NobitexExchangeClient
    # degrade gracefully instead of crashing.
    token: Optional[str] = field(default_factory=lambda: os.environ.get("NOBITEX_API_TOKEN"))
    url_send_order: Optional[str] = "https://apiv2.nobitex.ir/market/orders/add"
    url_wallet_balance: Optional[str] = "https://apiv2.nobitex.ir/users/wallets/balance"
