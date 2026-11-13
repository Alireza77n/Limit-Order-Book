"""The exchange dimension: every market belongs to one exchange (config), the dashboard's Exchange
selector filters the Track list, the admin panel shows one exchange at a time, and the engine rounds
order amounts to what the exchange accepts."""
import re
import types

import pytest
from werkzeug.security import generate_password_hash

import config
import dashboard
import run
from admin_app import create_app
from engine import SimpleMarketMaker

MAZDAX = ["ahrm1", "roba1", "nogh1", "zfam1"]
NOBITEX = ["btc", "usdt", "eth", "xaut", "trx"]


def test_profiles_belong_to_one_exchange():
    assert config.exchange_profiles("nobitex") == NOBITEX
    assert config.exchange_profiles("mazdax") == MAZDAX
    assert config.profile_exchange("btc") == "nobitex" and config.profile_exchange("ahrm1") == "mazdax"
    for name in MAZDAX:
        profile = config.TRADING_PROFILES[name]
        assert profile["pair"] == f"{name.upper()}IRR" and profile["currency_2"] == "irr"
        assert profile["tick"] == 1.0 and profile["amount_step"] == 1.0 and profile["min_notional"] > 0
        assert "_" not in name                      # dashboard track keys split on "_"


def test_symbol_and_tick_helpers():
    assert config.split_symbol("AHRM1IRR") == ("AHRM1", "IRR")
    assert config.split_symbol("BTCIRT") == ("BTC", "IRT")
    assert config.infer_tick("BTCIRT") == 10.0 and config.infer_tick("AHRM1IRR", 1.0) == 1.0


def test_mazdax_config_uses_its_own_credentials_and_tick(monkeypatch, tmp_path):
    monkeypatch.setenv("MAZDAX_API_KEY", "mzx_test")
    monkeypatch.setenv("MAZDAX_SECRET_KEY", "secret_test")
    monkeypatch.setenv("NOBITEX_API_KEY", "nobitex_test")
    cfg = run.make_cfg("ahrm1", "real", session_id="s")
    assert (cfg.exchange, cfg.api_key, cfg.secret_key) == ("mazdax", "mzx_test", "secret_test")
    assert run.build_strategy(cfg).tick_size() == 1.0
    assert run.make_cfg("btc", "real", session_id="s").api_key == "nobitex_test"


def test_a_profile_runs_on_its_own_exchange_only():
    cfg = run.make_cfg("ahrm1", "real", session_id="s")
    with pytest.raises(ValueError, match="trades on mazdax"):
        run.make_bot(cfg, "algo3", "nobitex", "real")


def test_engine_rounds_amounts_to_the_exchange_rules():
    bot = SimpleMarketMaker.__new__(SimpleMarketMaker)
    bot.cfg = types.SimpleNamespace(amount_step=1.0, min_notional=100000.0)
    assert bot._exchange_amount(34.9, 88000.0) == 34.0
    assert bot._exchange_amount(1.0, 88000.0) == 0.0          # 88,000 rial is under the 100,000 floor
    assert bot._exchange_amount(2.0, 88000.0) == 2.0
    bot.cfg = types.SimpleNamespace(amount_step=0.0, min_notional=0.0)
    assert bot._exchange_amount(0.000015, 5e9) == 0.000015     # Nobitex markets: unchanged


def test_dashboard_tracks_follow_the_exchange_selector():
    assert dashboard.DASHBOARD_EXCHANGES == ["nobitex", "mazdax"]
    mazdax = dashboard.visible_tracks("all", "mazdax")
    assert mazdax and all(key.split("_")[0] in MAZDAX for key in mazdax)
    assert all(key.split("_")[0] in NOBITEX for key in dashboard.visible_tracks("all", "nobitex"))
    assert set(dashboard.visible_tracks("all")) == set(dashboard.TRACKS)          # no filter: every track
    options, value = dashboard.tracks_for_algorithm("algo3", None, "btc_demo_algo3", "mazdax")
    assert value == "ahrm1_demo_algo3"                                            # same view, other exchange
    assert {option["value"].split("_")[0] for option in options} <= set(MAZDAX)
    assert dashboard.apply_url_exchange("?exchange=mazdax") == "mazdax"
    assert dashboard.apply_url_exchange("?track=ahrm1_real") == "mazdax"


@pytest.fixture
def client(tmp_path):
    for name in ("control", "state", "results", "backups"):
        (tmp_path / name).mkdir()
    app = create_app({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": generate_password_hash("correct horse", method="pbkdf2:sha256"),
        "SECRET_KEY": "test-secret", "TESTING": True,
        "CONTROL_DIR": str(tmp_path / "control"), "STATE_DIR": str(tmp_path / "state"),
        "RESULTS_DIR": str(tmp_path / "results"), "BACKUPS_DIR": str(tmp_path / "backups"),
    })
    client = app.test_client()
    page = client.get("/admin/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    client.post("/admin/login", data={"username": "admin", "password": "correct horse", "csrf_token": token})
    return client


def _config_links(html):
    return re.findall(r">Config ([A-Z0-9]+)<", html)


def test_admin_shows_one_exchange_at_a_time(client):
    html = client.get("/admin/", headers={"Accept": "text/html"}).get_data(as_text=True)
    assert "exchange-switch" in html and ">Nobitex<" in html and ">Mazdax<" in html
    assert _config_links(html) == [name.upper() for name in NOBITEX]
    response = client.get("/admin/exchange/mazdax?next=/admin/workers")
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/workers")
    assert _config_links(client.get("/admin/", headers={"Accept": "text/html"}).get_data(as_text=True)) == [name.upper() for name in MAZDAX]
    client.get("/admin/exchange/nobitex")
    assert _config_links(client.get("/admin/", headers={"Accept": "text/html"}).get_data(as_text=True)) == [name.upper() for name in NOBITEX]


def test_admin_exchange_selector_refuses_unknown_names_and_foreign_targets(client):
    assert client.get("/admin/exchange/binance").status_code == 404
    response = client.get("/admin/exchange/mazdax?next=https://example.com/x")
    assert response.headers["Location"].endswith("/admin/")
    response = client.get("/admin/exchange/mazdax?next=/admin/config/btc")
    assert response.headers["Location"].endswith("/admin/")     # that market is not on the other exchange
