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

if os.environ.get("LOB_DISABLE_DOTENV") != "1":
    load_dotenv()


@dataclass
class MMConfig:
    # Trading / market params
    commission_perc: float = 0.0025
    # Age at which a resting order of ours is cancelled. 3 minutes since 20
    # October 2026 (it was 10): on the demo, most fills were of orders that
    # had rested long enough for the market to run through them.
    cancel_after_minutes: float = 3.0
    # Minimum expected round-trip NET return, as a fraction of the capital
    # deployed at the submitted buy price. Algorithm 1 only quotes a
    # (bid, ask) pair when its fee-adjusted round-trip return
    #     R_net = (A_q(1-fb)(1-fs) - B_q) / B_q
    # strictly exceeds this value -- see strategy/algo1.py's spread_ok.
    # The last traded price is never used as a return denominator; the
    # market's relative quoted spread is described by the mid-price instead.
    # If left None, engine.py's __init__ bootstraps it to commission_perc (a
    # 2x-commission bar), which is the value both profiles below now set
    # explicitly, so adding the field to TRADING_PROFILES here changes
    # nothing about current behavior. It is a real per-pair lever, though:
    # BTC/IRT trades at a wide enough spread to clear a 2x-commission bar
    # most rounds; USDT/IRT's spread rarely does, so lowering just its
    # exp_ret_min (via Admin, hot-reloadable) is the intended way to make it
    # quote more often without touching commission_perc or Algorithm 1 itself.
    exp_ret_min: Optional[float] = None
    algorithm_id: str = "default"           # if None, equals commission_perc
    # Which quoting algorithm the worker runs (strategy/catalog.py): "algo1"
    # or "algo3". Hot in Demo (the engine swaps the strategy at the next
    # round boundary); a live worker reads it when it is started.
    algorithm: str = "algo1"
    # Demo lanes (run.py's LaneRunner): the demo worker of a pair runs every
    # enabled algorithm at the same time, each in its own lane with its own
    # paper account and result files. Ignored by a live worker.
    algo1_enabled: bool = True
    algo3_enabled: bool = True
    algo4_enabled: bool = True
    # The lane this configuration belongs to (the algorithm key), set by
    # run.py's make_cfg(..., lane=...) for a demo lane; None otherwise.
    lane: Optional[str] = None
    # Requote band: a resting order of ours is kept while the new price is
    # within max(hysteresis_ticks x tick, requote_band_perc x price) of it;
    # a new price outside the band cancels it and replaces it, so at most one
    # order rests per side (engine._replace_outside_band). Before 20 October
    # 2026 the band was ticks only, 50 rial, which on BTC is nothing: a new
    # order was added almost every round and up to 11 of ours rested at once.
    requote_band_perc: float = 0.0003
    # Trend pause: when the mid fell by more than trend_pause_perc over the
    # last trend_window_seconds, no buy is placed and resting buys are
    # cancelled; when it rose by more than that, the same for sells.
    # 0.3 % over 10 minutes by default since 20 October 2026: the operator
    # wants no buying into a fall. Replayed on the demo's market of 11-17
    # October 2026 it changed the result by at most 0.06 of a point
    # (docs/algorithms/MM_algo2.md section 19). trend_window_seconds = 0 or
    # trend_pause_perc = 0 switches it off.
    trend_window_seconds: float = 600.0
    trend_pause_perc: float = 0.003
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

    # Pre-trade risk guard: reject a new order if the capital it would commit
    # pushes locked exposure past a configured share of the account's total
    # relevant resource. BUY locks quote cash; SELL locks base inventory --
    # independent switches/ratios so either side, on either pair, can be
    # tuned or disabled without affecting the other. See
    # SimpleMarketMaker._locked_cash_guard_reason /
    # _locked_inventory_guard_reason in engine.py. Ratio is a fraction
    # 0.0-1.0 (0.4 = 40%), matching this project's existing convention for
    # fractional config values (see commission_perc).
    buy_locked_cash_limit_enabled: bool = False
    buy_locked_cash_limit_ratio: float = 0.8
    sell_locked_inventory_limit_enabled: bool = False
    sell_locked_inventory_limit_ratio: float = 0.8

    demo_level1_fast_fill_enabled: bool = True
    demo_level1_fill_delay_seconds: float = 0.0
    demo_level1_fill_fraction: float = 1.0
    demo_queue_enabled: bool = False
    demo_max_fill_per_observation_orders: float = 1.0
    demo_adverse_fill_enabled: bool = False
    demo_adverse_fill_probability: float = 1.0
    demo_non_adverse_fill_probability: float = 1.0
    # Linear inventory-based order sizing -- see
    # SimpleMarketMaker._inventory_sized_order_targets in engine.py. Off for a
    # bare MMConfig; both TRADING_PROFILES below turn it on. strength is the
    # slope k (0 = fixed order_size, 1 = buy fades to zero at inv_max and sell
    # fades to zero at inv_min); min_fraction is a floor on each side as a
    # fraction of order_size (0 = no floor).
    inventory_sizing_enabled: bool = False
    inventory_sizing_strength: float = 1.0
    inventory_sizing_min_fraction: float = 0.0

    # MM_algo1v2 (Algorithm 1 v2) quote selection -- see strategy/algo1.py; it is
    # specified on the MM_algo1v2 branch (documented there, not on this branch).
    # All of these are hot-reloadable from
    # the admin panel (demo_config.FIELD_DEFINITIONS / forward_config.FORWARD_FIELDS).
    #
    # Candidate distances from the quote centre, as fractions of price:
    # delta_k = grid_min_perc * grid_ratio^(k-1) while delta_k <= grid_max_perc.
    grid_min_perc: float = 0.0005
    grid_ratio: float = 1.4
    grid_max_perc: float = 0.02
    # Inventory moves the quote centre: c = mid * (1 - skew_perc * (2x - 1)),
    # x = position inside [inv_min, inv_max]. 0 = no skew.
    skew_perc: float = 0.002
    # Below one_sided_lo only bids are quoted, above one_sided_hi only asks.
    one_sided_lo: float = 0.1
    one_sided_hi: float = 0.9
    # A lone sell is judged against the average cost of inventory we bought
    # ourselves when it is known (True), else against the quote centre.
    cost_basis_reference: bool = True
    # Expected-value floor (fraction of capital per order lifetime) and the
    # adverse-selection cost charged when only one leg fills.
    ev_min: float = 0.00001
    adverse_selection_perc: float = 0.0
    # Keep a resting price while the new one is within hysteresis_ticks and
    # the quote is younger than refresh_seconds (0 = always re-quote).
    hysteresis_ticks: int = 5
    refresh_seconds: float = 300.0
    # Fill-probability model (fill_model.py): measured table when fresh and
    # populated, else the formula p = 1 - exp(-lambda_tau * exp(-kappa * delta)).
    fill_table_enabled: bool = True
    fill_table_max_age_hours: float = 48.0
    fill_table_min_samples: int = 50
    fill_kappa: float = 400.0
    fill_lambda_tau: float = 1.0
    # Queue correction p *= exp(-volume_ahead / fill_queue_volume_ref); 0 = off.
    fill_queue_volume_ref: float = 0.0
    # MM_algo2 (strategy/algo3.py, docs/algorithms/MM_algo2.md): the
    # imbalance of the top algo3_depth_k levels (weights algo3_decay_rho^i)
    # leans a microprice centre by algo3_beta per unit of imbalance; the
    # inventory skew is shared with Algorithm 1 (skew_perc); one symmetric
    # half-spread algo3_half_spread (0 = the smallest that clears the fee
    # bar, plus one tick). A top of book thinner than
    # algo3_min_volume_orders * order_size carries no signal (imbalance 0,
    # centre at the mid); the imbalance is clipped to +-algo3_imbalance_clip
    # so one spoofed level cannot dominate.
    algo3_depth_k: int = 3
    algo3_decay_rho: float = 0.5
    algo3_beta: float = 0.0
    algo3_half_spread: float = 0.0
    algo3_min_volume_orders: float = 20.0
    algo3_imbalance_clip: float = 0.8
    # Algorithm 4 (strategy/algo4.py, shown as MM_algo3): a mean-reverting
    # (Ornstein-Uhlenbeck) forecast of the mid fitted on the last
    # algo4_window_seconds of samples taken every algo4_sample_seconds; the
    # forecast is used only when admissible (unit root rejected, half-life
    # within [algo4_halflife_min_seconds, algo4_halflife_max_seconds]), else
    # the quotes are the Avellaneda-Stoikov ones (algo4_use_alpha = False
    # forces that). The lean is the expected reversion over
    # algo4_horizon_seconds (0 = the order lifetime), clipped to
    # algo4_lean_clip of the price. 1/algo4_kappa is the fee-free optimal
    # half-spread when the fill table cannot be fitted (400 = 0.25 % of the
    # price); algo4_risk_perc widens both depths; algo4_sigma_pause stops
    # quoting while the fit's per-sample residual exceeds that fraction of
    # the price (0 = off).
    algo4_use_alpha: bool = True
    algo4_window_seconds: float = 1800.0
    algo4_sample_seconds: float = 10.0
    algo4_horizon_seconds: float = 0.0
    algo4_halflife_min_seconds: float = 60.0
    algo4_halflife_max_seconds: float = 900.0
    algo4_kappa: float = 400.0
    algo4_risk_perc: float = 0.0
    algo4_lean_clip: float = 0.005
    algo4_sigma_pause: float = 0.0
    fill_table_path: Optional[str] = None
    demo_state_path: Optional[str] = None
    demo_fee_state_path: Optional[str] = None
    demo_archive_path: Optional[str] = None
    demo_hot_terminal_order_limit: int = 1_000
    demo_archive_max_bytes: int = 10_000_000
    demo_result_db_path: Optional[str] = None
    demo_csv_export_max_rows: int = 10_000
    # Seconds between rewrites of the bounded CSV export during a demo run
    # (the SQLite store is updated every round; the CSV is also written at
    # start and at session end). 0 = every round.
    demo_csv_export_interval_seconds: float = 60.0
    demo_result_retain_rows: int = 100_000
    no_quote_alert_rounds: int = 30
    no_quote_alert_seconds: float = 300.0
    no_quote_alert_cooldown_seconds: float = 1_800.0
    no_quote_recovery_alert: bool = True
    algo1_diagnostics_enabled: bool = False
    algo1_diagnostics_path: Optional[str] = None
    # The per-round diagnostics JSONL (candidates, probabilities, expected
    # values, the selection) feeds the dashboard's "Quote decision" tab; the
    # engine keeps the newest N documents (see engine._rotate_diagnostics).
    # About 60 KB per USDT/IRT round (every scored pair is recorded), so the
    # file stays near 120 MB and is rewritten, streaming, at twice that.
    algo1_diagnostics_keep_rounds: int = 2000


    # Loop / time controls
    time_step: int = 60                            # seconds between rounds
    time_horizon_days: int = 1                     # until midnight of +N days
    max_rounds: Optional[int] = None
    continuous: bool = False
    # None = run to horizon
    output_csv: str =None   # <-- NEW
    
    session: Any = None
    mode: str = "real"
    profile_name: Optional[str] = None
    demo_config_state_dir: Optional[str] = None


    # API-key authentication (required to read balances / place orders).
    # Both values come from the environment (.env); no credentials are
    # hardcoded in source. If either value is unset, authenticated exchange
    # calls degrade gracefully instead of attempting a malformed request.
    api_key: Optional[str] = field(default_factory=lambda: os.environ.get("NOBITEX_API_KEY"))
    secret_key: Optional[str] = field(default_factory=lambda: os.environ.get("NOBITEX_SECRET_KEY"))
    url_send_order: Optional[str] = "https://apiv2.nobitex.ir/market/orders/add"
    url_wallet_balance: Optional[str] = "https://apiv2.nobitex.ir/users/wallets/balance"


