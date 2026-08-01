from enum import Enum


class DemoSafetyError(RuntimeError):
    """Raised when Demo Mode is wired to a real trading capability."""


class TradingMode(str, Enum):
    REAL = "real"
    DEMO = "demo"


def enforce_engine_safety(mode: str, exchange, persist_to_db: bool) -> None:
    if mode != TradingMode.DEMO.value:
        return
    if persist_to_db:
        raise DemoSafetyError("Demo Mode cannot enable production MySQL persistence")
    if not getattr(exchange, "is_demo", False):
        raise DemoSafetyError("Demo Mode requires DemoExchangeClient")
