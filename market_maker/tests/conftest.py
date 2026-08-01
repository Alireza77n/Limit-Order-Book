import sys
from pathlib import Path

import pytest

# engine.py / config.py use flat, non-package imports (e.g. `from config
# import MMConfig`), so the market_maker directory itself must be importable
# as top-level modules. Adding it to sys.path here keeps that quirk
# contained to test collection instead of touching production code.
PACKAGE_DIR = Path(__file__).resolve().parent.parent
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))


@pytest.fixture(autouse=True)
def disable_external_telegram(monkeypatch):
    """Unit tests must never inherit an operator's live Telegram credentials."""
    import engine

    monkeypatch.setattr(engine, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(engine, "TELEGRAM_CHAT_ID", None)
