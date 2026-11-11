"""Algorithm 4: market making with a mean-reverting signal (stochastic control).

Shown as MM_algo3 (the branch it was built on). Specification:
docs/MM_algo3.md (from the design note "Algorithm 4 -- Market
Making with a Mean-Reverting Signal", the Avellaneda-Stoikov /
Cartea-Jaimungal family).

Decision logic only, like Algorithms 1 and 3: reads ``cfg``, a plain
order-book dict and the current inventory. Unlike them it is *stateful*: it
keeps a window of recent mid-prices (the engine passes neither history nor
time) and reads its own clock. Its only file access is the fill table, read
through the project's FillModel, which gives the fill-intensity decay
``kappa``.

What it does, per round:

* appends the mid to its window (one sample per ``algo4_sample_seconds``,
  the window ``algo4_window_seconds`` long) and fits the Ornstein-Uhlenbeck
  price model ``dS = theta (mu - S) dt + sigma dW`` to it by the AR(1)
  regression ``S_{k+1} = a + b S_k + e``: ``theta = -ln b / dt``,
  ``mu = a / (1 - b)``, half-life ``ln 2 / theta``;
* uses the forecast only when it is admissible: ``0 < b < 1``, the unit
  root rejected (t-statistic of ``b - 1`` below -2.86, the Dickey-Fuller 5 %
  critical value) and the half-life inside
  ``[algo4_halflife_min_seconds, algo4_halflife_max_seconds]``; otherwise
  the lean is zero and the quotes are the Avellaneda-Stoikov ones;
* lean ``m = (mu - S)(1 - e^{-theta tau}) / S`` over the horizon ``tau`` (the
  order lifetime by default), clipped to ``algo4_lean_clip`` of the price:
  price below its mean -> bid closer, ask farther;
* depths ``delta+ = 1/kappa + m - skew + risk`` and
  ``delta- = 1/kappa - m + skew + risk`` (relative to the price), the
  inventory skew ``skew_perc (2x - 1)`` and the risk widening
  ``algo4_risk_perc`` both sides; then the fee shift ``+ f`` on each side;
* prices ``A = S (1 + delta+)``, ``B = S (1 - delta-)``, clipped to the
  touch, rounded outward to the tick, and widened symmetrically until the
  round trip clears the fee bar ``R_net > exp_ret_min``;
* a volatility guard (no quote while the per-sample residual exceeds
  ``algo4_sigma_pause`` of the price) and the same hysteresis as the other
  algorithms (the resting pair is kept inside the requote band).

During the warm-up (fewer than 30 samples) it quotes without the forecast
instead of staying silent, so a restarted worker is back in the book at
once.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from config import MMConfig
from fill_model import FillModel
from strategy.base import Strategy
from strategy.common import QuoteArithmetic

MIN_POINTS = 30             # samples before the regression is attempted
# The unit root is rejected below this t-statistic of b - 1: the Dickey-Fuller
# 5 % critical value with a constant (large sample). The design note wrote
# "-2"; measured on simulated random walks of 180 samples that loose rule
# admits about a third of them as mean-reverting, -2.86 about one in twenty
# (tests/test_strategy_algo4.py: 60 walks, half-life floor 30 s); 200 walks with
# the 60-s floor give 25.5 % and 4.5 % (docs/MM_algo3.md section 4.5).
T_STAT_MAX = -2.86


@dataclass
class OUEstimate:
    """The AR(1) fit of the window and what the admissibility rule made of it."""
    ok: bool                            # the forecast may be used
    reason: str                         # admissible | warming_up | degenerate | unit_root | negative_autocorrelation | half_life_short | half_life_long
    points: int = 0
    window_seconds: float = 0.0
    dt_mean: float = 0.0                # mean spacing of the samples (s)
    b: Optional[float] = None
    a: Optional[float] = None
    sigma_eps: Optional[float] = None   # per-sample residual sd (price units)
    t_stat: Optional[float] = None      # of b - 1
    theta: Optional[float] = None       # reversion speed (1/s)
    mu: Optional[float] = None          # long-run mean (price units)
    sigma: Optional[float] = None       # price units per sqrt(second)
    half_life: Optional[float] = None   # seconds


def ou_fit(samples: Sequence[Tuple[float, float]], *, halflife_min: float, halflife_max: float,
           min_points: int = MIN_POINTS, t_stat_max: float = T_STAT_MAX) -> OUEstimate:
    """Ordinary least squares of S_{k+1} on S_k over the (time, mid) samples,
    then the OU parameters (docs/MM_algo3.md sections 4.3 to 4.5). Pure."""
    n = len(samples)
    if n < max(3, int(min_points)):
        return OUEstimate(False, "warming_up", points=n,
                          window_seconds=(samples[-1][0] - samples[0][0]) if n else 0.0)
    times = [float(t) for t, _ in samples]
    prices = [float(s) for _, s in samples]
    window = times[-1] - times[0]
    dt = window / (n - 1)
    if dt <= 0 or any(p <= 0 for p in prices):
        return OUEstimate(False, "degenerate", points=n, window_seconds=window)
    x, y = prices[:-1], prices[1:]
    m = n - 1
    xbar, ybar = sum(x) / m, sum(y) / m
    sxx = sum((xi - xbar) ** 2 for xi in x)
    sxy = sum((xi - xbar) * (yi - ybar) for xi, yi in zip(x, y))
    if sxx <= 0:
        return OUEstimate(False, "degenerate", points=n, window_seconds=window, dt_mean=dt)
    b = sxy / sxx
    a = ybar - b * xbar
    ssr = sum((yi - a - b * xi) ** 2 for xi, yi in zip(x, y))
    dof = m - 2
    s2 = ssr / dof if dof > 0 else 0.0
    sigma_eps = math.sqrt(max(0.0, s2))
    se_b = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    if se_b > 0:
        t_stat = (b - 1.0) / se_b
    else:
        t_stat = -math.inf if b < 1.0 else math.inf
    estimate = OUEstimate(False, "", points=n, window_seconds=window, dt_mean=dt,
                          b=b, a=a, sigma_eps=sigma_eps, t_stat=t_stat)
    if b >= 1.0:
        estimate.reason = "unit_root"
        return estimate
    if b <= 0.0:
        estimate.reason = "negative_autocorrelation"
        return estimate
    theta = -math.log(b) / dt
    estimate.theta = theta
    estimate.mu = a / (1.0 - b)
    estimate.half_life = math.log(2.0) / theta
    estimate.sigma = math.sqrt(s2 * 2.0 * theta / (1.0 - b * b)) if s2 > 0 else 0.0
    if t_stat > t_stat_max:
        estimate.reason = "unit_root"
    elif estimate.half_life < halflife_min:
        estimate.reason = "half_life_short"
    elif estimate.half_life > halflife_max:
        estimate.reason = "half_life_long"
    else:
        estimate.ok, estimate.reason = True, "admissible"
    return estimate


@dataclass
class Algo4Evaluation:
    """Everything one round's decision is made of; built once, read by
    ``decide_order``, ``diagnostics`` and ``no_quote_reason``."""
    ok: bool
    reason: str
    mid: Optional[float] = None
    spread: Optional[float] = None
    last: Optional[float] = None
    estimate: Optional[OUEstimate] = None
    forecast_used: bool = False
    lean_raw: Optional[float] = None    # m_tau before the clip (price units)
    lean: float = 0.0                   # m_tau / S after the clip
    lean_clipped: bool = False
    horizon_seconds: float = 0.0
    sigma_rel: Optional[float] = None   # sigma_eps / S
    position: Optional[float] = None
    skew: float = 0.0                   # skew_perc (2x - 1)
    risk: float = 0.0                   # algo4_risk_perc
    kappa: float = 0.0                  # per unit of relative depth
    kappa_source: str = "config"
    fill_lambda: Optional[float] = None
    base_depth: float = 0.0             # 1 / kappa
    fee: float = 0.0
    depth_bid: float = 0.0              # delta- / S after the fee shift
    depth_ask: float = 0.0              # delta+ / S after the fee shift
    centre: Optional[float] = None
    raw_bid: Optional[float] = None
    raw_ask: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    clipped_bid: bool = False
    clipped_ask: bool = False
    widening: float = 0.0               # w / S added to each side for the fee bar
    bid_level: Optional[int] = None
    ask_level: Optional[int] = None
    gross_return: Optional[float] = None
    net_return: Optional[float] = None
    relative_quoted_spread: Optional[float] = None
    levels: List[Dict[str, float]] = field(default_factory=list)


class Algo4Strategy(QuoteArithmetic, Strategy):
    """Mean-reverting-signal market maker (Algorithm 4, shown as MM_algo3)."""

    key = "algo4"

    def __init__(self, cfg: MMConfig, *, clock=None, fill_model: Optional[FillModel] = None):
        self.cfg = cfg
        self.clock = clock or time.monotonic
        self.fill_model = fill_model or FillModel(cfg)
        self._samples: Deque[Tuple[float, float]] = deque()
        self._estimate: Optional[OUEstimate] = None
        # Hysteresis memory: the resting pair, (bid, ask, issued_at).
        self._resting: Optional[Tuple[float, float, float]] = None

    # ---------- Parameters ----------
    def _setting(self, name: str, default: float) -> float:
        try:
            value = float(getattr(self.cfg, name, default))
        except (TypeError, ValueError):
            return float(default)
        return value if math.isfinite(value) else float(default)

    def window_seconds(self) -> float:
        return max(0.0, self._setting("algo4_window_seconds", 1800.0))

    def sample_seconds(self) -> float:
        return max(0.0, self._setting("algo4_sample_seconds", 10.0))

    def horizon_seconds(self) -> float:
        """tau: ``algo4_horizon_seconds``, or the order lifetime when 0."""
        horizon = self._setting("algo4_horizon_seconds", 0.0)
        if horizon > 0:
            return horizon
        return max(0.0, self._setting("cancel_after_minutes", 3.0) * 60.0)

    def halflife_bounds(self) -> Tuple[float, float]:
        return (max(0.0, self._setting("algo4_halflife_min_seconds", 60.0)),
                max(0.0, self._setting("algo4_halflife_max_seconds", 900.0)))

    def use_alpha(self) -> bool:
        return bool(getattr(self.cfg, "algo4_use_alpha", True))

    def lean_clip(self) -> float:
        return max(0.0, self._setting("algo4_lean_clip", 0.005))

    def risk_widening(self) -> float:
        return max(0.0, self._setting("algo4_risk_perc", 0.0))

    def sigma_pause(self) -> float:
        return max(0.0, self._setting("algo4_sigma_pause", 0.0))

    def skew(self) -> float:
        return self._setting("skew_perc", 0.0)

    def configured_kappa(self) -> float:
        return max(0.0, self._setting("algo4_kappa", 400.0))

    def kappa(self) -> Tuple[float, str, Optional[float]]:
        """(kappa, source, lambda): the fill-intensity decay per unit of
        relative depth from the fresh fill table (docs/MM_algo3.md 6.1) when it
        can be fitted, else ``algo4_kappa``."""
        intensity = None
        try:
            intensity = self.fill_model.intensity()
        except Exception:  # noqa: BLE001 -- a broken table must not stop the round
            intensity = None
        if intensity and intensity.get("kappa", 0.0) > 0:
            return float(intensity["kappa"]), "table", float(intensity.get("lambda") or 0.0) or None
        return self.configured_kappa(), "config", None

    # ---------- State ----------
    def observe(self, mid: float, now: Optional[float] = None) -> bool:
        """Append a (time, mid) sample when ``algo4_sample_seconds`` have
        passed since the last one; drop samples older than the window; refit.
        Returns True when a sample was taken. Called by decide_order only."""
        now = float(self.clock()) if now is None else float(now)
        if not (mid and math.isfinite(mid) and mid > 0):
            return False
        if self._samples and now - self._samples[-1][0] < self.sample_seconds():
            return False
        self._samples.append((now, float(mid)))
        window = self.window_seconds()
        while self._samples and now - self._samples[0][0] > window:
            self._samples.popleft()
        self._estimate = None
        return True

    def estimate(self) -> OUEstimate:
        if self._estimate is None:
            low, high = self.halflife_bounds()
            self._estimate = ou_fit(list(self._samples), halflife_min=low, halflife_max=high)
        return self._estimate

    def reset_memory(self) -> None:
        """Forget the resting pair (new session); the price window stays."""
        self._resting = None

    # ---------- The decision ----------
    def evaluate(self, ob: Dict[str, Any], inv_current: float) -> Algo4Evaluation:
        """The steps of docs/MM_algo3.md sections 4.6 to 6.4 on the current window. Pure: no
        sample is taken, no hysteresis."""
        mid = self.mid_price(ob)
        if mid is None:
            return Algo4Evaluation(False, "invalid_or_single_level_order_book")
        try:
            bids = [(float(p), float(v)) for p, v in ob["bids"][: self.depth_cap(ob) + 1]]
            asks = [(float(p), float(v)) for p, v in ob["asks"][: self.depth_cap(ob) + 1]]
        except (KeyError, TypeError, ValueError):
            return Algo4Evaluation(False, "invalid_or_single_level_order_book")
        if not bids or not asks or any(p <= 0 for p, _ in bids) or any(p <= 0 for p, _ in asks):
            return Algo4Evaluation(False, "invalid_or_single_level_order_book")
        best_bid, best_ask = bids[0][0], asks[0][0]
        epsilon = self.tick_size()
        try:
            last = float(ob.get("lastTradePrice")) if ob.get("lastTradePrice") is not None else None
        except (TypeError, ValueError):
            last = None

        estimate = self.estimate()
        tau = self.horizon_seconds()
        evaluation = Algo4Evaluation(True, "", mid=mid, spread=best_ask - best_bid, last=last,
                                     estimate=estimate, horizon_seconds=tau,
                                     levels=[{"level": i, "bid": bids[i][0], "bid_volume": bids[i][1],
                                              "ask": asks[i][0], "ask_volume": asks[i][1]}
                                             for i in range(min(len(bids), len(asks), 5))])
        # Volatility guard: the per-sample residual against the price.
        if estimate.sigma_eps is not None:
            evaluation.sigma_rel = estimate.sigma_eps / mid
            pause = self.sigma_pause()
            if pause > 0 and evaluation.sigma_rel > pause:
                evaluation.ok, evaluation.reason = False, "volatility_pause"
                return evaluation
        # The lean: the expected reversion over the horizon, when admissible.
        lean = 0.0
        if self.use_alpha() and estimate.ok and estimate.mu is not None and estimate.theta is not None:
            raw = (estimate.mu - mid) * (1.0 - math.exp(-estimate.theta * tau))
            clip = self.lean_clip() * mid
            lean_price = max(-clip, min(clip, raw))
            evaluation.lean_raw, evaluation.lean_clipped = raw, abs(raw) > clip
            evaluation.forecast_used = True
            lean = lean_price / mid
        evaluation.lean = lean

        x = self.inventory_position(inv_current)
        skew = self.skew() * (2.0 * x - 1.0)
        risk = self.risk_widening()
        kappa, source, fill_lambda = self.kappa()
        fee = float(self.cfg.commission_perc)
        evaluation.position, evaluation.skew, evaluation.risk = x, skew, risk
        evaluation.kappa, evaluation.kappa_source, evaluation.fill_lambda = kappa, source, fill_lambda
        evaluation.fee = fee
        if kappa <= 0:
            evaluation.ok, evaluation.reason = False, "invalid_kappa"
            return evaluation
        base = 1.0 / kappa
        evaluation.base_depth = base
        tick_rel = epsilon / mid
        # The first-order optimal depths with the fee shift (docs/MM_algo3.md 5.3, 5.6, 6.2),
        # relative to the price; each depth at least one tick before the fee.
        depth_ask = max(tick_rel, base + lean - skew + risk) + fee
        depth_bid = max(tick_rel, base - lean + skew + risk) + fee
        evaluation.depth_ask, evaluation.depth_bid = depth_ask, depth_bid
        raw_ask, raw_bid = mid * (1.0 + depth_ask), mid * (1.0 - depth_bid)
        evaluation.centre = (raw_ask + raw_bid) / 2.0
        max_bid, min_ask = round(best_ask - epsilon, 8), round(best_bid + epsilon, 8)
        bid = self._round_down(min(raw_bid, max_bid))
        ask = self._round_up(max(raw_ask, min_ask))
        evaluation.raw_bid, evaluation.raw_ask = raw_bid, raw_ask
        evaluation.clipped_bid, evaluation.clipped_ask = raw_bid > max_bid, raw_ask < min_ask
        # The fee bar on the prices sent (docs/MM_algo3.md 6.4): widen both sides by the same amount.
        gross = self.gross_bar()
        if not math.isfinite(gross):
            evaluation.ok, evaluation.reason = False, "half_spread_not_attainable"
            return evaluation
        if bid > 0 and ask / bid <= 1.0 + gross:
            w = ((1.0 + gross) * bid - ask) / (2.0 + gross)
            w = max(w, 0.0) + epsilon
            bid = self._round_down(bid - w)
            ask = self._round_up(ask + w)
            evaluation.widening = w / mid
        evaluation.bid, evaluation.ask = bid, ask
        evaluation.gross_return = self.gross_return(bid, ask)
        evaluation.net_return = self.net_return(bid, ask)
        evaluation.relative_quoted_spread = (ask - bid) / mid
        if bid <= 0 or bid >= ask or evaluation.net_return is None or evaluation.net_return <= self.min_required_return():
            evaluation.ok, evaluation.reason = False, "net_return_below_bar"
            return evaluation
        evaluation.bid_level = max((i for i, (p, _) in enumerate(bids) if p >= bid), default=0)
        evaluation.ask_level = max((j for j, (p, _) in enumerate(asks) if p <= ask), default=0)
        evaluation.reason = "mean_reversion_pair" if evaluation.forecast_used else "symmetric_pair"
        return evaluation

    # ---------- Hysteresis ----------
    def _apply_hysteresis(self, bid: float, ask: float) -> Tuple[float, float, bool]:
        """Keep the resting pair while both new prices are within the requote
        band of it and it is younger than refresh_seconds (as Algorithm 3)."""
        refresh = float(getattr(self.cfg, "refresh_seconds", 0.0) or 0.0)
        now = float(self.clock())
        previous = self._resting
        if (
            previous is not None
            and self.requote_band(previous[0]) > 0
            and abs(bid - previous[0]) <= self.requote_band(previous[0]) + 1e-9
            and abs(ask - previous[1]) <= self.requote_band(previous[1]) + 1e-9
            and now - previous[2] < refresh
        ):
            net = self.net_return(previous[0], previous[1])
            if net is not None and net > self.min_required_return():
                return previous[0], previous[1], True
        self._resting = (bid, ask, now)
        return bid, ask, False

    # ---------- Strategy interface ----------
    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        mid = self.mid_price(ob)
        if mid is not None:
            self.observe(mid)
        evaluation = self.evaluate(ob, inv_current)
        if not evaluation.ok:
            self._resting = None
            return None
        bid, ask, kept = self._apply_hysteresis(evaluation.bid, evaluation.ask)
        estimate = evaluation.estimate
        fee = float(self.cfg.commission_perc)
        return {
            "bid": bid,
            "ask": ask,
            "bid level": evaluation.bid_level,
            "ask level": evaluation.ask_level,
            "lastTradePrice": evaluation.last,
            "mid price": mid,
            "centre": evaluation.centre,
            "inventory position": evaluation.position,
            "relative quoted spread": self.relative_quoted_spread(ob, bid, ask),
            "gross return": self.gross_return(bid, ask),
            "net return": self.net_return(bid, ask),
            "buy fee": fee,
            "sell fee": fee,
            "min required return": self.min_required_return(),
            "bid spread": mid - bid,
            "ask spread": ask - mid,
            "bid distance": (evaluation.centre - bid) / evaluation.centre,
            "ask distance": (ask - evaluation.centre) / evaluation.centre,
            "bid exec prob": None,
            "ask exec prob": None,
            "expected value": None,
            "quote mode": "pair",
            "selection reason": evaluation.reason + ("_kept" if kept else ""),
            "reference price": None,
            "imbalance": None,
            "microprice": None,
            "lean": evaluation.lean,
            "mu hat": estimate.mu if estimate else None,
            "theta hat": estimate.theta if estimate else None,
            "half life": estimate.half_life if estimate else None,
            "forecast admissible": bool(estimate.ok) if estimate else False,
            "kappa": evaluation.kappa,
            "bid depth": evaluation.depth_bid,
            "ask depth": evaluation.depth_ask,
            "bid kept": kept,
            "ask kept": kept,
        }

    def diagnostics(
        self, ob: Dict[str, Any], inv_current: float, *, round_no: int, session_id,
    ) -> Dict[str, Any]:
        """Describe the decision without changing the strategy's memory (no
        sample is taken, the resting pair is untouched). The keys the engine,
        Telegram and the admin panel read from Algorithm 1 documents are kept,
        with the one evaluated pair as the only candidate."""
        evaluation = self.evaluate(ob, inv_current)
        estimate = evaluation.estimate or self.estimate()
        min_required = self.min_required_return()
        fee = float(self.cfg.commission_perc)
        mid = evaluation.mid
        candidate_results = []
        if evaluation.bid is not None and evaluation.ask is not None:
            candidate_results.append({
                "bid_level": evaluation.bid_level, "ask_level": evaluation.ask_level,
                "bid_price": evaluation.raw_bid, "ask_price": evaluation.raw_ask,
                "submitted_bid_price": evaluation.bid, "submitted_ask_price": evaluation.ask,
                "last_price": evaluation.last, "mid_price": mid,
                "spread_ratio": evaluation.relative_quoted_spread,
                "relative_quoted_spread": evaluation.relative_quoted_spread,
                "gross_return": evaluation.gross_return, "net_return": evaluation.net_return,
                "p_bid": None, "p_ask": None, "expected_value": None,
                "buy_fee": fee, "sell_fee": fee, "commission_perc": fee,
                "exp_ret_min": float(min_required), "required_threshold": float(min_required),
                "min_required_return": float(min_required),
                "passed": bool(evaluation.ok),
            })
        resting = self._resting
        samples = list(self._samples)
        step = max(1, len(samples) // 60)
        low, high = self.halflife_bounds()
        return {
            "symbol": self.cfg.pair,
            "session_id": str(session_id),
            "round": int(round_no),
            "algorithm": self.key,
            "algorithm_version": 4,
            "inventory": float(inv_current) if inv_current is not None else None,
            "inv_min": float(self.cfg.inv_min),
            "inv_max": float(self.cfg.inv_max),
            "inventory_position": evaluation.position if evaluation.position is not None else self.inventory_position(inv_current),
            "mid_price": mid,
            "spread": evaluation.spread,
            "last_price": evaluation.last,
            "levels": evaluation.levels,
            "window": {
                "points": estimate.points, "seconds": estimate.window_seconds, "dt_mean": estimate.dt_mean,
                "window_seconds": self.window_seconds(), "sample_seconds": self.sample_seconds(),
                "samples": [[round(t - samples[-1][0], 1), s] for t, s in samples[::step]] if samples else [],
            },
            "estimate": {
                "ok": estimate.ok, "reason": estimate.reason, "b": estimate.b, "a": estimate.a,
                "sigma_eps": estimate.sigma_eps, "t_stat": (None if estimate.t_stat is None or not math.isfinite(estimate.t_stat) else estimate.t_stat),
                "theta": estimate.theta, "mu": estimate.mu, "sigma": estimate.sigma, "half_life": estimate.half_life,
                "halflife_min_seconds": low, "halflife_max_seconds": high, "t_stat_max": T_STAT_MAX,
                "use_alpha": self.use_alpha(),
            },
            "mu_shift": (estimate.mu / mid - 1.0) if (estimate.mu and mid) else None,
            "horizon_seconds": evaluation.horizon_seconds,
            "forecast_used": evaluation.forecast_used,
            "lean_raw": evaluation.lean_raw,
            "lean": evaluation.lean,
            "lean_clip": self.lean_clip(),
            "lean_clipped": evaluation.lean_clipped,
            "sigma_rel": evaluation.sigma_rel,
            "sigma_pause": self.sigma_pause(),
            "skew_perc": self.skew(),
            "skew": evaluation.skew,
            "risk_perc": evaluation.risk,
            "kappa": evaluation.kappa,
            "kappa_source": evaluation.kappa_source,
            "kappa_config": self.configured_kappa(),
            "fill_lambda": evaluation.fill_lambda,
            "base_depth": evaluation.base_depth,
            "depth_bid": evaluation.depth_bid,
            "depth_ask": evaluation.depth_ask,
            "centre": evaluation.centre,
            "centre_shift": (evaluation.centre / mid - 1.0) if (evaluation.centre and mid) else None,
            "commission_perc": fee,
            "exp_ret_min": float(min_required),
            "required_threshold": float(min_required),
            "min_required_return": float(min_required),
            "gross_bar": self.gross_bar(),
            "widening": evaluation.widening,
            "raw_bid": evaluation.raw_bid,
            "raw_ask": evaluation.raw_ask,
            "clipped_bid": evaluation.clipped_bid,
            "clipped_ask": evaluation.clipped_ask,
            "net_return": evaluation.net_return,
            "gross_return": evaluation.gross_return,
            "relative_quoted_spread": evaluation.relative_quoted_spread,
            "candidate_results": candidate_results,
            "single_results": [],
            "quote_mode": "pair" if evaluation.ok else "none",
            "selected_bid_level": evaluation.bid_level if evaluation.ok else None,
            "selected_ask_level": evaluation.ask_level if evaluation.ok else None,
            "selected_bid_price": evaluation.bid if evaluation.ok else None,
            "selected_ask_price": evaluation.ask if evaluation.ok else None,
            "selected_expected_value": None,
            "reference_price": None,
            "selection_reason": evaluation.reason,
            "fallback_detected": False,
            "hysteresis": {
                "ticks": int(getattr(self.cfg, "hysteresis_ticks", 0) or 0),
                "refresh_seconds": float(getattr(self.cfg, "refresh_seconds", 0.0) or 0.0),
                "resting_bid": resting[0] if resting else None,
                "resting_ask": resting[1] if resting else None,
                "resting_age_seconds": (float(self.clock()) - resting[2]) if resting else None,
            },
        }

    def no_quote_reason(self, ob: Dict[str, Any], inv_current: float) -> str:
        evaluation = self.evaluate(ob, inv_current)
        if evaluation.ok:
            return "quote_available"
        return evaluation.reason
