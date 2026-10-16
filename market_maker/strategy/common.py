"""Order-book arithmetic every quoting algorithm needs: the tick, the mid,
the inventory position, round-trip returns and the fee bar.

Algorithm 1 (strategy/algo1.py) and Algorithm 3 (strategy/algo3.py) share
these so that "profitable" means the same thing in both:
``R_net = (1-f)^2 * A/B - 1`` on the prices actually submitted, strictly
above ``exp_ret_min`` (bootstrapped to ``commission_perc`` when unset).
Pure functions of ``self.cfg`` and a plain order-book dict; no I/O.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional


class QuoteArithmetic:
    """Mixin: expects ``self.cfg`` (an MMConfig)."""

    cfg: Any

    # ---------- Microstructure helpers ----------
    def depth_cap(self, ob: Dict[str, Any]) -> int:
        """Maximum valid level index available on both sides, capped by config."""
        try:
            return max(
                0,
                min(self.cfg.depth_max, len(ob["bids"]) - 1, len(ob["asks"]) - 1),
            )
        except Exception:
            return 0

    def tick_size(self) -> float:
        if self.cfg.pair.upper().endswith("IRT"):
            return 10.0
        if self.cfg.pair.upper().endswith("USDT"):
            return 0.01
        return 0.01

    def mid_price(self, ob: Dict[str, Any]) -> Optional[float]:
        """(best bid + best ask) / 2, or None for a missing, non-positive or
        crossed top of book."""
        try:
            best_bid = float(ob["bids"][0][0])
            best_ask = float(ob["asks"][0][0])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
            return None
        return (best_bid + best_ask) / 2.0

    def relative_quoted_spread(
        self, ob: Dict[str, Any], bid_price: float, ask_price: float
    ) -> Optional[float]:
        """(A - B) / M_t: a market descriptor, never a return denominator."""
        mid = self.mid_price(ob)
        if mid is None or mid <= 0:
            return None
        try:
            return (float(ask_price) - float(bid_price)) / mid
        except (TypeError, ValueError):
            return None

    def gross_return(self, bid_price: float, ask_price: float) -> Optional[float]:
        """Round-trip gross return on the submitted buy price: (A - B) / B."""
        try:
            bid = float(bid_price)
            ask = float(ask_price)
        except (TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0:
            return None
        return (ask - bid) / bid

    def net_return(
        self, bid_price: float, ask_price: float,
        buy_fee: Optional[float] = None, sell_fee: Optional[float] = None,
    ) -> Optional[float]:
        """R_net = (1-fb)(1-fs)(1 + R_gross) - 1."""
        gross = self.gross_return(bid_price, ask_price)
        if gross is None:
            return None
        if buy_fee is None:
            buy_fee = float(self.cfg.commission_perc)
        if sell_fee is None:
            sell_fee = float(self.cfg.commission_perc)
        return (1.0 - buy_fee) * (1.0 - sell_fee) * (1.0 + gross) - 1.0

    def min_required_return(self) -> float:
        """The bar R_net must strictly exceed (exp_ret_min, bootstrapped to
        commission_perc when unset)."""
        if self.cfg.exp_ret_min is None:
            return float(self.cfg.commission_perc)
        return float(self.cfg.exp_ret_min)

    def gross_bar(self) -> float:
        """G = (1 + r_min) / (1 - f)^2 - 1: the sell/buy ratio minus one that a
        round trip needs to clear the bar (about 2f + r_min)."""
        fee = float(self.cfg.commission_perc)
        if fee >= 1.0:
            return math.inf
        return (1.0 + self.min_required_return()) / ((1.0 - fee) ** 2) - 1.0

    def spread_ok(self, ob: Dict[str, Any], bid_idx: int, ask_idx: int) -> bool:
        """Is one tick inside the level pair (bid_idx, ask_idx) profitable?
        A diagnostic helper kept from Algorithm 1 version 1."""
        try:
            raw_bid = float(ob["bids"][bid_idx][0])
            raw_ask = float(ob["asks"][ask_idx][0])
        except Exception:
            return False
        epsilon = self.tick_size()
        net = self.net_return(raw_bid + epsilon, raw_ask - epsilon)
        if net is None:
            return False
        return net > self.min_required_return()

    # ---------- Inventory ----------
    def inventory_position(self, inv_current: float) -> float:
        """x in [0, 1]: where the inventory sits inside [inv_min, inv_max]."""
        inv_min = float(self.cfg.inv_min)
        inv_max = float(self.cfg.inv_max)
        band = inv_max - inv_min
        if not math.isfinite(band) or band <= 0:
            return 0.5
        try:
            value = (float(inv_current) - inv_min) / band
        except (TypeError, ValueError):
            return 0.5
        if not math.isfinite(value):
            return 0.5
        return max(0.0, min(1.0, value))

    # ---------- Tick grid ----------
    def _round_down(self, price: float) -> float:
        epsilon = self.tick_size()
        return round(math.floor(price / epsilon + 1e-9) * epsilon, 8)

    def _round_up(self, price: float) -> float:
        epsilon = self.tick_size()
        return round(math.ceil(price / epsilon - 1e-9) * epsilon, 8)
