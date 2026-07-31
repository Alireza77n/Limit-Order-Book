"""Pins the env-var sourcing behavior introduced when the hardcoded Nobitex
token and MySQL credentials were removed from source. MMConfig.token and
engine's MYSQL_* constants must reflect whatever is in the environment at
construction/import time, and degrade to None (not crash) when unset.
"""

import importlib

from config import MMConfig


def test_token_reads_from_environment(monkeypatch):
    monkeypatch.setenv("NOBITEX_API_TOKEN", "unit-test-token")
    cfg = MMConfig()
    assert cfg.token == "unit-test-token"


def test_token_defaults_to_none_when_unset(monkeypatch):
    monkeypatch.delenv("NOBITEX_API_TOKEN", raising=False)
    cfg = MMConfig()
    assert cfg.token is None


def test_mysql_env_vars_have_expected_fallbacks(monkeypatch):
    # A local market_maker/.env exists in this dev environment; reloading
    # engine would otherwise repopulate these vars from it via
    # load_dotenv(), masking the "unset in a fresh environment" case this
    # test targets.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("MYSQL_HOST", raising=False)
    monkeypatch.delenv("MYSQL_USER", raising=False)
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)

    import engine
    importlib.reload(engine)

    assert engine.MYSQL_HOST == "localhost"
    assert engine.MYSQL_USER is None
    assert engine.MYSQL_PASSWORD is None
    assert engine.MYSQL_DATABASE == "trading_bot"
