from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class Strategy(ABC):
    """Quoting-decision interface shared by all market-making algorithms.

    Implement this to plug a new algorithm into SimpleMarketMaker without
    touching engine or exchange code. `decide_order` is the only contract
    the engine relies on -- how a strategy gets there internally (extra
    helper methods, parameters, etc.) is up to the implementation.
    """

    @abstractmethod
    def decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        ...
