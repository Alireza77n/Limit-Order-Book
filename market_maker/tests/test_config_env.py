"""Pin Nobitex API-key and MySQL environment sourcing behavior."""

import importlib

from config import MMConfig


def test_api_credentials_read_from_environment(monkeypatch):
    monkeypatch.setenv("NOBITEX_API_KEY", "unit-test-key")
    monkeypatch.setenv("NOBITEX_SECRET_KEY", "unit-test-secret")
    cfg = MMConfig()
    assert cfg.api_key == "unit-test-key"
    assert cfg.secret_key == "unit-test-secret"


def test_api_credentials_default_to_none_when_unset(monkeypatch):
    monkeypatch.delenv("NOBITEX_API_KEY", raising=False)
    monkeypatch.delenv("NOBITEX_SECRET_KEY", raising=False)
    cfg = MMConfig()
    assert cfg.api_key is None
    assert cfg.secret_key is None


def test_mysql_env_vars_have_expected_fallbacks(monkeypatch):
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


def test_engine_does_not_load_dotenv_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setenv("LOB_DISABLE_DOTENV", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: calls.append((a, k)))

    import engine
    importlib.reload(engine)

    assert calls == []
