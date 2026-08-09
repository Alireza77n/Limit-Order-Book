from datetime import datetime, timezone

import pytest

import config
import demo_config
import run
from demo_config import overlay_path, write_overrides
from session import WorkerLease


TRADING_PROFILES = config.TRADING_PROFILES
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class StaticMarketData:
    def get_orderbook(self, symbol):
        return make_orderbook()


class FixedQuoteStrategy:
    def __init__(self, cfg):
        self.cfg = cfg

    def decide_order(self, ob, inv_current):
        return {
            "bid": 90.0,
            "ask": 110.0,
            "bid level": 1,
            "ask level": 1,
        }


def make_orderbook(depth=12):
    return {
        "bids": [[str(100 - level), "10"] for level in range(depth)],
        "asks": [[str(101 + level), "10"] for level in range(depth)],
        "lastTradePrice": "100",
    }


def build_runtime_bot(state_dir, monkeypatch, *, session="runtime-session"):
    state_dir = state_dir.resolve()
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(state_dir))
    cfg = run.make_cfg("usdt", mode="demo", session_id=session)
    cfg.output_csv = str(state_dir / "usdt-demo.csv")
    cfg.demo_state_path = str(state_dir / "usdt-ledger.json")
    cfg.demo_fee_state_path = str(state_dir / "fees.json")
    cfg.demo_archive_path = str(state_dir / "usdt-archive.jsonl")
    cfg.demo_result_db_path = str(state_dir / "usdt-results.sqlite3")
    return run.make_bot(cfg, mode="demo")


@pytest.fixture
def runtime_bot(tmp_path, monkeypatch):
    bot = build_runtime_bot(tmp_path, monkeypatch)
    with WorkerLease(tmp_path / ".runtime-test.lock", identity="runtime-test"):
        assert bot.config_sync.reload_at_round_boundary().ok
    return bot, tmp_path


def save_desired(state_dir, **values):
    return write_overrides(
        overlay_path(state_dir, "usdt"),
        values,
        pair="usdt",
        profiles=TRADING_PROFILES,
    )


def run_one_round(bot):
    bot.exchange._market_data = StaticMarketData()
    bot.strategy = FixedQuoteStrategy(bot.cfg)
    bot.cfg.max_rounds = 1
    bot.now_provider = lambda: NOW
    bot.monotonic_provider = lambda: 0.0
    bot.sleep_fn = lambda seconds: None
    return bot.run()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_size", 2.0),
        ("inv_max", 20.0),
        ("inv_min", 1.0),
        ("cancel_after_minutes", 3.0),
        ("depth_max", 4),
        ("time_step", 2),
    ],
)
def test_hot_field_updates_existing_engine(field, value, runtime_bot):
    bot, state_dir = runtime_bot
    original_cfg = bot.cfg
    save_desired(state_dir, **{field: value})

    result = bot.config_sync.reload_at_round_boundary()

    assert result.ok
    assert bot.cfg is original_cfg
    assert getattr(bot.cfg, field) == value
    assert bot.strategy.cfg is original_cfg


def test_hot_order_size_changes_next_round_order_amount(runtime_bot):
    bot, state_dir = runtime_bot
    save_desired(state_dir, order_size=2.0)

    run_one_round(bot)

    buy_orders = [order for order in bot.exchange.ledger.order_history.values() if order.side == "buy"]
    assert buy_orders
    assert buy_orders[0].original_amount == 2.0


def test_hot_inventory_bounds_change_strategy_skew(runtime_bot):
    bot, state_dir = runtime_bot
    book = make_orderbook()
    before = bot.strategy.diagnostics(book, 10.0, round_no=1, session_id=bot.session)["inv_d"]
    save_desired(state_dir, inv_min=1.0, inv_max=20.0)

    result = bot.config_sync.reload_at_round_boundary()
    after = bot.strategy.diagnostics(book, 10.0, round_no=1, session_id=bot.session)["inv_d"]

    assert result.ok
    assert after != before
    assert after == -1


