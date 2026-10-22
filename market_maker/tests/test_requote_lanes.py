"""The engine rules of 20 October 2026 and the demo lanes.

* requote band: hysteresis_ticks ticks or requote_band_perc of the price,
  whichever is wider, shared by both strategies and the engine;
* one order per side: a resting order outside the band of the new price is
  cancelled ("replaced"), one inside it keeps the new order from being sent;
* orders live cancel_after_minutes (3) and the target is 0.02 % by default (0.20 % on 20-22 October);
* the trend pause blocks buys after a fall and sells after a rise (0.3 %
  over 10 minutes by default);
* a lane (run.py) has its own files, a pinned algorithm, a runtime snapshot
  of its own, and stops through the engine's stop event; LaneRunner starts
  and stops lanes as the overlay's algo*_enabled flags change.
"""
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run
from config import MMConfig, TRADING_PROFILES
from demo_config import FIELD_DEFINITIONS, overlay_path, read_runtime_snapshot, write_overrides
from engine import CoordinatedShutdown, SimpleMarketMaker
from exchange.base import ExchangeClient
from strategy.algo1 import Algo1Strategy
from strategy.algo3 import Algo3Strategy

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- settings
def test_new_fields_exist_with_the_documented_defaults():
    for field in ("requote_band_perc", "trend_window_seconds", "trend_pause_perc", "algo1_enabled", "algo3_enabled"):
        assert FIELD_DEFINITIONS[field].mode.value == "hot"
    for pair in ("btc", "usdt"):
        profile = TRADING_PROFILES[pair]
        assert profile["requote_band_perc"] == 0.0003
        assert profile["trend_pause_perc"] == 0.003 and profile["trend_window_seconds"] == 600.0
        assert profile["algo1_enabled"] is True and profile["algo3_enabled"] is True
        assert profile["cancel_after_minutes"] == 3
        assert profile["exp_ret_min"] == 0.0002
    from forward_config import FORWARD_FIELDS
    assert {"requote_band_perc", "trend_window_seconds", "trend_pause_perc"} <= FORWARD_FIELDS
    assert not {"algo1_enabled", "algo3_enabled"} & FORWARD_FIELDS


# ---------------------------------------------------------------- the band
def test_requote_band_is_the_wider_of_ticks_and_percent():
    cfg = MMConfig(pair="USDTIRT", hysteresis_ticks=5, requote_band_perc=0.0003)
    strategy = Algo3Strategy(cfg)
    assert strategy.requote_band(2_650_000.0) == pytest.approx(795.0)          # 0.03 % wins over 50 rial
    assert strategy.requote_band(10_000.0) == pytest.approx(50.0)              # ticks win on a small price
    cfg.requote_band_perc = 0.0
    assert strategy.requote_band(2_650_000.0) == pytest.approx(50.0)
    cfg.hysteresis_ticks = 0
    assert strategy.requote_band(2_650_000.0) == 0.0


def test_algo3_hysteresis_keeps_a_pair_inside_the_percent_band():
    clock = {"t": 0.0}
    cfg = MMConfig(pair="USDTIRT", hysteresis_ticks=5, requote_band_perc=0.0003, refresh_seconds=300.0,
                   commission_perc=0.0008, exp_ret_min=0.0001)      # the remembered pair must still clear the bar
    strategy = Algo3Strategy(cfg, clock=lambda: clock["t"])
    assert strategy._apply_hysteresis(2_650_000.0, 2_660_000.0) == (2_650_000.0, 2_660_000.0, False)
    clock["t"] = 10.0
    assert strategy._apply_hysteresis(2_650_500.0, 2_660_500.0)[2] is True     # 500 rial: inside 0.03 %
    assert strategy._apply_hysteresis(2_651_000.0, 2_660_000.0)[2] is False    # 1,000 rial: outside


def test_algo1_hysteresis_uses_the_same_band():
    clock = {"t": 0.0}
    cfg = MMConfig(pair="USDTIRT", hysteresis_ticks=5, requote_band_perc=0.0003, refresh_seconds=300.0)
    strategy = Algo1Strategy(cfg, clock=lambda: clock["t"])
    strategy._apply_hysteresis(2_650_000.0, 2_660_000.0)
    clock["t"] = 10.0
    bid, ask, kept = strategy._apply_hysteresis(2_650_600.0, 2_660_600.0)
    assert kept == {"bid": True, "ask": True} and (bid, ask) == (2_650_000.0, 2_660_000.0)


def test_engine_resting_order_guard_uses_the_band():
    from tests.helpers import make_bot
    bot = make_bot(MMConfig(pair="USDTIRT", hysteresis_ticks=5, requote_band_perc=0.0003))
    resting = [(2_650_000.0, 1.0)]
    assert bot._resting_order_blocks(2_650_700.0, resting) is True       # 70 ticks, inside 0.03 %
    assert bot._resting_order_blocks(2_651_000.0, resting) is False
    bot.cfg.requote_band_perc = 0.0
    assert bot._resting_order_blocks(2_650_700.0, resting) is False      # ticks only: 5 ticks
    assert bot._resting_order_blocks(2_650_030.0, resting) is True


