"""End-to-end tests for demo mode: the full run() loop, for real, against
real Nobitex market data, for every profile run.py exposes.

Unlike every other test in this suite, these make real network calls (to
Nobitex's public orderbook endpoint -- no auth, read-only, nothing placed).
They're kept in their own file and skip cleanly (not fail) if the network
is unavailable, so an offline/CI environment doesn't get a false failure.

What they prove, for each of run.py's PROFILES, that pure unit tests can't:
- The whole run() loop actually completes against live data without
  raising, for a real MMConfig/NobitexExchangeClient/DemoExchangeClient/
  Algo1Strategy wiring identical to what `python run.py --mode demo` uses.
- NobitexExchangeClient.send_order is never called, even once, across a
  real run -- not just "DemoExchangeClient doesn't expose a path to it"
  (proven structurally elsewhere) but "the engine's real control flow
  never reaches it," verified by monkeypatching it to raise if called.
- No MySQL connection is attempted (persist_to_db=False).
- The output CSV is written with sane, complete rows.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import requests

import run
from config import MMConfig
from portfolio import Nobitex_portfolio
from engine import SimpleMarketMaker
from exchange.nobitex import NobitexExchangeClient
from exchange.demo import DemoExchangeClient
from strategy.algo1 import Algo1Strategy

EXPECTED_COLUMNS = [
    "Session_id", "Algorithm", "Crypto", "Timestamp", "Bid Level", "Ask Level",
    "inv_current", "cash_current", "Portfolio Wealth", "Best Bid", "Best Ask",
    "Last Price", "Bid Price Sent", "Ask Price Sent", "Buy Size", "Sell Size",
    "Buy Sent", "Sell Sent", "Buy Status", "Sell Status",
]


def _skip_if_offline():
    try:
        requests.get("https://apiv2.nobitex.ir/v3/orderbook/BTCIRT", timeout=5)
    except requests.RequestException:
        pytest.skip("No network access to Nobitex -- skipping end-to-end demo test.")


@pytest.mark.parametrize("profile_name", sorted(run.PROFILES))
def test_demo_mode_end_to_end_real_data_no_real_orders(monkeypatch, tmp_path, profile_name):
    _skip_if_offline()

    def _forbidden_send_order(*args, **kwargs):
        raise AssertionError(
            "NobitexExchangeClient.send_order was called during a demo-mode "
            "run -- this must never happen, real orders must never be placed."
        )

    monkeypatch.setattr(NobitexExchangeClient, "send_order", _forbidden_send_order)

    overrides = dict(run.PROFILES[profile_name])
    overrides["max_rounds"] = 2
    overrides["time_step"] = 1
    overrides["output_csv"] = str(tmp_path / f"{profile_name}_demo_e2e.csv")

    cfg = MMConfig(session=datetime.now().timestamp(), **overrides)
    cfg.algorithm_id = f"{cfg.algorithm_id}-demo-e2e"

    nobi_portfo = Nobitex_portfolio(cfg.token)
    real_exchange = NobitexExchangeClient(
        token=cfg.token, url_send_order=cfg.url_send_order,
        url_wallet_balance=cfg.url_wallet_balance, algorithm_id=cfg.algorithm_id,
    )
    demo_exchange = DemoExchangeClient(market_data=real_exchange, pair=cfg.pair)
    strategy = Algo1Strategy(cfg)

    bot = SimpleMarketMaker(cfg, nobi_portfo, demo_exchange, strategy=strategy,
                             persist_to_db=False)

    assert bot.conn is None
    assert bot.cursor is None

    result_df = bot.run()

    assert isinstance(result_df, pd.DataFrame)
    assert len(result_df) == 2
    assert list(result_df.columns) == EXPECTED_COLUMNS
    assert (result_df["Algorithm"] == cfg.algorithm_id).all()
    assert (result_df["Crypto"] == cfg.pair).all()
    assert result_df["Best Bid"].notna().all()
    assert result_df["Best Ask"].notna().all()
    assert (result_df["Best Bid"] <= result_df["Best Ask"]).all()
    assert Path(overrides["output_csv"]).exists()
