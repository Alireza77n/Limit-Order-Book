"""Algorithm 3: order-book imbalance and microprice market maker.

Shown as MM_algo2 (the branch it was built on). Specification:
https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo2/docs/MM_algo2.md (from the design note
"Algorithm 3 -- Order-Book Imbalance and Microprice Market Maker").

Pure decision logic, like Algorithm 1: reads ``cfg``, a plain order-book
dict and the current inventory; no network, DB or file I/O. The engine calls
``decide_order`` once per round and sizes, caps and sends whatever comes
back; ``diagnostics`` and ``no_quote_reason`` describe the same decision for
the dashboard, Telegram and the admin panel.

What it does, per round:

* imbalance of the top ``k`` levels, deeper levels weighted ``rho^i``:
  ``I = (V_b - V_a) / (V_b + V_a)``, clipped to ``[-I_max, I_max]``, forced
  to 0 when the top of the book is thinner than ``V_min``;
* the microprice ``(a0 v_b0 + b0 v_a0) / (v_b0 + v_a0)`` = the mid shifted by
  half the spread times the level-0 imbalance;
* a quote centre ``c = microprice * (1 + beta I) * (1 - skew (2x - 1))``:
  leaned with the imbalance, skewed against the inventory;
* one symmetric half-spread ``h = max(h_cfg, h_min + tick/c)`` around it,
  where ``h_min = G / (2 + G)`` is the smallest half-spread whose round trip
  clears the fee bar ``R_net > exp_ret_min``;
* bid rounded down, ask rounded up to the tick, neither crossing the touch;
  the fee bar is re-checked on the final prices;
* hysteresis: the previous pair is kept while both prices are within
  ``hysteresis_ticks`` of it and it is younger than ``refresh_seconds``.

Algorithm 3 never looks at fill probabilities: the lean and the skew move the
centre only, so ``A/B = (1+h)/(1-h)`` and the round-trip return is fixed by
``h`` whatever the signal.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import MMConfig
from strategy.base import Strategy
from strategy.common import QuoteArithmetic


@dataclass
class Algo3Evaluation:
    """Everything one round's decision is made of; built once, read by
    ``decide_order``, ``diagnostics`` and ``no_quote_reason``."""
    ok: bool
    reason: str
    mid: Optional[float] = None
    spread: Optional[float] = None
    last: Optional[float] = None
    depth_k: int = 0
    decay_rho: float = 1.0
    levels: List[Dict[str, float]] = field(default_factory=list)
    volume_bid: float = 0.0
    volume_ask: float = 0.0
    min_volume: float = 0.0
    thin_book: bool = False
    imbalance_raw: float = 0.0
    imbalance: float = 0.0
    imbalance_level0: float = 0.0
    imbalance_clip: float = 1.0
    microprice: Optional[float] = None
    position: Optional[float] = None
    beta: float = 0.0
    skew_perc: float = 0.0
    lean_factor: float = 1.0
    skew_factor: float = 1.0
    centre: Optional[float] = None
    h_min: float = 0.0
    h_config: float = 0.0
    tick_fraction: float = 0.0
    h: float = 0.0
    raw_bid: Optional[float] = None
    raw_ask: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    clipped_bid: bool = False
    clipped_ask: bool = False
    bid_level: Optional[int] = None
    ask_level: Optional[int] = None
    gross_return: Optional[float] = None
    net_return: Optional[float] = None
    relative_quoted_spread: Optional[float] = None


class Algo3Strategy(QuoteArithmetic, Strategy):
    """Imbalance-leaned, microprice-centred, fixed-spread quoting (Algorithm 3)."""

    key = "algo3"

    def __init__(self, cfg: MMConfig, *, clock=None):
        self.cfg = cfg
        self.clock = clock or time.monotonic
        # Hysteresis memory: the resting pair, (bid, ask, issued_at).
        self._resting: Optional[Tuple[float, float, float]] = None

    # ---------- Parameters ----------
    def depth_k(self) -> int:
        return max(1, int(getattr(self.cfg, "algo3_depth_k", 3) or 1))

    def decay_rho(self) -> float:
        rho = float(getattr(self.cfg, "algo3_decay_rho", 1.0) or 0.0)
        return min(1.0, max(0.0, rho))

    def beta(self) -> float:
        return float(getattr(self.cfg, "algo3_beta", 0.0) or 0.0)

    def imbalance_clip(self) -> float:
        clip = float(getattr(self.cfg, "algo3_imbalance_clip", 1.0) or 0.0)
        return min(1.0, max(0.0, clip))

    def min_volume(self) -> float:
        """V_min in base units: ``algo3_min_volume_orders`` times the order
        size (0 = never treat the book as thin)."""
        multiple = float(getattr(self.cfg, "algo3_min_volume_orders", 0.0) or 0.0)
        size = float(self.cfg.order_size or 0.0)
        return max(0.0, multiple * size)

    def skew(self) -> float:
        return float(getattr(self.cfg, "skew_perc", 0.0) or 0.0)

    def configured_half_spread(self) -> float:
        return max(0.0, float(getattr(self.cfg, "algo3_half_spread", 0.0) or 0.0))

    def minimum_half_spread(self) -> float:
        """h_min = G / (2 + G): with B = c(1-h), A = c(1+h) the round trip
        clears the bar exactly when (1+h)/(1-h) > 1 + G, i.e. h > G/(2+G)."""
        gross = self.gross_bar()
        if not math.isfinite(gross):
            return 1.0
        return gross / (2.0 + gross)

    # ---------- Signals ----------
    @staticmethod
    def _parse_levels(ob: Dict[str, Any], count: int) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        bids = [(float(p), float(v)) for p, v in ob["bids"][:count]]
        asks = [(float(p), float(v)) for p, v in ob["asks"][:count]]
        return bids, asks

    def imbalance(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> Tuple[float, float, float]:
        """(V_b, V_a, I) over the top k levels with weights rho^i."""
        k, rho = self.depth_k(), self.decay_rho()
        volume_bid = sum((rho ** i) * bids[i][1] for i in range(min(k, len(bids))))
        volume_ask = sum((rho ** i) * asks[i][1] for i in range(min(k, len(asks))))
        total = volume_bid + volume_ask
        imbalance = (volume_bid - volume_ask) / total if total > 0 else 0.0
        return volume_bid, volume_ask, imbalance

    @staticmethod
    def microprice(bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> Optional[float]:
        """(a0 v_b0 + b0 v_a0) / (v_b0 + v_a0): the mid pulled toward the side
        with less resting volume; the mid itself when level 0 is empty."""
        (b0, vb0), (a0, va0) = bids[0], asks[0]
        if vb0 + va0 <= 0:
            return (a0 + b0) / 2.0
        return (a0 * vb0 + b0 * va0) / (vb0 + va0)

    # ---------- The decision ----------
    def evaluate(self, ob: Dict[str, Any], inv_current: float) -> Algo3Evaluation:
        """Steps 1-9 and 11 of the specification. Pure: no hysteresis."""
        mid = self.mid_price(ob)
        if mid is None:
            return Algo3Evaluation(False, "invalid_or_single_level_order_book")
        k = self.depth_k()
        try:
            if len(ob["bids"]) < k or len(ob["asks"]) < k:
                return Algo3Evaluation(False, "book_shallower_than_depth_k")
            bids, asks = self._parse_levels(ob, max(k, self.depth_cap(ob) + 1))
        except (KeyError, TypeError, ValueError):
            return Algo3Evaluation(False, "invalid_or_single_level_order_book")
        if any(p <= 0 for p, _ in bids) or any(p <= 0 for p, _ in asks):
            return Algo3Evaluation(False, "invalid_or_single_level_order_book")
        best_bid, best_ask = bids[0][0], asks[0][0]
        epsilon = self.tick_size()
        try:
            last = float(ob.get("lastTradePrice")) if ob.get("lastTradePrice") is not None else None
        except (TypeError, ValueError):
            last = None

        rho = self.decay_rho()
        volume_bid, volume_ask, imbalance_raw = self.imbalance(bids, asks)
        min_volume = self.min_volume()
        thin = min_volume > 0 and (volume_bid + volume_ask) < min_volume
        clip = self.imbalance_clip()
        imbalance = 0.0 if thin else max(-clip, min(clip, imbalance_raw))
        vb0, va0 = bids[0][1], asks[0][1]
        imbalance_level0 = (vb0 - va0) / (vb0 + va0) if vb0 + va0 > 0 else 0.0
        # A thin top of book carries no information: centre on the mid.
        micro = mid if thin else self.microprice(bids, asks)

        x = self.inventory_position(inv_current)
        beta, skew = self.beta(), self.skew()
        lean_factor = 1.0 + beta * imbalance
        skew_factor = 1.0 - skew * (2.0 * x - 1.0)
        centre = micro * lean_factor * skew_factor
        evaluation = Algo3Evaluation(
            True, "", mid=mid, spread=best_ask - best_bid, last=last, depth_k=k, decay_rho=rho,
            levels=[{
                "level": i, "bid": bids[i][0], "bid_volume": bids[i][1],
                "ask": asks[i][0], "ask_volume": asks[i][1], "weight": rho ** i,
            } for i in range(k)],
            volume_bid=volume_bid, volume_ask=volume_ask, min_volume=min_volume, thin_book=thin,
            imbalance_raw=imbalance_raw, imbalance=imbalance, imbalance_level0=imbalance_level0,
            imbalance_clip=clip, microprice=micro, position=x, beta=beta, skew_perc=skew,
            lean_factor=lean_factor, skew_factor=skew_factor, centre=centre,
        )
        if centre <= 0:
            evaluation.ok, evaluation.reason = False, "invalid_centre"
            return evaluation

        h_min = self.minimum_half_spread()
        tick_fraction = epsilon / centre
        h = max(self.configured_half_spread(), h_min + tick_fraction)
        evaluation.h_min, evaluation.h_config = h_min, self.configured_half_spread()
        evaluation.tick_fraction, evaluation.h = tick_fraction, h
        if h >= 1.0:
            evaluation.ok, evaluation.reason = False, "half_spread_not_attainable"
            return evaluation

        raw_bid, raw_ask = centre * (1.0 - h), centre * (1.0 + h)
        max_bid, min_ask = round(best_ask - epsilon, 8), round(best_bid + epsilon, 8)
        bid = self._round_down(min(raw_bid, max_bid))
        ask = self._round_up(max(raw_ask, min_ask))
        evaluation.raw_bid, evaluation.raw_ask = raw_bid, raw_ask
        evaluation.bid, evaluation.ask = bid, ask
        evaluation.clipped_bid, evaluation.clipped_ask = raw_bid > max_bid, raw_ask < min_ask
        evaluation.gross_return = self.gross_return(bid, ask)
        evaluation.net_return = self.net_return(bid, ask)
        evaluation.relative_quoted_spread = (ask - bid) / mid
        # Safety net on the prices actually sent. With h > h_min it cannot
        # fail: rounding and the touch clip only lower the bid and raise the
        # ask, so A/B >= (1+h)/(1-h) > 1 + G (the MM_algo2 document, https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo2/docs/MM_algo2.md).
        if bid <= 0 or bid >= ask or evaluation.net_return is None or evaluation.net_return <= self.min_required_return():
            evaluation.ok = False
            evaluation.reason = "net_return_below_bar"
            return evaluation
        # Book levels for logging: the deepest bid at or above B, the deepest ask at or below A.
        evaluation.bid_level = max((i for i, (p, _) in enumerate(bids) if p >= bid), default=0)
        evaluation.ask_level = max((j for j, (p, _) in enumerate(asks) if p <= ask), default=0)
        evaluation.reason = "microprice_pair"
        return evaluation

    # ---------- Hysteresis ----------
    def _apply_hysteresis(self, bid: float, ask: float) -> Tuple[float, float, bool]:
        """Keep the resting pair while both new prices are within the requote
        band of it (strategy/common.py requote_band: hysteresis_ticks ticks
        or requote_band_perc of the price, whichever is wider) and it is
        younger than refresh_seconds."""
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

    def reset_memory(self) -> None:
        """Forget the resting pair (new session)."""
        self._resting = None

    # ---------- Strategy interface ----------
    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        evaluation = self.evaluate(ob, inv_current)
        if not evaluation.ok:
            self._resting = None
            return None
        bid, ask, kept = self._apply_hysteresis(evaluation.bid, evaluation.ask)
        mid = evaluation.mid
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
            "selection reason": "microprice_pair_kept" if kept else "microprice_pair",
            "reference price": None,
            "imbalance": evaluation.imbalance,
            "microprice": evaluation.microprice,
            "bid kept": kept,
            "ask kept": kept,
        }

    def diagnostics(
        self, ob: Dict[str, Any], inv_current: float, *, round_no: int, session_id,
    ) -> Dict[str, Any]:
        """Describe the decision without changing the strategy's memory. The
        keys the engine, Telegram and the admin panel read from Algorithm 1
        documents (candidate_results[*].{...}, required_threshold, ...) are
        kept, with the one evaluated pair as the only candidate."""
        evaluation = self.evaluate(ob, inv_current)
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
        return {
            "symbol": self.cfg.pair,
            "session_id": str(session_id),
            "round": int(round_no),
            "algorithm": self.key,
            "algorithm_version": 3,
            "inventory": float(inv_current) if inv_current is not None else None,
            "inv_min": float(self.cfg.inv_min),
            "inv_max": float(self.cfg.inv_max),
            "inventory_position": evaluation.position if evaluation.position is not None else self.inventory_position(inv_current),
            "mid_price": mid,
            "spread": evaluation.spread,
            "last_price": evaluation.last,
            "depth_k": self.depth_k(),
            "decay_rho": self.decay_rho(),
            "levels": evaluation.levels,
            "volume_bid": evaluation.volume_bid,
            "volume_ask": evaluation.volume_ask,
            "min_volume": evaluation.min_volume,
            "thin_book": evaluation.thin_book,
            "imbalance_raw": evaluation.imbalance_raw,
            "imbalance": evaluation.imbalance,
            "imbalance_level0": evaluation.imbalance_level0,
            "imbalance_clip": self.imbalance_clip(),
            "microprice": evaluation.microprice,
            "microprice_shift": (evaluation.microprice / mid - 1.0) if (evaluation.microprice and mid) else None,
            "beta": self.beta(),
            "lean_factor": evaluation.lean_factor,
            "skew_perc": self.skew(),
            "skew_factor": evaluation.skew_factor,
            "centre": evaluation.centre,
            "centre_shift": (evaluation.centre / mid - 1.0) if (evaluation.centre and mid) else None,
            "commission_perc": fee,
            "exp_ret_min": float(min_required),
            "required_threshold": float(min_required),
            "min_required_return": float(min_required),
            "gross_bar": self.gross_bar(),
            "h_min": evaluation.h_min if evaluation.centre else self.minimum_half_spread(),
            "h_config": self.configured_half_spread(),
            "tick_fraction": evaluation.tick_fraction,
            "h": evaluation.h,
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