# ---------------------------------------------------------------- the demo loop
class _MarketData(ExchangeClient):
    """A book whose mid can be moved between rounds."""

    def __init__(self):
        self.bid, self.ask = 99_000.0, 100_000.0

    def get_orderbook(self, symbol):
        return {"bids": [[str(self.bid), "10"], [str(self.bid - 1_000), "10"]],
                "asks": [[str(self.ask), "10"], [str(self.ask + 1_000), "10"]],
                "lastTradePrice": str((self.bid + self.ask) / 2)}

    def wallet_balance(self, currency):
        raise AssertionError("authenticated wallet access is forbidden")

    def send_order(self, *args, **kwargs):
        raise AssertionError("real order access is forbidden")

    def get_order_list_status(self, *args, **kwargs):
        raise AssertionError("real order-status access is forbidden")

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_all_open_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")

    def cancel_old_orders(self, *args, **kwargs):
        raise AssertionError("real cancellation is forbidden")


class _Quotes:
    """A strategy that quotes the prices it is told to, round by round."""

    def __init__(self, prices):
        self.prices = list(prices)

    def decide_order(self, ob, inv_current):
        bid, ask = self.prices.pop(0) if self.prices else self.prices_last
        self.prices_last = (bid, ask)
        return {"bid": bid, "ask": ask, "bid level": 1, "ask level": 1}


def _demo_bot(monkeypatch, tmp_path, strategy, *, rounds, band=0.0003, ticks=5, **overrides):
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "inventory_sizing_enabled", False)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "hysteresis_ticks", ticks)
    monkeypatch.setitem(run.TRADING_PROFILES["usdt"], "requote_band_perc", band)
    for key, value in overrides.items():
        monkeypatch.setitem(run.TRADING_PROFILES["usdt"], key, value)
    cfg = run.make_cfg("usdt", mode="demo", session_id="requote-demo")
    cfg.output_csv = str(tmp_path / "usdt-demo.csv")
    cfg.demo_state_path = str(tmp_path / "usdt-state.json")
    cfg.demo_fee_state_path = str(tmp_path / "fee-state.json")
    cfg.demo_result_db_path = str(tmp_path / "usdt-demo.sqlite3")
    cfg.cash_initial = 10_000_000.0
    cfg.demo_inv_initial = 20.0
    cfg.order_size = 1.0
    cfg.max_rounds = rounds
    cfg.time_step = 0
    bot = run.make_bot(cfg, mode="demo")
    market = _MarketData()
    bot.exchange._market_data = market
    bot.strategy = strategy
    bot.telegram_reporter = None
    times = iter([T0 + timedelta(seconds=10 * n) for n in range(rounds + 2)])
    bot.now_provider = lambda: next(times)
    bot.sleep_fn = lambda seconds: None
    return bot, market


def _orders(bot):
    return sorted(bot.exchange.ledger.order_history.values(), key=lambda o: o.client_order_id)


def test_a_new_price_outside_the_band_replaces_the_resting_order(monkeypatch, tmp_path):
    """Round 1 quotes 90,000/110,000; round 2 quotes 85,000/115,000 (outside
    the band of 50 rial): the round-1 orders are cancelled as "replaced" and
    the new ones rest alone. Round 3 quotes 85,010/115,000 (one tick away,
    inside the band): nothing new is sent."""
    bot, _ = _demo_bot(monkeypatch, tmp_path, _Quotes([(90_000.0, 110_000.0), (85_000.0, 115_000.0), (85_010.0, 115_000.0)]), rounds=3)

    frame = bot.run()

    orders = _orders(bot)
    reasons = {o.client_order_id: o.fill_reason for o in orders}
    assert len(orders) == 4                                   # two per round for rounds 1 and 2, none in round 3
    replaced = sorted(cid for cid, reason in reasons.items() if reason == "replaced")
    assert replaced == ["usdtirt-buy-1-1792483200000", "usdtirt-sell-1-1792483200000"]
    # round 2's orders rested alone until the session's end cancelled them
    assert {reasons[cid] for cid in reasons if "-2-" in cid} == {"session_end"}
    assert sorted(o.limit_price for o in orders if o.fill_reason == "session_end") == [85_000.0, 115_000.0]
    assert frame["Buy Not Sent Reason"].tolist()[2] == "resting_order_kept"