# Where run() writes its CSV output -- anchored to this file's own
# directory (not the process's CWD) so run.py and dashboard.py agree on
# the same location regardless of where each is launched from.
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.environ.get("LOB_RESULTS_DIR", os.path.join(_PACKAGE_DIR, "results"))
DEMO_STATE_DIR = os.environ.get("LOB_STATE_DIR", RESULTS_DIR)


def demo_state_path(filename: str) -> str:
    return os.path.join(DEMO_STATE_DIR, filename)


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
        # Matches commission_perc, i.e. today's actual bootstrap default
        # (engine.py falls back to exp_ret_min=commission_perc when unset) --
        # adding it here makes it Admin-editable without changing behavior.
        exp_ret_min=0.0002,
        cancel_after_minutes=3,
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
        buy_locked_cash_limit_enabled=False,
        buy_locked_cash_limit_ratio=0.8,
        sell_locked_inventory_limit_enabled=False,
        sell_locked_inventory_limit_ratio=0.8,
        demo_queue_enabled=False,
        demo_max_fill_per_observation_orders=1.0,
        demo_adverse_fill_enabled=False,
        demo_adverse_fill_probability=1.0,
        demo_non_adverse_fill_probability=1.0,
        # Linear inventory sizing (engine._inventory_sized_order_targets):
        # buy = 2*order_size*(1 - x), sell = 2*order_size*x, with x the
        # position in the [inv_min, inv_max] band. strength is the slope k
        # (0 = fixed size), min_fraction a floor on each side (0 = none).
        inventory_sizing_enabled=True,
        inventory_sizing_strength=1.0,
        inventory_sizing_min_fraction=0.0,
        # Algorithm 1 v2 quote selection (strategy/algo1.py). The grid runs
        # 0.05% .. 2% from the centre in steps of x1.4; BTC/IRT's book is
        # sparse, so most grid points anchor to the same few levels.
        grid_min_perc=0.0005,
        grid_ratio=1.4,
        grid_max_perc=0.02,
        skew_perc=0.002,
        one_sided_lo=0.1,
        one_sided_hi=0.9,
        cost_basis_reference=True,
        ev_min=0.00001,
        adverse_selection_perc=0.0,
        hysteresis_ticks=5,
        refresh_seconds=300.0,
        requote_band_perc=0.0003,
        trend_window_seconds=600.0,
        trend_pause_perc=0.003,
        fill_table_enabled=True,
        fill_table_max_age_hours=48.0,
        fill_table_min_samples=50,
        fill_kappa=400.0,
        fill_lambda_tau=1.0,
        fill_queue_volume_ref=0.0,
        # Which algorithm runs (admin-selectable): "algo1" or "algo3".
        algorithm="algo1",
        algo1_enabled=True,
        algo3_enabled=True,
        algo4_enabled=True,
        # Algorithm 3 (strategy/algo3.py); beta stays 0 until it is estimated.
        algo3_depth_k=3,
        algo3_decay_rho=0.5,
        algo3_beta=0.0,
        algo3_half_spread=0.0,
        algo3_min_volume_orders=20.0,
        algo3_imbalance_clip=0.8,
        # Algorithm 4 (strategy/algo4.py, shown as MM_algo3).
        algo4_use_alpha=True,
        algo4_window_seconds=1800.0,
        algo4_sample_seconds=10.0,
        algo4_horizon_seconds=0.0,
        algo4_halflife_min_seconds=60.0,
        algo4_halflife_max_seconds=900.0,
        algo4_kappa=400.0,
        algo4_risk_perc=0.0,
        algo4_lean_clip=0.005,
        algo4_sigma_pause=0.0,
        # The demo matcher's "level-1 fast fill" assumed a level-1 quote sat
        # at the touch; v2 quotes by distance, so fills come from the price
        # path only (price_touch), consistent with the fill model.
        demo_level1_fast_fill_enabled=False,
        # Per-round decision diagnostics for the dashboard's Quote-decision tab
        # (bounded to the newest algo1_diagnostics_keep_rounds documents).
        algo1_diagnostics_enabled=True,
        url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
        url_send_order="https://apiv2.nobitex.ir/market/orders/add",
        output_csv=os.path.join(RESULTS_DIR, "btc_track.csv"),
    ),
    "usdt": dict(
        commission_perc=0.0010,
        # Matches commission_perc, i.e. today's actual bootstrap default --
        # see the "btc" profile's exp_ret_min comment. USDT/IRT's spread
        # rarely clears the resulting 2x-commission bar (see
        # reports/live-session-audit-2026-08-10.md); lower this from Admin
        # if more frequent USDT quoting is wanted. Left unchanged here so
        # this commit does not itself alter trading behavior.
        exp_ret_min=0.0002,
        cancel_after_minutes=3,
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
        buy_locked_cash_limit_enabled=False,
        buy_locked_cash_limit_ratio=0.8,
        sell_locked_inventory_limit_enabled=False,
        sell_locked_inventory_limit_ratio=0.8,
        demo_queue_enabled=False,
        demo_max_fill_per_observation_orders=1.0,
        demo_adverse_fill_enabled=False,
        demo_adverse_fill_probability=1.0,
        demo_non_adverse_fill_probability=1.0,
        # Linear inventory sizing (engine._inventory_sized_order_targets):
        # buy = 2*order_size*(1 - x), sell = 2*order_size*x, with x the
        # position in the [inv_min, inv_max] band. strength is the slope k
        # (0 = fixed size), min_fraction a floor on each side (0 = none).
        inventory_sizing_enabled=True,
        inventory_sizing_strength=1.0,
        inventory_sizing_min_fraction=0.0,
        # Algorithm 1 v2 quote selection (strategy/algo1.py); same defaults
        # as BTC, see the comment there.
        grid_min_perc=0.0005,
        grid_ratio=1.4,
        grid_max_perc=0.02,
        skew_perc=0.002,
        one_sided_lo=0.1,
        one_sided_hi=0.9,
        cost_basis_reference=True,
        ev_min=0.00001,
        adverse_selection_perc=0.0,
        hysteresis_ticks=5,
        refresh_seconds=300.0,
        requote_band_perc=0.0003,
        trend_window_seconds=600.0,
        trend_pause_perc=0.003,
        fill_table_enabled=True,
        fill_table_max_age_hours=48.0,
        fill_table_min_samples=50,
        fill_kappa=400.0,
        fill_lambda_tau=1.0,
        fill_queue_volume_ref=0.0,
        # Which algorithm runs (admin-selectable): "algo1" or "algo3".
        algorithm="algo1",
        algo1_enabled=True,
        algo3_enabled=True,
        algo4_enabled=True,
        # Algorithm 3 (strategy/algo3.py); beta stays 0 until it is estimated.
        algo3_depth_k=3,
        algo3_decay_rho=0.5,
        algo3_beta=0.0,
        algo3_half_spread=0.0,
        algo3_min_volume_orders=20.0,
        algo3_imbalance_clip=0.8,
        # Algorithm 4 (strategy/algo4.py, shown as MM_algo3).
        algo4_use_alpha=True,
        algo4_window_seconds=1800.0,
        algo4_sample_seconds=10.0,
        algo4_horizon_seconds=0.0,
        algo4_halflife_min_seconds=60.0,
        algo4_halflife_max_seconds=900.0,
        algo4_kappa=400.0,
        algo4_risk_perc=0.0,
        algo4_lean_clip=0.005,
        algo4_sigma_pause=0.0,
        demo_level1_fast_fill_enabled=False,
        # Per-round decision diagnostics for the dashboard's Quote-decision tab
        # (bounded to the newest algo1_diagnostics_keep_rounds documents).
        algo1_diagnostics_enabled=True,
        url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
        url_send_order="https://apiv2.nobitex.ir/market/orders/add",
        output_csv=os.path.join(RESULTS_DIR, "usdt_track.csv"),
    ),
}
