from typing import Any, Dict, Optional

from Config import MMConfig
from strategy.base import Strategy


class Algo2Strategy(Strategy):
    """Placeholder for the Avellaneda-Stoikov market-making model (Algorithm 2).

    Not implemented yet. README.md ("Algorithm 2: Avellaneda-Stoikov Model")
    documents the intended math (stochastic mid-price, optimal reservation
    price, optimal bid/ask spreads); algo2_/research/ has notebooks for
    estimating the model's kappa/sigma parameters, but no reference
    implementation of the quoting decision itself exists yet. This class
    exists so the engine (SimpleMarketMaker) and tests can already depend on
    the Strategy interface -- swap this stub for a real implementation
    without touching engine or exchange code.
    """

    def __init__(self, cfg: MMConfig):
        self.cfg = cfg

    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        raise NotImplementedError(
            "Algo2Strategy (Avellaneda-Stoikov) is not implemented yet. "
            "See README.md for the intended model."
        )