def test_hot_inventory_max_changes_next_round_buy_cap(runtime_bot):
    bot, state_dir = runtime_bot
    save_desired(state_dir, order_size=2.0, inv_max=2.0)

    result = run_one_round(bot)

    assert result.iloc[0]["Buy Not Sent Reason"] == "inventory_cap"
    assert not [order for order in bot.exchange.ledger.order_history.values() if order.side == "buy"]


def test_hot_cancel_age_changes_next_stale_cancellation(runtime_bot):
    bot, state_dir = runtime_bot
    observed_ages = []
    original_cancel_stale = bot.exchange.ledger.cancel_stale

    def capture_cancel_age(*, max_age_seconds, now):
        observed_ages.append(max_age_seconds)
        return original_cancel_stale(max_age_seconds=max_age_seconds, now=now)

    bot.exchange.ledger.cancel_stale = capture_cancel_age
    save_desired(state_dir, cancel_after_minutes=3.0)

    run_one_round(bot)

    assert observed_ages[0] == 180.0


def test_hot_depth_max_changes_strategy_depth_cap(runtime_bot):
    bot, state_dir = runtime_bot
    save_desired(state_dir, depth_max=4)

    result = bot.config_sync.reload_at_round_boundary()

    assert result.ok
    assert bot._depth_cap(make_orderbook()) == 4


def test_hot_time_step_changes_next_scheduling_interval(runtime_bot):
    bot, state_dir = runtime_bot
    save_desired(state_dir, time_step=2)
    sleeps = []
    bot.monotonic_provider = lambda: 0.0
    bot.sleep_fn = sleeps.append

    result = bot.config_sync.reload_at_round_boundary()
    target = bot._wait_for_next_round(0.0)

    assert result.ok
    assert target == 2.0
    assert sleeps == [2.0]


def test_hot_invalid_batch_keeps_every_applied_field_unchanged(runtime_bot):
    bot, state_dir = runtime_bot
    before = {
        field: getattr(bot.cfg, field)
        for field in ("order_size", "inv_min", "inv_max", "depth_max", "time_step")
    }
    overlay_path(state_dir, "usdt").write_text(
        '{"order_size": 2.0, "inv_min": 30.0, "inv_max": 20.0, "depth_max": 4}'
    )

    result = bot.config_sync.reload_at_round_boundary()

    assert not result.ok
    assert {
        field: getattr(bot.cfg, field)
        for field in ("order_size", "inv_min", "inv_max", "depth_max", "time_step")
    } == before


def test_runtime_snapshot_reports_applied_and_pending_values(runtime_bot):
    assert hasattr(demo_config, "read_runtime_snapshot")
    bot, state_dir = runtime_bot
    save_desired(state_dir, order_size=2.0, commission_perc=0.02)

    result = bot.config_sync.reload_at_round_boundary()
    snapshot = demo_config.read_runtime_snapshot(state_dir, "usdt")

    assert result.ok
    assert snapshot["applied"]["order_size"] == 2.0
    assert snapshot["applied"]["commission_perc"] == 0.001
    assert snapshot["pending_new_session"]["commission_perc"] == 0.02
    assert snapshot["session_id"] == str(bot.session)
    assert snapshot["pair"] == "usdt"
    assert snapshot["status"] == "applied"


def test_runtime_snapshot_helpers_atomically_round_trip(tmp_path):
    assert hasattr(demo_config, "write_runtime_snapshot")
    document = {
        "schema_version": 1,
        "pair": "usdt",
        "session_id": "s1",
        "applied": {"order_size": 2.0},
        "pending_new_session": {},
        "desired_digest": "digest",
        "status": "applied",
        "error": None,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }

    demo_config.write_runtime_snapshot(tmp_path, "usdt", document)

    assert demo_config.runtime_snapshot_path(tmp_path, "usdt").name == "runtime_config_usdt.json"
    assert demo_config.read_runtime_snapshot(tmp_path, "usdt") == document


