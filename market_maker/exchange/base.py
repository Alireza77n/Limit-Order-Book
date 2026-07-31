from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ExchangeClient(ABC):
    """Exchange-specific I/O boundary used by SimpleMarketMaker.

    Implement this interface to plug in a different exchange without
    touching strategy or engine (SimpleMarketMaker) code. Method shapes
    match what Algorithm 1 calls today via NobitexExchangeClient.
    """

    @abstractmethod
    def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def wallet_balance(self, currency: str) -> Optional[float]:
        ...

    @abstractmethod
    def send_order(self, order_type: str, srcCurrency: str, dstCurrency: str,
                    amount: float, price: float,
                    clientOrderId: Optional[str] = None) -> Optional[str]:
        ...

    @abstractmethod
    def get_order_list_status(self, status: str = "open", details: int = 2,
                               page_size: int = 100, max_pages: int = 1) -> List[Dict]:
        ...

    @abstractmethod
    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def cancel_all_open_orders(self, max_pages: int = 10) -> None:
        ...

    @abstractmethod
    def cancel_old_orders(self, max_age_minutes: float, df=None, tracked_orders=None):
        ...
