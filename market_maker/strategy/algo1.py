"""Algorithm 1, version 2: distance-anchored, expected-value quoting.

Pure decision logic: reads ``cfg``, a plain order-book dict and the current
inventory; no network, DB or file I/O (the fill model reads one JSON file).
The engine (``SimpleMarketMaker``) calls ``decide_order`` once per round and
sizes, caps and sends whatever comes back; ``diagnostics`` and
``no_quote_reason`` describe the same decision for the CSV, Telegram and
the admin panel.

What changed from version 1 (see docs/algorithms/ALGORITHM_1_V2.md on the
MM_algo1v2 branch):

* candidates are built on a geometric grid of *price distances* from the
  quote centre and anchored to the first book level at or beyond each
  distance (one tick inside it), instead of enumerating level indices;
* the inventory moves the quote centre continuously,
  ``c = m * (1 - skew_perc * (2x - 1))``, instead of shifting level indices
  by the ``int(10x) - 5`` staircase;
* every bid/ask pair is scored by expected value
  ``J = p_bid * p_ask * R_net - eta * [p_bid(1-p_ask) + p_ask(1-p_bid)]``
  with fill probabilities from ``fill_model.FillModel``; the best pair that
  clears the fee bar ``R_net > exp_ret_min`` wins, instead of "stop at the
  first failing pair";
* a single side is quoted when inventory sits at a band edge or when no pair
  clears the bar but one leg does against a reference price (the current
  centre, or the average cost of inventory we bought ourselves);
* a resting price is kept while the new one is within ``hysteresis_ticks``
  and the quote is younger than ``refresh_seconds``.

The profitability semantics are unchanged: ``R_net = (1-f)^2 * A/B - 1`` on
the prices actually submitted, strictly above ``exp_ret_min`` (bootstrapped
to ``commission_perc`` when unset), and the last traded price is never a
denominator.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import MMConfig
from fill_model import FillModel
from strategy.base import Strategy
from strategy.common import QuoteArithmetic


SIDES = ("bid", "ask")
MAX_GRID_POINTS = 64


@dataclass(frozen=True)
class Candidate:
    side: str                   # "bid" or "ask"
    price: float                # price that would be submitted (on the tick grid)
    distance: float             # realised relative distance from the centre, (c - B)/c or (A - c)/c
    level: Optional[int]        # anchored book level index (0 = best); None when synthetic
    anchored: bool              # True when a book level sits at/beyond the target distance
    raw_price: Optional[float]  # the anchor level's own price
    volume_ahead: float         # volume resting at better prices on our side
    probability: float = 0.0    # P(fill within one lifetime), filled in by evaluate()


@dataclass
class PairResult:
    bid: Candidate
    ask: Candidate
    gross_return: float
    net_return: float
    expected_value: float
    passed: bool


@dataclass
class SingleResult:
    candidate: Candidate
    reference_price: float
    net_return: float
    expected_value: float
    passed: bool


@dataclass
class Evaluation:
    """Everything one round's decision is made of; built once, read by
    ``decide_order``, ``diagnostics`` and ``no_quote_reason``."""
    ok: bool
    reason: str
    mid: Optional[float] = None
    centre: Optional[float] = None
    position: Optional[float] = None
    depth_available: int = 0
    bids: List[Candidate] = field(default_factory=list)
    asks: List[Candidate] = field(default_factory=list)
    pairs: List[PairResult] = field(default_factory=list)
    singles: List[SingleResult] = field(default_factory=list)
    mode: str = "none"                       # "pair" | "bid_only" | "ask_only" | "none"
    chosen_bid: Optional[Candidate] = None
    chosen_ask: Optional[Candidate] = None
    chosen_pair: Optional[PairResult] = None
    chosen_single: Optional[SingleResult] = None
    reference_price: Optional[float] = None


class Algo1Strategy(QuoteArithmetic, Strategy):
    """Depth-aware, inventory-skewed quoting strategy (Algorithm 1, v2).

    The order-book arithmetic (tick, mid, returns, fee bar, inventory
    position) lives in ``strategy.common.QuoteArithmetic``, shared with
    Algorithm 3."""

    key = "algo1"

    def __init__(self, cfg: MMConfig, *, fill_model: Optional[FillModel] = None,
                 clock=None):
        self.cfg = cfg
        self.fill_model = fill_model or FillModel(cfg)
        self.clock = clock or time.monotonic
        # Hysteresis memory: side -> (price, issued_at).
        self._resting: Dict[str, Tuple[float, float]] = {}
        # Cost basis of inventory we bought ourselves (units with a known cost).
        self._avg_cost: Optional[float] = None
        self._cost_units: float = 0.0
        self._tracked_inventory: Optional[float] = None

    # ---------- Inventory ----------
    def quote_centre(self, ob: Dict[str, Any], inv_current: float) -> Optional[float]:
        """c = m * (1 - skew_perc * (2x - 1)): below the mid when inventory is
        high (sell more easily), above it when inventory is low."""
        mid = self.mid_price(ob)
        if mid is None:
            return None
        skew = float(getattr(self.cfg, "skew_perc", 0.0) or 0.0)
        x = self.inventory_position(inv_current)
        return mid * (1.0 - skew * (2.0 * x - 1.0))

    # ---------- Candidates ----------
    def distance_grid(self) -> List[float]:
        """delta_k = grid_min_perc * grid_ratio^(k-1), up to grid_max_perc."""
        start = float(getattr(self.cfg, "grid_min_perc", 0.0005))
        ratio = float(getattr(self.cfg, "grid_ratio", 1.4))
        stop = float(getattr(self.cfg, "grid_max_perc", 0.02))
        if not (start > 0 and math.isfinite(start)):
            return []
        ratio = max(1.05, ratio)
        grid: List[float] = []
        value = start
        while value <= stop * (1.0 + 1e-9) and len(grid) < MAX_GRID_POINTS:
            grid.append(value)
            value *= ratio
        return grid

    def build_candidates(
        self, ob: Dict[str, Any], centre: float
    ) -> Tuple[List[Candidate], List[Candidate]]:
        """One bid and one ask candidate per grid distance, anchored to the
        first book level at or beyond the target and quoted one tick inside
        it; synthetic (tick-rounded target) when the book is shallower.
        Candidates never cross the touch; duplicates (several grid points
        on one level) are kept once."""
        epsilon = self.tick_size()
        depth = self.depth_cap(ob)
        try:
            bids = [(float(p), float(v)) for p, v in ob["bids"][: depth + 1]]
            asks = [(float(p), float(v)) for p, v in ob["asks"][: depth + 1]]
            best_bid, best_ask = bids[0][0], asks[0][0]
        except (KeyError, IndexError, TypeError, ValueError):
            return [], []
        if centre is None or centre <= 0 or best_bid <= 0 or best_ask <= best_bid:
            return [], []

        bid_candidates: List[Candidate] = []
        ask_candidates: List[Candidate] = []
        seen_bid: set = set()
        seen_ask: set = set()
        max_bid = round(best_ask - epsilon, 8)   # most aggressive non-crossing bid
        min_ask = round(best_bid + epsilon, 8)   # most aggressive non-crossing ask

        for delta in self.distance_grid():
            # ---- bid side ----
            target = centre * (1.0 - delta)
            level = next((i for i, (price, _) in enumerate(bids) if price <= target), None)
            if level is not None:
                raw = bids[level][0]
                price = round(raw + epsilon, 8)
                ahead = sum(v for i, (_, v) in enumerate(bids) if i < level)
            else:
                raw = None
                price = self._round_down(target)
                ahead = sum(v for _, v in bids)
            if price > max_bid:
                price, ahead = max_bid, 0.0
                level = 0 if level is not None else level
            if price > 0 and price not in seen_bid:
                seen_bid.add(price)
                bid_candidates.append(Candidate(
                    side="bid", price=price, distance=(centre - price) / centre,
                    level=level, anchored=raw is not None, raw_price=raw,
                    volume_ahead=float(ahead),
                ))
            # ---- ask side ----
            target = centre * (1.0 + delta)
            level = next((j for j, (price, _) in enumerate(asks) if price >= target), None)
            if level is not None:
                raw = asks[level][0]
                price = round(raw - epsilon, 8)
                ahead = sum(v for j, (_, v) in enumerate(asks) if j < level)
            else:
                raw = None
                price = self._round_up(target)
                ahead = sum(v for _, v in asks)
            if price < min_ask:
                price, ahead = min_ask, 0.0
                level = 0 if level is not None else level
            if price > 0 and price not in seen_ask:
                seen_ask.add(price)
                ask_candidates.append(Candidate(
                    side="ask", price=price, distance=(price - centre) / centre,
                    level=level, anchored=raw is not None, raw_price=raw,
                    volume_ahead=float(ahead),
                ))
        bid_candidates.sort(key=lambda c: c.distance)
        ask_candidates.sort(key=lambda c: c.distance)
        return bid_candidates, ask_candidates

    # ---------- Fill probability and scores ----------
    def fill_probability(self, candidate: Candidate) -> float:
        probability = float(self.fill_model.probability(candidate.distance, candidate.side))
        reference = float(getattr(self.cfg, "fill_queue_volume_ref", 0.0) or 0.0)
        if reference > 0 and candidate.volume_ahead > 0:
            probability *= math.exp(-candidate.volume_ahead / reference)
        return max(0.0, min(1.0, probability))

    def _adverse_selection(self) -> float:
        return max(0.0, float(getattr(self.cfg, "adverse_selection_perc", 0.0) or 0.0))

    def _ev_floor(self) -> float:
        return float(getattr(self.cfg, "ev_min", 0.0) or 0.0)

    def pair_value(self, bid: Candidate, ask: Candidate) -> Optional[PairResult]:
        """Fee bar and expected value of quoting (bid, ask) for one lifetime."""
        gross = self.gross_return(bid.price, ask.price)
        net = self.net_return(bid.price, ask.price)
        if gross is None or net is None:
            return None
        p_b, p_a = bid.probability, ask.probability
        both = p_b * p_a
        one_leg = p_b * (1.0 - p_a) + p_a * (1.0 - p_b)
        expected = both * net - self._adverse_selection() * one_leg
        passed = net > self.min_required_return() and expected > self._ev_floor()
        return PairResult(bid, ask, gross, net, expected, passed)

    def single_value(self, candidate: Candidate, reference_price: float) -> Optional[SingleResult]:
        """A lone leg judged against the price it is expected to be closed at."""
        if reference_price is None or reference_price <= 0 or candidate.price <= 0:
            return None
        fee = float(self.cfg.commission_perc)
        if candidate.side == "ask":
            net = (1.0 - fee) ** 2 * candidate.price / reference_price - 1.0
        else:
            net = (1.0 - fee) ** 2 * reference_price / candidate.price - 1.0
        expected = candidate.probability * net - self._adverse_selection() * candidate.probability
        passed = net > self.min_required_return() and expected > self._ev_floor()
        return SingleResult(candidate, reference_price, net, expected, passed)

    # ---------- Cost basis ----------
    def cost_basis(self) -> Optional[float]:
        """Average price paid for the inventory we bought ourselves, or None
        when nothing with a known cost is held."""
        if self._avg_cost is None or self._cost_units <= 0:
            return None
        return self._avg_cost

    def _update_cost_basis(self, inv_current: float) -> None:
        """Inventory changes between rounds are fills of our own resting
        orders at the prices we quoted (the engine has no fill hook); buys
        raise the known-cost units at the last bid, sells consume them."""
        try:
            inventory = float(inv_current)
        except (TypeError, ValueError):
            return
        if not math.isfinite(inventory):
            return
        if self._tracked_inventory is None:
            self._tracked_inventory = inventory
            return
        delta = inventory - self._tracked_inventory
        self._tracked_inventory = inventory
        tolerance = 1e-12
        if delta > tolerance:
            resting_bid = self._resting.get("bid")
            if resting_bid is None:
                return  # inventory arrived from outside the strategy; cost unknown
            price = resting_bid[0]
            total = self._cost_units + delta
            if self._avg_cost is None or self._cost_units <= 0:
                self._avg_cost = price
            else:
                self._avg_cost = (self._avg_cost * self._cost_units + price * delta) / total
            self._cost_units = total
        elif delta < -tolerance:
            self._cost_units = max(0.0, self._cost_units + delta)

    # ---------- The decision ----------
    def evaluate(self, ob: Dict[str, Any], inv_current: float) -> Evaluation:
        """Build candidates, score every pair and every lone leg, pick the
        winner. Pure: no hysteresis or cost-basis mutation."""
        if self.depth_cap(ob) == 0:
            return Evaluation(False, "invalid_or_single_level_order_book")
        mid = self.mid_price(ob)
        centre = self.quote_centre(ob, inv_current)
        if mid is None or centre is None:
            return Evaluation(False, "invalid_or_single_level_order_book")
        x = self.inventory_position(inv_current)
        bids, asks = self.build_candidates(ob, centre)
        bids = [self._with_probability(c) for c in bids]
        asks = [self._with_probability(c) for c in asks]
        evaluation = Evaluation(
            True, "", mid=mid, centre=centre, position=x,
            depth_available=self.depth_cap(ob), bids=bids, asks=asks,
        )
        if not bids and not asks:
            evaluation.ok = False
            evaluation.reason = "no_candidates"
            return evaluation

        cost = self.cost_basis() if bool(getattr(self.cfg, "cost_basis_reference", True)) else None
        ask_reference = cost if cost is not None else centre
        bid_reference = centre
        lo = float(getattr(self.cfg, "one_sided_lo", 0.1))
        hi = float(getattr(self.cfg, "one_sided_hi", 0.9))

        # Inventory at a band edge: only the side that unwinds it.
        if x >= hi and asks:
            self._score_singles(evaluation, asks, ask_reference)
            best = self._best_single(evaluation.singles)
            if best is not None:
                self._choose_single(evaluation, best, "ask_only_inventory_edge")
            else:
                evaluation.ok, evaluation.reason = False, "no_side_clears_bar"
            return evaluation
        if x <= lo and bids:
            self._score_singles(evaluation, bids, bid_reference)
            best = self._best_single(evaluation.singles)
            if best is not None:
                self._choose_single(evaluation, best, "bid_only_inventory_edge")
            else:
                evaluation.ok, evaluation.reason = False, "no_side_clears_bar"
            return evaluation

        # Two-sided: the pair with the largest expected value that clears the bar.
        best_pair: Optional[PairResult] = None
        any_clears_bar = False
        for bid in bids:
            for ask in asks:
                result = self.pair_value(bid, ask)
                if result is None:
                    continue
                evaluation.pairs.append(result)
                if result.net_return > self.min_required_return():
                    any_clears_bar = True
                if result.passed and (
                    best_pair is None
                    or result.expected_value > best_pair.expected_value
                    or (result.expected_value == best_pair.expected_value
                        and result.bid.distance + result.ask.distance
                        < best_pair.bid.distance + best_pair.ask.distance)
                ):
                    best_pair = result
        if best_pair is not None:
            evaluation.mode = "pair"
            evaluation.chosen_pair = best_pair
            evaluation.chosen_bid = best_pair.bid
            evaluation.chosen_ask = best_pair.ask
            evaluation.reason = "pair_selected"
            return evaluation

        # No pair: try each side alone.
        self._score_singles(evaluation, asks, ask_reference)
        self._score_singles(evaluation, bids, bid_reference)
        best = self._best_single(evaluation.singles)
        if best is not None:
            side = best.candidate.side
            self._choose_single(evaluation, best, f"{side}_only_fallback")
            return evaluation
        evaluation.ok = False
        evaluation.reason = (
            "expected_value_below_floor" if any_clears_bar else "no_side_clears_bar"
        )
        return evaluation

    def _with_probability(self, candidate: Candidate) -> Candidate:
        return Candidate(
            side=candidate.side, price=candidate.price, distance=candidate.distance,
            level=candidate.level, anchored=candidate.anchored,
            raw_price=candidate.raw_price, volume_ahead=candidate.volume_ahead,
            probability=self.fill_probability(candidate),
        )

    def _score_singles(self, evaluation: Evaluation, candidates: List[Candidate],
                       reference: float) -> None:
        for candidate in candidates:
            result = self.single_value(candidate, reference)
            if result is not None:
                evaluation.singles.append(result)

    @staticmethod
    def _best_single(singles: List[SingleResult]) -> Optional[SingleResult]:
        passing = [s for s in singles if s.passed]
        if not passing:
            return None
        return max(passing, key=lambda s: (s.expected_value, -s.candidate.distance))

    @staticmethod
    def _choose_single(evaluation: Evaluation, result: SingleResult, reason: str) -> None:
        evaluation.chosen_single = result
        evaluation.reference_price = result.reference_price
        evaluation.reason = reason
        if result.candidate.side == "ask":
            evaluation.mode = "ask_only"
            evaluation.chosen_ask = result.candidate
        else:
            evaluation.mode = "bid_only"
            evaluation.chosen_bid = result.candidate

    # ---------- Hysteresis ----------
    def _apply_hysteresis(self, bid: Optional[float], ask: Optional[float]) -> Tuple[Optional[float], Optional[float], Dict[str, bool]]:
        """Keep a resting price while the new one is within hysteresis_ticks
        of it and the quote is younger than refresh_seconds; a side that is
        no longer quoted forgets its resting price."""
        ticks = max(0, int(getattr(self.cfg, "hysteresis_ticks", 0) or 0))
        refresh = float(getattr(self.cfg, "refresh_seconds", 0.0) or 0.0)
        epsilon = self.tick_size()
        now = float(self.clock())
        kept = {"bid": False, "ask": False}
        prices = {"bid": bid, "ask": ask}
        for side in SIDES:
            new = prices[side]
            if new is None:
                self._resting.pop(side, None)
                continue
            previous = self._resting.get(side)
            if (
                previous is not None and ticks > 0
                and abs(new - previous[0]) <= ticks * epsilon + 1e-9
                and now - previous[1] < refresh
            ):
                prices[side] = previous[0]
                kept[side] = True
            else:
                self._resting[side] = (new, now)
        return prices["bid"], prices["ask"], kept

    def _hysteresis_preserves_bar(self, bid: Optional[float], ask: Optional[float],
                                  evaluation: Evaluation) -> bool:
        bar = self.min_required_return()
        if evaluation.mode == "pair":
            net = self.net_return(bid, ask)
            return net is not None and net > bar
        single = evaluation.chosen_single
        if single is None:
            return False
        price = ask if evaluation.mode == "ask_only" else bid
        probe = Candidate(single.candidate.side, price, single.candidate.distance,
                          single.candidate.level, single.candidate.anchored,
                          single.candidate.raw_price, single.candidate.volume_ahead,
                          single.candidate.probability)
        result = self.single_value(probe, single.reference_price)
        return result is not None and result.net_return > bar

    def reset_memory(self) -> None:
        """Forget resting prices and the cost basis (new session)."""
        self._resting.clear()
        self._avg_cost = None
        self._cost_units = 0.0
        self._tracked_inventory = None

    # ---------- Strategy interface ----------
    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        self._update_cost_basis(inv_current)
        evaluation = self.evaluate(ob, inv_current)
        if not evaluation.ok:
            self._resting.clear()
            return None
        chosen_bid = evaluation.chosen_bid
        chosen_ask = evaluation.chosen_ask
        bid_price = chosen_bid.price if chosen_bid is not None else None
        ask_price = chosen_ask.price if chosen_ask is not None else None

        kept_bid, kept_ask, kept = self._apply_hysteresis(bid_price, ask_price)
        if (kept["bid"] or kept["ask"]) and not self._hysteresis_preserves_bar(kept_bid, kept_ask, evaluation):
            # The kept price no longer clears the bar with the new partner: re-quote.
            now = float(self.clock())
            for side, price in (("bid", bid_price), ("ask", ask_price)):
                if price is not None:
                    self._resting[side] = (price, now)
            kept = {"bid": False, "ask": False}
        else:
            bid_price, ask_price = kept_bid, kept_ask

        mid = evaluation.mid
        fee = float(self.cfg.commission_perc)
        try:
            last = float(ob.get("lastTradePrice")) if ob.get("lastTradePrice") is not None else None
        except (TypeError, ValueError):
            last = None
        if evaluation.mode == "pair":
            gross = self.gross_return(bid_price, ask_price)
            net = self.net_return(bid_price, ask_price)
            relative = self.relative_quoted_spread(ob, bid_price, ask_price)
            expected = evaluation.chosen_pair.expected_value
        else:
            single = evaluation.chosen_single
            price = ask_price if evaluation.mode == "ask_only" else bid_price
            probe = Candidate(single.candidate.side, price, single.candidate.distance,
                              single.candidate.level, single.candidate.anchored,
                              single.candidate.raw_price, single.candidate.volume_ahead,
                              single.candidate.probability)
            refreshed = self.single_value(probe, single.reference_price) or single
            gross = (price / single.reference_price - 1.0) if evaluation.mode == "ask_only" else (single.reference_price / price - 1.0)
            net = refreshed.net_return
            relative = None
            expected = refreshed.expected_value

        return {
            "bid": bid_price,
            "ask": ask_price,
            "bid level": chosen_bid.level if chosen_bid is not None else None,
            "ask level": chosen_ask.level if chosen_ask is not None else None,
            "lastTradePrice": last,
            "mid price": mid,
            "centre": evaluation.centre,
            "inventory position": evaluation.position,
            "relative quoted spread": relative,
            "gross return": gross,
            "net return": net,
            "buy fee": fee,
            "sell fee": fee,
            "min required return": self.min_required_return(),
            "bid spread": (mid - bid_price) if bid_price is not None else None,
            "ask spread": (ask_price - mid) if ask_price is not None else None,
            "bid distance": chosen_bid.distance if chosen_bid is not None else None,
            "ask distance": chosen_ask.distance if chosen_ask is not None else None,
            "bid exec prob": chosen_bid.probability if chosen_bid is not None else None,
            "ask exec prob": chosen_ask.probability if chosen_ask is not None else None,
            "expected value": expected,
            "quote mode": evaluation.mode,
            "selection reason": evaluation.reason,
            "reference price": evaluation.reference_price,
            "bid kept": kept["bid"],
            "ask kept": kept["ask"],
        }

    def diagnostics(
        self, ob: Dict[str, Any], inv_current: float, *, round_no: int, session_id,
    ) -> Dict[str, Any]:
        """Describe the decision without changing the strategy's memory.
        Keeps the version-1 keys the engine, Telegram and admin views read
        (candidate_results[*].{bid_level, ask_level, submitted_*_price,
        spread_ratio, net_return, passed}, required_threshold, ...)."""
        evaluation = self.evaluate(ob, inv_current)
        min_required = self.min_required_return()
        fee = float(self.cfg.commission_perc)
        try:
            last = float(ob.get("lastTradePrice")) if ob.get("lastTradePrice") is not None else None
        except (TypeError, ValueError):
            last = None
        mid = evaluation.mid
        x = evaluation.position if evaluation.position is not None else self.inventory_position(inv_current)

        def describe(candidate: Candidate) -> Dict[str, Any]:
            return {
                "side": candidate.side,
                "price": candidate.price,
                "distance": candidate.distance,
                "level": candidate.level,
                "anchored": candidate.anchored,
                "raw_price": candidate.raw_price,
                "volume_ahead": candidate.volume_ahead,
                "fill_probability": candidate.probability,
            }

        candidate_results = []
        for result in evaluation.pairs:
            spread_ratio = (
                (result.ask.price - result.bid.price) / mid if mid else None
            )
            candidate_results.append({
                "bid_level": result.bid.level,
                "ask_level": result.ask.level,
                "bid_price": result.bid.raw_price,
                "ask_price": result.ask.raw_price,
                "submitted_bid_price": result.bid.price,
                "submitted_ask_price": result.ask.price,
                "bid_distance": result.bid.distance,
                "ask_distance": result.ask.distance,
                "last_price": last,
                "mid_price": mid,
                "spread_ratio": spread_ratio,
                "relative_quoted_spread": spread_ratio,
                "gross_return": result.gross_return,
                "net_return": result.net_return,
                "p_bid": result.bid.probability,
                "p_ask": result.ask.probability,
                "expected_value": result.expected_value,
                "buy_fee": fee,
                "sell_fee": fee,
                "commission_perc": fee,
                "exp_ret_min": float(min_required),
                "required_threshold": float(min_required),
                "min_required_return": float(min_required),
                "passed": result.passed,
            })
        single_results = [{
            "side": result.candidate.side,
            "price": result.candidate.price,
            "level": result.candidate.level,
            "distance": result.candidate.distance,
            "fill_probability": result.candidate.probability,
            "reference_price": result.reference_price,
            "net_return": result.net_return,
            "expected_value": result.expected_value,
            "passed": result.passed,
        } for result in evaluation.singles]

        return {
            "symbol": self.cfg.pair,
            "session_id": str(session_id),
            "round": int(round_no),
            "algorithm": self.key,
            "algorithm_version": 2,
            "inventory": float(inv_current) if inv_current is not None else None,
            "inv_min": float(self.cfg.inv_min),
            "inv_max": float(self.cfg.inv_max),
            "inventory_position": x,
            "inv_d": int(10 * x) - 5,   # legacy staircase value, for old readers
            "skew_perc": float(getattr(self.cfg, "skew_perc", 0.0) or 0.0),
            "centre": evaluation.centre,
            "mid_price": mid,
            "last_price": last,
            "depth_max_config": int(self.cfg.depth_max),
            "depth_max_available": int(evaluation.depth_available),
            "distance_grid": self.distance_grid(),
            "candidate_bids": [describe(c) for c in evaluation.bids],
            "candidate_asks": [describe(c) for c in evaluation.asks],
            "candidate_pairs": [[r.bid.level, r.ask.level] for r in evaluation.pairs],
            "commission_perc": fee,
            "exp_ret_min": float(min_required),
            "required_threshold": float(min_required),
            "min_required_return": float(min_required),
            "gross_bar": self.gross_bar(),
            "adverse_selection_perc": self._adverse_selection(),
            "ev_min": self._ev_floor(),
            "candidate_results": candidate_results,
            "single_results": single_results,
            "quote_mode": evaluation.mode,
            "selected_bid_level": evaluation.chosen_bid.level if evaluation.chosen_bid else None,
            "selected_ask_level": evaluation.chosen_ask.level if evaluation.chosen_ask else None,
            "selected_bid_price": evaluation.chosen_bid.price if evaluation.chosen_bid else None,
            "selected_ask_price": evaluation.chosen_ask.price if evaluation.chosen_ask else None,
            "selected_expected_value": (
                evaluation.chosen_pair.expected_value if evaluation.chosen_pair is not None
                else evaluation.chosen_single.expected_value if evaluation.chosen_single is not None
                else None
            ),
            "reference_price": evaluation.reference_price,
            "cost_basis": self.cost_basis(),
            "selection_reason": evaluation.reason,
            "fallback_detected": False,
            "fill_model": self.fill_model.describe(),
        }

    def no_quote_reason(self, ob: Dict[str, Any], inv_current: float) -> str:
        evaluation = self.evaluate(ob, inv_current)
        if evaluation.ok:
            return "quote_available"
        return evaluation.reason
