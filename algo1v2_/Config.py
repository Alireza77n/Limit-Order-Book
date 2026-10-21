"""Configuration for the standalone Algorithm 1 v2 (subset of the project's
market_maker/config.py MMConfig: only the fields the strategy reads)."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class MMConfig:
    pair: str = "USDTIRT"             # tick: 10 rial for ...IRT, 0.01 otherwise
    commission_perc: float = 0.0025   # fee per leg
    exp_ret_min: Optional[float] = 0.0025   # required net return per round trip (None -> fee)
    inv_min: float = 0.0
    inv_max: float = 50.0
    depth_max: int = 10               # book levels considered when anchoring
    cancel_after_minutes: float = 3.0      # order lifetime: 3 minutes since 20 October 2026 (10 before)
    # candidate grid
    grid_min_perc: float = 0.0005
    grid_ratio: float = 1.4
    grid_max_perc: float = 0.02
    # inventory skew of the quote centre
    skew_perc: float = 0.002
    # one-sided quoting
    one_sided_lo: float = 0.1
    one_sided_hi: float = 0.9
    cost_basis_reference: bool = True
    # expected value
    ev_min: float = 0.00001
    adverse_selection_perc: float = 0.0
    # hysteresis
    hysteresis_ticks: int = 5
    refresh_seconds: float = 300.0
    requote_band_perc: float = 0.0003   # requote band: 5 ticks or 0.03 % of the price, whichever is wider
    # fill model
    fill_table_enabled: bool = True
    fill_table_max_age_hours: float = 48.0
    fill_table_min_samples: int = 50
    fill_kappa: float = 400.0
    fill_lambda_tau: float = 1.0
    fill_queue_volume_ref: float = 0.0
    fill_table_path: Optional[str] = None


PROFILES = {
    "btc": dict(pair="BTCIRT", inv_min=0.0, inv_max=0.000472, commission_perc=0.0025, exp_ret_min=0.0025),
    "usdt": dict(pair="USDTIRT", inv_min=0.0, inv_max=50.0, commission_perc=0.0025, exp_ret_min=0.0025),
}