def test_the_replaced_orders_are_recorded_on_their_rounds(monkeypatch, tmp_path):
    bot, _ = _demo_bot(monkeypatch, tmp_path, _Quotes([(90_000.0, 110_000.0), (85_000.0, 115_000.0)]), rounds=2)
    frame = bot.run()
    assert frame["Buy Status"].tolist()[0] == "cancelled" and frame["Buy Fill Reason"].tolist()[0] == "replaced"
    assert frame["Buy Status"].tolist()[1] in ("open", "cancelled")       # round 2's own order; the session end cancels it


def test_orders_live_cancel_after_minutes(monkeypatch, tmp_path):
    """With the band off and the same price every round, the first orders are
    cancelled as stale once older than cancel_after_minutes (3 minutes =
    18 rounds of 10 s here, so by round 20)."""
    bot, _ = _demo_bot(monkeypatch, tmp_path, _Quotes([(90_000.0, 110_000.0)]), rounds=20, band=0.0, ticks=0)
    frame = bot.run()
    assert bot.cfg.cancel_after_minutes == 3
    assert frame["Buy Fill Reason"].tolist()[0] == "stale_timeout"


def test_trend_pause_blocks_buys_after_a_fall_and_cancels_the_resting_buy(monkeypatch, tmp_path):
    bot, market = _demo_bot(monkeypatch, tmp_path, _Quotes([(90_000.0, 110_000.0)]), rounds=4,
                            trend_window_seconds=20.0, trend_pause_perc=0.005)
    rounds = {"n": 0}
    original = market.get_orderbook

    def falling(symbol):
        rounds["n"] += 1
        if rounds["n"] >= 3:
            market.bid, market.ask = 95_000.0, 96_000.0        # the mid falls 4 %
        return original(symbol)

    market.get_orderbook = falling
    frame = bot.run()
    reasons = frame["Buy Not Sent Reason"].tolist()
    assert "trend_pause" in reasons[2:]
    assert frame["Sell Not Sent Reason"].tolist()[2] == "resting_order_kept"   # sells are untouched
    assert any(o.fill_reason == "trend_pause" for o in _orders(bot) if o.side == "buy")


def test_trend_pause_is_on_by_default_at_0_3_percent_over_10_minutes(monkeypatch, tmp_path):
    """The default (0.3 % over 600 s): nothing until the samples cover half
    the window, nothing on a move inside 0.3 %, buys blocked after a fall
    beyond it, sells after a rise beyond it."""
    bot, _ = _demo_bot(monkeypatch, tmp_path, _Quotes([(90_000.0, 110_000.0)]), rounds=1)
    assert bot.cfg.trend_pause_perc == 0.003 and bot.cfg.trend_window_seconds == 600.0
    assert bot._trend_paused_sides(100_000.0, 0.0) == set()
    assert bot._trend_paused_sides(99_000.0, 200.0) == set()          # a 1 % fall, but only 200 s of samples
    assert bot._trend_paused_sides(99_800.0, 400.0) == set()          # 0.2 %: inside the threshold
    assert bot._trend_paused_sides(99_600.0, 450.0) == {"buy"}        # 0.4 % down: no buying into the fall
    assert bot._trend_paused_sides(100_500.0, 950.0) == {"sell"}      # 0.7 % up from the oldest sample still in the window (t = 400)
    bot.cfg.trend_pause_perc = 0.0
    assert bot._trend_paused_sides(90_000.0, 1_200.0) == set()        # 0 switches it off


def test_stop_event_ends_the_loop_like_a_signal(monkeypatch, tmp_path):
    bot, _ = _demo_bot(monkeypatch, tmp_path, _Quotes([(90_000.0, 110_000.0)]), rounds=50)
    bot.cfg.time_step = 1
    bot.stop_event = threading.Event()
    calls = {"n": 0}

    def sleep(seconds):
        calls["n"] += 1
        if calls["n"] == 3:
            bot.stop_event.set()

    bot.sleep_fn = sleep
    frame = bot.run()
    assert len(frame) < 50
    assert all(o.status == "cancelled" for o in _orders(bot))              # a stopped lane cancels its orders


