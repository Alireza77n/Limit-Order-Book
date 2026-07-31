from typing import Any, Dict, List, Optional, Tuple

from Config import MMConfig


class Algo1Strategy:
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
