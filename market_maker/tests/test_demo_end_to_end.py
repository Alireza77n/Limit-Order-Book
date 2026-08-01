"""End-to-end tests for demo mode: the full run() loop, for real, against
real Nobitex market data, for every profile run.py exposes.

Unlike every other test in this suite, these make real network calls (to
Nobitex's public orderbook endpoint -- no auth, read-only, nothing placed).
They're kept in their own file and skip cleanly (not fail) if the network
is unavailable, so an offline/CI environment doesn't get a false failure.

Construction goes through run.make_cfg()/run.make_bot() -- the exact same
functions `python run.py --mode demo` calls -- instead of hand-duplicating
that wiring. An earlier version of this file *did* hand-duplicate it and
missed the inv_initial=0.0 demo-mode override as a result, which was only
caught because it (very visibly) sent Telegram messages showing non-zero
starting inventory. Routing through the real functions makes that class of
drift structurally impossible: if make_cfg/make_bot ever changes, this
test automatically exercises the new behavior instead of a stale copy.

Telegram is force-disabled here regardless of what's in this machine's
.env -- a test run is not a real trading session and must never post to
a real chat.

What these tests prove, for each of run.py's PROFILES, that pure unit
tests can't:
- The whole run() loop actually completes against live data without
  raising, using the exact same setup path the CLI uses.
- Demo mode actually starts with zero inventory in practice, not just in
  the code that's supposed to set it.
- NobitexExchangeClient.send_order is never called, even once, across a
  real run -- not just "DemoExchangeClient doesn't expose a path to it"
  (proven structurally elsewhere) but "the engine's real control flow
  never reaches it," verified by monkeypatching it to raise if called.
- No MySQL connection is attempted (persist_to_db=False).
- The output CSV is written with sane, complete rows.
"""

from pathlib import Path

import pandas as pd
import pytest
import requests

import run
from exchange.nobitex import NobitexExchangeClient

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


@pytest.mark.parametrize("profile_name", sorted(run.TRADING_PROFILES))
def test_demo_mode_end_to_end_real_data_no_real_orders(monkeypatch, tmp_path, profile_name):
    _skip_if_offline()

    # Never let a test post to the real Telegram chat, no matter what this
    # machine's .env has configured.
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)

    def _forbidden_send_order(*args, **kwargs):
        raise AssertionError(
            "NobitexExchangeClient.send_order was called during a demo-mode "
            "run -- this must never happen, real orders must never be placed."
        )

    monkeypatch.setattr(NobitexExchangeClient, "send_order", _forbidden_send_order)

    cfg = run.make_cfg(profile_name, mode="demo")
    cfg.max_rounds = 2
    cfg.time_step = 1
    cfg.output_csv = str(tmp_path / f"{profile_name}_demo_e2e.csv")

    assert cfg.inv_initial == 0.0  # demo mode must start flat -- this is the bug that shipped once

    bot = run.make_bot(cfg, algorithm="algo1", exchange_name="nobitex", mode="demo")

    assert bot.conn is None
    assert bot.cursor is None
    assert bot.inv_initial == 0.0

    result_df = bot.run()

    assert isinstance(result_df, pd.DataFrame)
    assert len(result_df) == 2
    assert list(result_df.columns) == EXPECTED_COLUMNS
    assert (result_df["Algorithm"] == cfg.algorithm_id).all()
    assert (result_df["Crypto"] == cfg.pair).all()
    assert result_df["Best Bid"].notna().all()
    assert result_df["Best Ask"].notna().all()
    assert (result_df["Best Bid"] <= result_df["Best Ask"]).all()
    # Round 1's inventory is logged before any order this run could have
    # filled -- it must be exactly the flat starting point, not an
    # inherited real position.
    assert result_df["inv_current"].iloc[0] == pytest.approx(0.0)
    assert Path(cfg.output_csv).exists()
