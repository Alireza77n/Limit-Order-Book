from typing import Any, Dict, List, Optional, Tuple

from config import MMConfig
from strategy.base import Strategy


class Algo1Strategy(Strategy):
    """Depth-aware, inventory-skewed quoting strategy (Algorithm 1).

    Pure decision logic only: reads `cfg` and a plain order-book dict, no
    network/DB/file I/O. Moved verbatim out of SimpleMarketMaker so it can
    be constructed and tested without a live exchange connection.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg

    # ---------- Microstructure helpers ----------
    def depth_cap(self, ob: Dict[str, Any]) -> int:
        """Return the maximum valid level index available in both sides, capped by config."""
        try:
            return max(
                0,
                min(
                    self.cfg.depth_max,
                    len(ob["bids"]) - 1,
                    len(ob["asks"]) - 1
                )
            )
        except Exception:
            return 0

    def tick_size(self) -> float:
        if self.cfg.pair.upper().endswith("IRT"):
            return 10.0
        if self.cfg.pair.upper().endswith("USDT"):
            return 0.01
        return 0.01

    def spread_ok(self, ob: Dict[str, Any], bid_idx: int, ask_idx: int) -> bool:
        try:
            bid = float(ob["bids"][bid_idx][0])
            ask = float(ob["asks"][ask_idx][0])
            last = float(ob["lastTradePrice"])

            #print('aaa:',bid_idx,ask_idx,(ask - bid) / last , (self.cfg.commission_perc + self.cfg.exp_ret_min))
        except Exception:
            return False
        # Require gross spread to exceed (commission + minimum expected return).

        #print('bid_level:',bid_idx,'ask_level:',ask_idx,'left:',(ask - bid) / last,'right:',(self.cfg.commission_perc + self.cfg.exp_ret_min))

        return  (ask - bid) / last > (self.cfg.commission_perc + self.cfg.exp_ret_min)

    def leg_pairs(self, inv_diff: int, depth_max: int) -> List[Tuple[int, int]]:
        """Generate (bid_level, ask_level) pairs walking inward from depth_max."""
        pairs = []
        for bid_l in range(depth_max, 0, -1):
            ask_l = bid_l - inv_diff
            if 0 <= ask_l <= depth_max:
                pairs.append((bid_l, ask_l))


        return pairs

    # ---------- Quote selection (Algo1) ----------
    def algo1_bid_ask_prices(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        epsilon = self.tick_size()
        depth_max_local = self.depth_cap(ob)
        if depth_max_local == 0:
            return None

        # Map inventory to an integer shift in levels (same structure as your original idea)
        denom = self.cfg.inv_max - self.cfg.inv_min
        inv_d = int(10 * (inv_current - self.cfg.inv_min) / denom) - 5

        #print('inv_current:',inv_current,'inv_max:',self.cfg.inv_max,'ten:',int(10 * (inv_current - self.cfg.inv_min) / denom))


        pairs = self.leg_pairs(inv_d, depth_max_local)
        #print('pairs:',pairs,'inv_d:',inv_d,'inv_current:',inv_current,'self.cfg.inv_min:',self.cfg.inv_min,'denom:',denom)


        bid_level = ask_level = depth_max_local
        for bidx, aidx in pairs:
            if self.spread_ok(ob, bidx, aidx):
                bid_level, ask_level = bidx, aidx
            else:
                break

        bid_level = max(0, min(bid_level, depth_max_local))
        ask_level = max(0, min(ask_level, depth_max_local))

        try:
            raw_bid = float(ob["bids"][bid_level][0])
            raw_ask = float(ob["asks"][ask_level][0])
        except Exception:
            return None

        # Quote one tick inside the selected levels
        bid_price = raw_bid + epsilon
        ask_price = raw_ask - epsilon

        # Move one step more aggressive (to increase fill probability)
        #bid_level = max(0, bid_level - 1)
        #ask_level = max(0, ask_level - 1)

        best_bid = float(ob["bids"][0][0])
        best_ask = float(ob["asks"][0][0])
        mid_price = (best_bid + best_ask) / 2.0
        last = float(ob["lastTradePrice"])

        return {
            "bid": bid_price,
            "ask": ask_price,
            "bid level": bid_level,
            "ask level": ask_level,
            "lastTradePrice": last,
            "bid spread": mid_price - bid_price,
            "ask spread": ask_price - mid_price,
            "bid exec prob": 1.0,   # placeholders (no intensity model here)
            "ask exec prob": 1.0
        }

    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        depth_max_local = self.depth_cap(ob)

        if depth_max_local == 0:
            return None

        temp=self.algo1_bid_ask_prices(ob, inv_current)

        bid_level,ask_level=temp['bid level'],temp['ask level']

        if self.spread_ok(ob, bid_level, ask_level):


            return self.algo1_bid_ask_prices(ob, inv_current)
        return None

    def diagnostics(
        self,
        ob: Dict[str, Any],
        inv_current: float,
        *,
        round_no: int,
        session_id,
    ) -> Dict[str, Any]:
        """Describe the existing selection path without changing its result."""
        depth_available = self.depth_cap(ob)
        denom = self.cfg.inv_max - self.cfg.inv_min
        inv_d = None
        if denom:
            inv_d = int(10 * (inv_current - self.cfg.inv_min) / denom) - 5
        pairs = self.leg_pairs(inv_d, depth_available) if inv_d is not None else []
        candidate_results = []
        selected_candidate = None
        last = float(ob.get("lastTradePrice") or 0.0)
        for bid_level, ask_level in pairs:
            try:
                bid = float(ob["bids"][bid_level][0])
                ask = float(ob["asks"][ask_level][0])
                spread_ratio = (ask - bid) / last
            except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
                spread_ratio = None
            passed = self.spread_ok(ob, bid_level, ask_level)
            candidate_results.append({
                "bid_level": bid_level,
                "ask_level": ask_level,
                "bid_price": bid if spread_ratio is not None else None,
                "ask_price": ask if spread_ratio is not None else None,
                "last_price": last,
                "spread_ratio": spread_ratio,
                "commission_perc": float(self.cfg.commission_perc),
                "exp_ret_min": float(
                    self.cfg.commission_perc
                    if self.cfg.exp_ret_min is None
                    else self.cfg.exp_ret_min
                ),
                "required_threshold": float(
                    self.cfg.commission_perc
                    + (
                        self.cfg.commission_perc
                        if self.cfg.exp_ret_min is None
                        else self.cfg.exp_ret_min
                    )
                ),
                "passed": passed,
            })
            if passed:
                selected_candidate = (bid_level, ask_level)
            else:
                break

        quote = self.decide_order(ob, inv_current)
        fallback_detected = selected_candidate is None and depth_available > 0
        if depth_available == 0:
            selection_reason = "invalid_or_single_level_order_book"
            selected_bid_level = selected_ask_level = None
        elif selected_candidate is not None:
            selection_reason = "candidate_selected"
            selected_bid_level = quote["bid level"] if quote else None
            selected_ask_level = quote["ask level"] if quote else None
        elif not pairs and depth_available > 0:
            selection_reason = "inventory_skew_unexpressible_in_shallow_book"
            selected_bid_level = selected_ask_level = None
        elif quote is not None:
            selection_reason = "no_candidate_passed_fallback_selected"
            selected_bid_level = selected_ask_level = None
        else:
            selection_reason = "no_candidate_passed_fallback_rejected"
            selected_bid_level = selected_ask_level = None

        exp_ret_min = (
            self.cfg.commission_perc
            if self.cfg.exp_ret_min is None
            else self.cfg.exp_ret_min
        )
        return {
            "symbol": self.cfg.pair,
            "session_id": str(session_id),
            "round": int(round_no),
            "inventory": float(inv_current),
            "inv_min": float(self.cfg.inv_min),
            "inv_max": float(self.cfg.inv_max),
            "inv_d": inv_d,
            "depth_max_config": int(self.cfg.depth_max),
            "depth_max_available": int(depth_available),
            "candidate_pairs": [[bid, ask] for bid, ask in pairs],
            "commission_perc": float(self.cfg.commission_perc),
            "exp_ret_min": float(exp_ret_min),
            "required_threshold": float(self.cfg.commission_perc + exp_ret_min),
            "candidate_results": candidate_results,
            "selected_bid_level": selected_bid_level,
            "selected_ask_level": selected_ask_level,
            "fallback_bid_level": depth_available if fallback_detected else None,
            "fallback_ask_level": depth_available if fallback_detected else None,
            "selection_reason": selection_reason,
            "fallback_detected": fallback_detected,
        }

    def no_quote_reason(self, ob: Dict[str, Any], inv_current: float) -> str:
        diagnostic = self.diagnostics(
            ob, inv_current, round_no=0, session_id="reason-only"
        )
        reason = diagnostic["selection_reason"]
        if reason == "inventory_skew_unexpressible_in_shallow_book":
            return reason
        if reason == "invalid_or_single_level_order_book":
            return reason
        return "profitability_threshold"
