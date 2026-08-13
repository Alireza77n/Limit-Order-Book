from typing import Any, Dict, List, Optional, Tuple

from config import MMConfig
from strategy.base import Strategy


class Algo1Strategy(Strategy):
    """Depth-aware, inventory-skewed quoting strategy (Algorithm 1).

    Pure decision logic only: reads `cfg` and a plain order-book dict, no
    network/DB/file I/O. Moved verbatim out of SimpleMarketMaker so it can
    be constructed and tested without a live exchange connection.

    Profitability semantics: the last traded price is a stale snapshot of
    the market and is never used as a profitability denominator. Two
    quantities are kept distinct:

    * the market's relative quoted spread uses the current mid-price
      ``M_t = (best_bid + best_ask) / 2`` -- a market descriptor;
    * the economic return on deployed capital uses the submitted buy price
      as the capital basis, with both legs' fees deducted:
      ``R_net = (A_q(1-fb)(1-fs) - B_q) / B_q`` where ``B_q``/``A_q`` are
      the tick-adjusted prices that will actually be submitted.

    A candidate pair is accepted only when ``R_net > exp_ret_min``
    (bootstrapped to ``commission_perc`` when unset).
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

    def mid_price(self, ob: Dict[str, Any]) -> Optional[float]:
        """Current mid-price (best bid + best ask) / 2, or None when the
        book has no usable top of book (missing/malformed levels, non-positive
        prices, or a crossed book where best_bid > best_ask)."""
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
        """Size of the submitted spread relative to the current mid-price:
        (A_q - B_q) / M_t. Describes the market; never used as the return
        denominator. None when the mid-price is unavailable or the prices
        are malformed."""
        mid = self.mid_price(ob)
        if mid is None or mid <= 0:
            return None
        try:
            bid = float(bid_price)
            ask = float(ask_price)
        except (TypeError, ValueError):
            return None
        return (ask - bid) / mid

    def gross_return(self, bid_price: float, ask_price: float) -> Optional[float]:
        """Round-trip gross return before fees, with the submitted buy price
        as the capital basis: (A_q - B_q) / B_q. None when prices are
        malformed or non-positive."""
        try:
            bid = float(bid_price)
            ask = float(ask_price)
        except (TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0:
            return None
        return (ask - bid) / bid

    def net_return(
        self,
        bid_price: float,
        ask_price: float,
        buy_fee: Optional[float] = None,
        sell_fee: Optional[float] = None,
    ) -> Optional[float]:
        """Round-trip net return on the capital deployed at the submitted buy
        price, with proportional fees on both legs:

            R_net = (A_q(1-fb)(1-fs) - B_q) / B_q
                  = (1-fb)(1-fs)(1 + R_gross) - 1

        Fees default to the configured commission_perc (the paper ledger
        charges it in base for buys and in quote for sells -- see paper.py,
        which makes the symmetric-commission net return exactly this).
        None when gross return cannot be computed."""
        gross = self.gross_return(bid_price, ask_price)
        if gross is None:
            return None
        if buy_fee is None:
            buy_fee = float(self.cfg.commission_perc)
        if sell_fee is None:
            sell_fee = float(self.cfg.commission_perc)
        return (1.0 - buy_fee) * (1.0 - sell_fee) * (1.0 + gross) - 1.0

    def min_required_return(self) -> float:
        """The bar R_net must strictly exceed: exp_ret_min, bootstrapped to
        commission_perc when unset (engine.py also bootstraps, but resolving
        here too keeps the strategy safe when constructed standalone)."""
        if self.cfg.exp_ret_min is None:
            return float(self.cfg.commission_perc)
        return float(self.cfg.exp_ret_min)

    def spread_ok(self, ob: Dict[str, Any], bid_idx: int, ask_idx: int) -> bool:
        """Feasibility of quoting one tick inside (bid_idx, ask_idx).

        Evaluated on the FINAL tick-adjusted prices that would be submitted
        (raw_bid + eps, raw_ask - eps), never on the raw level gap or the
        last traded price. Accepts only when the fee-adjusted round-trip net
        return on the submitted buy price strictly exceeds the minimum
        required return."""
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


        pairs = self.leg_pairs(inv_d, depth_max_local)

        # bid_level/ask_level start unset (not depth_max_local): if the scan
        # never accepts a single candidate -- whether pairs is empty or the
        # loosest candidate fails immediately -- there is no validated,
        # inventory-skewed quote to fall back to. Silently reusing
        # (depth_max, depth_max) here used to let a maximally passive,
        # unskewed quote through whenever that untested pair happened to be
        # profitable -- see reports/batch-fill-definitive-root-cause.md.
        bid_level = ask_level = None
        for bidx, aidx in pairs:
            if self.spread_ok(ob, bidx, aidx):
                bid_level, ask_level = bidx, aidx
            else:
                break

        if bid_level is None:
            return None

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

        best_bid = float(ob["bids"][0][0])
        best_ask = float(ob["asks"][0][0])
        mid_price = (best_bid + best_ask) / 2.0
        last = float(ob["lastTradePrice"])
        buy_fee = sell_fee = float(self.cfg.commission_perc)

        return {
            "bid": bid_price,
            "ask": ask_price,
            "bid level": bid_level,
            "ask level": ask_level,
            "lastTradePrice": last,
            "mid price": mid_price,
            "relative quoted spread": self.relative_quoted_spread(ob, bid_price, ask_price),
            "gross return": self.gross_return(bid_price, ask_price),
            "net return": self.net_return(
                bid_price, ask_price, buy_fee=buy_fee, sell_fee=sell_fee
            ),
            "buy fee": buy_fee,
            "sell fee": sell_fee,
            "min required return": self.min_required_return(),
            "bid spread": mid_price - bid_price,
            "ask spread": ask_price - mid_price,
            "bid exec prob": 1.0,   # placeholders (no intensity model here)
            "ask exec prob": 1.0
        }

    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        if self.depth_cap(ob) == 0:
            return None
        return self.algo1_bid_ask_prices(ob, inv_current)

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
        epsilon = self.tick_size()
        min_required = self.min_required_return()
        buy_fee = sell_fee = float(self.cfg.commission_perc)
        mid = self.mid_price(ob)
        for bid_level, ask_level in pairs:
            try:
                bid = float(ob["bids"][bid_level][0])
                ask = float(ob["asks"][ask_level][0])
            except (KeyError, IndexError, TypeError, ValueError):
                bid = ask = None
            if bid is not None and ask is not None:
                submitted_bid = bid + epsilon
                submitted_ask = ask - epsilon
                spread_ratio = self.relative_quoted_spread(
                    ob, submitted_bid, submitted_ask
                )
                gross_return = self.gross_return(submitted_bid, submitted_ask)
                net_return = self.net_return(submitted_bid, submitted_ask)
            else:
                submitted_bid = submitted_ask = None
                spread_ratio = gross_return = net_return = None
            passed = self.spread_ok(ob, bid_level, ask_level)
            candidate_results.append({
                "bid_level": bid_level,
                "ask_level": ask_level,
                "bid_price": bid,
                "ask_price": ask,
                "submitted_bid_price": submitted_bid,
                "submitted_ask_price": submitted_ask,
                "last_price": last,
                "mid_price": mid,
                "spread_ratio": spread_ratio,
                "relative_quoted_spread": spread_ratio,
                "gross_return": gross_return,
                "net_return": net_return,
                "buy_fee": buy_fee,
                "sell_fee": sell_fee,
                "commission_perc": float(self.cfg.commission_perc),
                "exp_ret_min": float(min_required),
                "required_threshold": float(min_required),
                "min_required_return": float(min_required),
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
            "exp_ret_min": float(min_required),
            "required_threshold": float(min_required),
            "min_required_return": float(min_required),
            "mid_price": mid,
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
        if reason == "no_candidate_passed_fallback_rejected":
            # Not one candidate in the inventory-skewed scan cleared the
            # profitability bar -- including the loosest one. Previously this
            # silently fell back to an untested, unskewed (depth_max,
            # depth_max) quote instead of refusing to quote; see
            # reports/batch-fill-definitive-root-cause.md. Surfaced under its
            # own name (rather than the generic profitability_threshold
            # below) so this specific path is observable in the CSV/dashboard
            # and distinguishable in tests.
            return "no_profitable_inventory_skewed_candidate"
        return "profitability_threshold"
