"""The quoting algorithms this project can run, in one place.

``run.py`` builds the worker's strategy from here, the engine swaps the
strategy when the Demo configuration's ``algorithm`` field changes, the
admin panel offers the selectable keys, and the dashboard labels rounds and
sessions with them. Adding an algorithm means one ``AlgorithmInfo`` entry
plus its ``strategy/<key>.py``.

Names: the dashboard and the admin show each algorithm under the name of the
git branch it was built on (``MM_algo1v2`` = Algorithm 1 version 2,
``MM_algo2`` = Algorithm 3 of the design documents, ``MM_algo3`` = Algorithm 4
of the design documents); the configuration, the command line and the
recorded rounds keep the short keys ``algo1`` / ``algo3`` / ``algo4``, so saved
settings and history stay valid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from strategy.algo1 import Algo1Strategy
from strategy.algo2 import Algo2Strategy
from strategy.algo3 import Algo3Strategy
from strategy.algo4 import Algo4Strategy


@dataclass(frozen=True)
class AlgorithmInfo:
    key: str                 # config value, CLI --algorithm, "Strategy" column of every round row
    number: int              # the number in the project's design documents ("Algorithm 3")
    label: str               # display name on the dashboard and in the admin: the git branch it was built on
    title: str               # one-line description, shown after the name
    cls: type                # the Strategy class
    implemented: bool
    doc: str                 # where it is specified
    summary: Tuple[str, ...] # what it does, for the dashboard's Algorithm tab
    fields: Tuple[str, ...]  # the config fields that belong to it alone
    uses_fill_model: bool


ALGORITHMS: Mapping[str, AlgorithmInfo] = {
    "algo1": AlgorithmInfo(
        key="algo1", number=1, label="MM_algo1v2",
        title="Algorithm 1, version 2: distance grid, fill probabilities and expected value",
        cls=Algo1Strategy, implemented=True, doc="https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/docs/MM_algo1v2.md",
        summary=(
            "Moves a quote centre away from the mid as the inventory fills: c = mid · (1 − skew · (2x − 1)).",
            "Builds candidate bids and asks on a geometric grid of distances from the centre, each one tick inside a real book level.",
            "Reads every candidate's fill probability from the measured fill table (formula fallback).",
            "Scores every bid/ask pair by expected value J = p_bid · p_ask · R_net − η · P(one leg) and sends the best pair that clears the fee bar R_net > exp_ret_min and the floor ev_min.",
            "Quotes one side only at the band edges or when only one leg is profitable against its reference price; keeps a resting price within hysteresis_ticks.",
        ),
        fields=("grid_min_perc", "grid_ratio", "grid_max_perc", "skew_perc", "one_sided_lo", "one_sided_hi",
                "cost_basis_reference", "ev_min", "adverse_selection_perc", "hysteresis_ticks", "refresh_seconds",
                "fill_table_enabled", "fill_table_max_age_hours", "fill_table_min_samples",
                "fill_kappa", "fill_lambda_tau", "fill_queue_volume_ref"),
        uses_fill_model=True,
    ),
    "algo3": AlgorithmInfo(
        key="algo3", number=3, label="MM_algo2",
        title="order-book imbalance, microprice and a fixed spread",
        cls=Algo3Strategy, implemented=True, doc="https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo2/docs/MM_algo2.md",
        summary=(
            "Measures the imbalance of the resting volumes in the top k levels, I = (V_bid − V_ask) / (V_bid + V_ask), deeper levels weighted ρ^i.",
            "Starts the centre at the microprice, the mid pulled toward the thinner side: (a₀·v_bid₀ + b₀·v_ask₀) / (v_bid₀ + v_ask₀).",
            "Leans the centre with the imbalance (× (1 + β·I)) and skews it against the inventory (× (1 − skew · (2x − 1))). With algo3_beta = 0, the default, the imbalance is recorded and charted but does not move the quotes.",
            "Quotes one symmetric half-spread h = max(h_config, h_min + one tick) around the centre, with h_min = G / (2 + G) the smallest half-spread that clears the fee bar.",
            "Rounds the bid down and the ask up to the tick without crossing the touch, re-checks R_net > exp_ret_min on the final prices, and keeps the resting pair within hysteresis_ticks. Fill probabilities are not used.",
        ),
        fields=("algo3_depth_k", "algo3_decay_rho", "algo3_beta", "algo3_half_spread",
                "algo3_min_volume_orders", "algo3_imbalance_clip", "skew_perc",
                "hysteresis_ticks", "refresh_seconds"),
        uses_fill_model=False,
    ),
    "algo4": AlgorithmInfo(
        key="algo4", number=4, label="MM_algo3",
        title="mean-reverting signal and optimal quotes (stochastic control)",
        cls=Algo4Strategy, implemented=True, doc="https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo3/docs/MM_algo3.md",
        summary=(
            "Keeps a window of recent mids and fits the Ornstein–Uhlenbeck model dS = θ(μ − S)dt + σdW to it by the AR(1) regression S_{k+1} = a + b·S_k: θ = −ln b / Δt, μ = a / (1 − b), half-life ln 2 / θ.",
            "Uses the forecast only when it is admissible: 0 < b < 1, the unit root rejected (t-statistic of b − 1 at most −2.86, the Dickey–Fuller 5 % value) and the half-life inside the configured bounds; otherwise the lean is zero and the quotes are the Avellaneda–Stoikov ones.",
            "Leans the quotes by the expected reversion over the horizon, m = (μ − S)(1 − e^{−θτ}) / S, clipped to algo4_lean_clip: price below its mean → bid closer, ask farther.",
            "Depths from the first-order solution of the Hamilton–Jacobi–Bellman equation: δ± = 1/κ ± m ∓ skew + risk, with 1/κ the fee-free optimal half-spread (κ from the fill table when it can be fitted, else algo4_kappa), the inventory skew skew_perc·(2x − 1) and the risk widening algo4_risk_perc, each at least one tick; then the fee shift + f on each side.",
            "Prices S(1 + δ+) and S(1 − δ−), clipped to the touch, rounded outward to the tick and widened symmetrically until the round trip clears the fee bar R_net > exp_ret_min; a volatility guard and the shared hysteresis (requote band) as the other algorithms.",
        ),
        fields=("algo4_use_alpha", "algo4_window_seconds", "algo4_sample_seconds", "algo4_horizon_seconds",
                "algo4_halflife_min_seconds", "algo4_halflife_max_seconds", "algo4_kappa", "algo4_risk_perc",
                "algo4_lean_clip", "algo4_sigma_pause", "skew_perc", "hysteresis_ticks", "refresh_seconds",
                "fill_table_enabled", "fill_table_max_age_hours", "fill_table_min_samples"),
        uses_fill_model=True,
    ),
}

# What the CLI accepts: the implemented algorithms plus the Avellaneda-Stoikov
# stub (key algo2, unrelated to the name MM_algo2; kept so `--algorithm algo2`
# fails loudly, see strategy/algo2.py).
STRATEGIES: Dict[str, type] = {key: info.cls for key, info in ALGORITHMS.items()}
STRATEGIES["algo2"] = Algo2Strategy

# What the admin panel and the Demo/live configuration may select.
SELECTABLE_ALGORITHMS: Tuple[str, ...] = tuple(key for key, info in ALGORITHMS.items() if info.implemented)
DEFAULT_ALGORITHM = "algo1"


def algorithm_info(key: Optional[str]) -> AlgorithmInfo:
    """The catalog entry for a key; unknown or missing keys (rows written
    before the key existed) mean Algorithm 1."""
    return ALGORITHMS.get(str(key) if key else DEFAULT_ALGORITHM, ALGORITHMS[DEFAULT_ALGORITHM])


def algorithm_label(key: Optional[str]) -> str:
    return algorithm_info(key).label


def build_strategy(cfg: Any, key: Optional[str] = None):
    """The strategy ``cfg`` asks for (its ``algorithm`` field) or the one
    named explicitly; a key the catalog does not know raises KeyError."""
    chosen = key or getattr(cfg, "algorithm", None) or DEFAULT_ALGORITHM
    return STRATEGIES[chosen](cfg)


def strategy_key(strategy: Any) -> Optional[str]:
    """The catalog key of a strategy instance (None for a stand-in that is
    not from the catalog, e.g. a test double)."""
    return getattr(strategy, "key", None)
