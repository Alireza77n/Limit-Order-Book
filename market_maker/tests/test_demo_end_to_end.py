"""Deterministic two-symbol Demo replay with zero-call safety tripwires."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import run
from engine import SimpleMarketMaker
from exchange.base import ExchangeClient
from exchange.nobitex import NobitexExchangeClient


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


class ReplayMarketData(ExchangeClient):
    def get_orderbook(self, symbol):
        return {
            "bids": [[str(1_000_000 - level * 1_000), "10"] for level in range(11)],
            "asks": [[str(1_001_000 + level * 1_000), "10"] for level in range(11)],
            "lastTradePrice": "1000500",
        }

    def wallet_balance(self, currency):
        raise AssertionError("authenticated wallet access is forbidden")

    def send_order(self, *args, **kwargs):
        raise AssertionError("real order access is forbidden")

    def get_order_list_status(self, *args, **kwargs):
        raise AssertionError("real status access is forbidden")

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_all_open_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_old_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")


def test_two_symbol_demo_replay_shares_session_isolates_state_and_has_zero_real_calls(
    monkeypatch, tmp_path
):
    calls = {"send": 0, "cancel": 0, "bulk_cancel": 0, "stale_cancel": 0, "mysql": 0}

    def forbidden(name):
        def tripwire(*args, **kwargs):
            calls[name] += 1
            raise AssertionError(f"forbidden real side effect: {name}")
        return tripwire

    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr("engine.mysql.connector.connect", forbidden("mysql"))
    monkeypatch.setattr(NobitexExchangeClient, "send_order", forbidden("send"))
    monkeypatch.setattr(NobitexExchangeClient, "cancel_order", forbidden("cancel"))
    monkeypatch.setattr(NobitexExchangeClient, "cancel_all_open_orders", forbidden("bulk_cancel"))
    monkeypatch.setattr(NobitexExchangeClient, "cancel_old_orders", forbidden("stale_cancel"))

    # make_cfg(mode="demo") reads any live Admin-desired overlay from
    # run.DEMO_STATE_DIR at construction time, not just at reload -- it must
    # be patched to an empty directory before make_cfg runs, or this
    # replay's config (and therefore its fill outcome) depends on whatever
    # the host's live Demo overlay currently contains.
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))

    shared_session = "deterministic-shared-session"
    fee_path = tmp_path / "fee-scopes.json"
    results = {}
    ledgers = {}

    for profile_name in sorted(run.TRADING_PROFILES):
        cfg = run.make_cfg(profile_name, mode="demo", session_id=shared_session)
        cfg.max_rounds = 2
        cfg.time_step = 0
        cfg.output_csv = str(tmp_path / f"{profile_name}_demo.csv")
        cfg.demo_state_path = str(tmp_path / f"{profile_name}_state.json")
        cfg.demo_fee_state_path = str(fee_path)
        cfg.demo_level1_fill_delay_seconds = 0
        # Constant order size keeps the replay's fill outcome identical for
        # both pairs; linear inventory sizing is covered in
        # test_inventory_sizing.py and test_demo_engine_lifecycle.py.
        monkeypatch.setitem(
            run.TRADING_PROFILES[profile_name], "inventory_sizing_enabled", False
        )

        bot = run.make_bot(cfg, algorithm="algo1", mode="demo")     # each profile on its own exchange
        bot.exchange._market_data = ReplayMarketData()
        times = iter([T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)])
        bot.now_provider = lambda times=times: next(times)
        bot.sleep_fn = lambda seconds: None

        results[profile_name] = bot.run()
        ledgers[profile_name] = bot.exchange.ledger

        assert bot.conn is None
        assert bot.cursor is None
        assert cfg.inv_initial == 0.0
        assert Path(cfg.output_csv).exists()

    assert calls == {"send": 0, "cancel": 0, "bulk_cancel": 0, "stale_cancel": 0, "mysql": 0}
    assert {ledger.session_id for ledger in ledgers.values()} == {shared_session}
    assert ledgers["btc"].symbol == "BTCIRT"
    assert ledgers["usdt"].symbol == "USDTIRT"
    assert set(ledgers["btc"].order_history).isdisjoint(ledgers["usdt"].order_history)

    for profile_name, frame in results.items():
        assert isinstance(frame, pd.DataFrame)
        assert len(frame) == 2
        assert list(frame.columns) == SimpleMarketMaker.DEMO_COLUMNS
        assert set(frame["Session_id"]) == {shared_session}
        assert set(frame["Mode"]) == {"demo"}
        assert set(frame["Crypto"]) == {run.TRADING_PROFILES[profile_name]["pair"]}
        assert frame["Best Bid"].notna().all()
        assert frame["Best Ask"].notna().all()
        # Zero inventory: Algorithm 1 v2 quotes the bid side only, the
        # paper order rests (the replay book never moves) and is cancelled
        # with the session.
        assert isinstance(frame["Buy Order ID"].iloc[0], str)
        assert frame["Buy Status"].iloc[0] == "cancelled"
        assert frame["Buy Original Amount"].iloc[0] > 0
        assert frame["Sell Status"].iloc[0] == "not_sent_one_sided_quote"
        assert frame["Buy Status"].iloc[1] == "not_sent_resting_order_kept"
        assert frame["Sell Status"].iloc[1] == "not_sent_one_sided_quote"

    scopes = ledgers["btc"].fee_scopes()
    expected_combined = ledgers["btc"].fees_quote + ledgers["usdt"].fees_quote
    assert scopes.session_all_symbols == pytest.approx(expected_combined)