# ---------------------------------------------------------------- lanes
def test_lane_config_has_its_own_files_and_a_pinned_algorithm(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DEMO_STATE_DIR", str(tmp_path))       # demo_state_path() reads it at call time
    cfg = run.make_cfg("btc", "demo", session_id="s1", lane="algo3")
    assert cfg.lane == "algo3" and cfg.algorithm == "algo3"
    assert Path(cfg.output_csv).name == "btc_track_demo_algo3.csv"
    assert Path(cfg.algo1_diagnostics_path).name == "btc_track_demo_algo3_diagnostics.jsonl"
    assert Path(cfg.demo_state_path) == tmp_path / "paper_s1_btcirt_algo3.json"
    assert Path(cfg.demo_archive_path) == tmp_path / "paper_s1_btcirt_algo3_archive.jsonl"
    assert Path(cfg.demo_fee_state_path) == tmp_path / "paper_fee_scopes.json"      # shared
    with pytest.raises(ValueError):
        run.make_cfg("btc", "demo", session_id="s1", lane="algo2")


def test_lane_keeps_its_algorithm_when_the_overlay_names_another(monkeypatch, tmp_path):
    """The overlay's `algorithm` field is live-only: a lane reports it as
    applied but never rebuilds its strategy from it, and it publishes its
    snapshot to its own file."""
    monkeypatch.setattr("engine.TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr("engine.TELEGRAM_CHAT_ID", None)
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    write_overrides(overlay_path(tmp_path, "usdt"), {"algorithm": "algo1"}, pair="usdt", profiles=TRADING_PROFILES)
    cfg = run.make_cfg("usdt", "demo", session_id="lane-session", lane="algo3")
    cfg.output_csv = str(tmp_path / "usdt_track_demo_algo3.csv")
    bot = run.make_bot(cfg, "algo3", mode="demo")
    bot.exchange.ledger.persist_initial_state()
    result = bot.config_sync.reload_at_round_boundary()
    assert result.ok, result.error
    bot._sync_strategy_with_config()
    assert isinstance(bot.strategy, Algo3Strategy) and bot.cfg.algorithm == "algo3"
    snapshot = read_runtime_snapshot(tmp_path, "usdt", "algo3")
    assert snapshot["status"] == "applied" and snapshot["applied"]["algorithm"] == "algo1"
    assert read_runtime_snapshot(tmp_path, "usdt") is None                 # nothing in the single-worker file


class _FakeBot:
    def __init__(self, lane):
        self.lane = lane
        self.stop_event = None
        self.started = threading.Event()
        self.cfg = type("Cfg", (), {"output_csv": f"{lane}.csv"})()
        self.exchange = type("Exchange", (), {"ledger": type("Ledger", (), {"persist_initial_state": lambda self: None})()})()
        self.config_sync = type("Sync", (), {"reload_at_round_boundary": lambda self: type("R", (), {"ok": True})()})()

    def run(self):
        self.started.set()
        self.stop_event.wait(5)


def test_lane_runner_starts_and_stops_lanes_as_the_overlay_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    runner = run.LaneRunner("usdt", poll_seconds=0, sleep=lambda s: None)
    built = []

    def build(lane):
        bot = _FakeBot(lane)
        built.append(bot)
        return bot

    runner.build = build
    write_overrides(overlay_path(tmp_path, "usdt"), {"algo1_enabled": True, "algo3_enabled": True},
                    pair="usdt", profiles=TRADING_PROFILES)
    runner.reconcile()
    assert sorted(runner.lanes) == ["algo1", "algo3"]
    assert all(bot.started.wait(2) for bot in built)
    assert all(runner.lanes[lane]["thread"].is_alive() for lane in runner.lanes)

    write_overrides(overlay_path(tmp_path, "usdt"), {"algo1_enabled": False, "algo3_enabled": True},
                    pair="usdt", profiles=TRADING_PROFILES)
    runner.reconcile()
    assert sorted(runner.lanes) == ["algo3"]
    assert built[0].stop_event.is_set() and not built[0].started.is_set() or True
    runner.reconcile()                                                      # idempotent
    assert sorted(runner.lanes) == ["algo3"] and len(built) == 2
    for lane in list(runner.lanes):
        runner.stop_lane(lane)
    assert not runner.lanes


def test_lane_runner_run_stops_every_lane_on_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    bots = []
    runner = run.LaneRunner("usdt", poll_seconds=0, sleep=lambda s: (_ for _ in ()).throw(CoordinatedShutdown(15)))
    runner.build = lambda lane: bots.append(_FakeBot(lane)) or bots[-1]
    runner.run()
    assert len(bots) == 2 and all(bot.stop_event.is_set() for bot in bots)
    assert not runner.lanes


def test_demo_main_without_algorithm_runs_lanes_under_one_lease(monkeypatch, tmp_path):
    import argparse
    events = []
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(run, "install_shutdown_signal_handlers", lambda: None)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args",
                        lambda self: argparse.Namespace(profile="btc", mode="demo", algorithm=None, exchange="nobitex", continuous=True))

    class Lease:
        def __init__(self, path, identity):
            events.append(("lease", Path(path).name, identity))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("release",))

    class Runner:
        def __init__(self, profile, exchange, continuous):
            events.append(("runner", profile, exchange, continuous))

        def run(self):
            events.append(("run",))

    monkeypatch.setattr(run, "WorkerLease", Lease)
    monkeypatch.setattr(run, "LaneRunner", Runner)
    run.main()
    assert events == [("lease", ".worker_btcirt.lock", "BTCIRT-demo"), ("runner", "btc", "nobitex", True), ("run",), ("release",)]
