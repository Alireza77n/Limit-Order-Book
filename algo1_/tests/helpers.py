"""Test-only helpers for exercising SimpleMarketMaker without I/O.

SimpleMarketMaker.__init__ unconditionally opens a live MySQL connection and
wires up real Nobitex API clients, so it cannot be constructed normally in
tests. The quoting logic under test (`_algo1_bid_ask_prices`, `_spread_ok`,
`_leg_pairs`, `_tick_size`, `_depth_cap`, `_decide_order`) only ever reads
`self.cfg`, so `make_bot` builds an instance via `__new__` (skipping
`__init__`) and replays just the config-normalization lines `__init__` would
have run. This is a test seam only -- production code is untouched.
"""

from Algo1 import SimpleMarketMaker


def make_bot(cfg):
    bot = object.__new__(SimpleMarketMaker)
    bot.cfg = cfg
    if bot.cfg.exp_ret_min is None:
        bot.cfg.exp_ret_min = bot.cfg.commission_perc
    if bot.cfg.order_size is None:
        bot.cfg.order_size = bot.cfg.inv_max / 100.0
    return bot


def make_orderbook(n=11, last=100.0):
    """Synthetic Nobitex-shaped order book: bids/asks 1 unit apart per level."""
    bids = [[str(100 - i), "1.0"] for i in range(n)]
    asks = [[str(101 + i), "1.0"] for i in range(n)]
    return {"bids": bids, "asks": asks, "lastTradePrice": str(last)}