def test_failed_reload_keeps_last_snapshot_applied_values(runtime_bot):
    bot, state_dir = runtime_bot
    save_desired(state_dir, order_size=2.0)
    assert bot.config_sync.reload_at_round_boundary().ok
    applied_before = demo_config.read_runtime_snapshot(state_dir, "usdt")["applied"]
    overlay_path(state_dir, "usdt").write_text('{"order_size": "invalid"}')

    result = bot.config_sync.reload_at_round_boundary()
    failed_snapshot = demo_config.read_runtime_snapshot(state_dir, "usdt")

    assert not result.ok
    assert failed_snapshot["status"] == "error"
    assert failed_snapshot["error"]
    assert failed_snapshot["applied"] == applied_before
    assert bot.cfg.order_size == 2.0


def test_unchanged_hot_digest_does_not_rewrite_snapshot(runtime_bot, monkeypatch):
    bot, _ = runtime_bot
    writes = []
    original_write = demo_config.write_runtime_snapshot

    def capture_write(*args, **kwargs):
        writes.append(args)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(demo_config, "write_runtime_snapshot", capture_write)

    first = bot.config_sync.reload_at_round_boundary()
    second = bot.config_sync.reload_at_round_boundary()

    assert first.ok and second.ok
    assert writes == []


def test_hot_snapshot_write_failure_rolls_back_config(runtime_bot, monkeypatch):
    bot, state_dir = runtime_bot
    before = bot.cfg.order_size
    save_desired(state_dir, order_size=2.0)
    monkeypatch.setattr(
        demo_config,
        "write_runtime_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    result = bot.config_sync.reload_at_round_boundary()

    assert not result.ok
    assert bot.cfg.order_size == before


def test_unleased_construction_cannot_overwrite_active_worker_snapshot(
    tmp_path, monkeypatch
):
    active_snapshot = {
        "schema_version": 1,
        "pair": "usdt",
        "session_id": "active-session",
        "applied": {"order_size": 7.0},
        "pending_new_session": {},
        "desired_digest": "active-digest",
        "status": "applied",
        "error": None,
        "updated_at": "2026-08-09T12:00:00+00:00",
    }
    demo_config.write_runtime_snapshot(tmp_path, "usdt", active_snapshot)

    duplicate = build_runtime_bot(
        tmp_path, monkeypatch, session="duplicate-session"
    )

    assert demo_config.read_runtime_snapshot(tmp_path, "usdt") == active_snapshot

    lease_path = tmp_path / ".worker_usdtirt.lock"
    with WorkerLease(lease_path, identity="USDTIRT-demo"):
        result = duplicate.config_sync.reload_at_round_boundary()

    assert result.ok
    assert demo_config.read_runtime_snapshot(tmp_path, "usdt")["session_id"] == (
        "duplicate-session"
    )


def test_first_reload_error_rejects_stale_previous_session_snapshot(
    tmp_path, monkeypatch
):
    bot = build_runtime_bot(tmp_path, monkeypatch, session="current-session")
    stale_snapshot = {
        "schema_version": 1,
        "pair": "usdt",
        "session_id": "previous-session",
        "applied": {"order_size": 99.0, "commission_perc": 0.5},
        "pending_new_session": {"commission_perc": 0.9},
        "desired_digest": "stale-digest",
        "status": "applied",
        "error": None,
        "updated_at": "2026-08-08T12:00:00+00:00",
    }
    demo_config.write_runtime_snapshot(tmp_path, "usdt", stale_snapshot)
    overlay_path(tmp_path, "usdt").write_text('{"order_size": "invalid"}')

    with WorkerLease(
        tmp_path / ".worker_usdtirt.lock", identity="USDTIRT-demo"
    ):
        result = bot.config_sync.reload_at_round_boundary()
    snapshot = demo_config.read_runtime_snapshot(tmp_path, "usdt")

    assert not result.ok
    assert snapshot["status"] == "error"
    assert snapshot["session_id"] == "current-session"
    assert snapshot["applied"]["order_size"] == bot.cfg.order_size
    assert snapshot["applied"]["commission_perc"] == (
        bot.exchange.ledger.commission_perc
    )
    assert snapshot["desired_digest"] != "stale-digest"
