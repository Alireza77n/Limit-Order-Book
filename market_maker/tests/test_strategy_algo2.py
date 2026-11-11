"""Algo2Strategy is a deliberate placeholder -- no Avellaneda-Stoikov math
exists yet (Alg2.ipynb was empty; docs/MM_algo3.md section 5.4 states the
model). These tests just pin the stub's contract: it's constructible and
plugs into the Strategy interface, and calling decide_order fails loudly
and clearly rather than silently returning a made-up quote.
"""

import pytest

from config import MMConfig
from strategy.base import Strategy
from strategy.algo2 import Algo2Strategy
from helpers import make_orderbook


def test_algo2_strategy_is_a_strategy():
    strategy = Algo2Strategy(MMConfig())
    assert isinstance(strategy, Strategy)


def test_algo2_decide_order_raises_not_implemented():
    strategy = Algo2Strategy(MMConfig())
    ob = make_orderbook()
    with pytest.raises(NotImplementedError):
        strategy.decide_order(ob, 0.5)
