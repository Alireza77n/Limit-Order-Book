from datetime import datetime, timedelta
import signal

import pytest

import run
from engine import SimpleMarketMaker
from session import DuplicateWorkerError, WorkerLease


def test_continuous_config_disables_round_limit():
    cfg = run.make_cfg("btc", mode="demo", session_id="shared", continuous=True)

    assert cfg.continuous is True
    assert cfg.max_rounds is None


def test_continuous_engine_ignores_elapsed_horizon():
    bot = object.__new__(SimpleMarketMaker)
    bot.cfg = run.make_cfg("btc", mode="demo", session_id="shared", continuous=True)
    bot.horizon_dt = datetime.now() - timedelta(days=1)

    assert bot._within_horizon() is True

    bot.cfg.continuous = False
    assert bot._within_horizon() is False


def test_duplicate_worker_lease_fails_closed_and_releases(tmp_path):
    path = tmp_path / ".worker_btc.lock"
    first = WorkerLease(path, identity="btc-demo")
    second = WorkerLease(path, identity="btc-demo-duplicate")

    first.acquire()
    with pytest.raises(DuplicateWorkerError, match="btc-demo-duplicate"):
        second.acquire()
    first.release()

    second.acquire()
    second.release()


def test_shutdown_signal_handlers_turn_sigterm_into_clean_interrupt(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda number, handler: installed.setdefault(number, handler))

    run.install_shutdown_signal_handlers()

    assert signal.SIGTERM in installed
    assert signal.SIGINT in installed
    with pytest.raises(run.CoordinatedShutdown) as raised:
        installed[signal.SIGTERM](signal.SIGTERM, None)
    assert raised.value.signum == signal.SIGTERM


def test_monotonic_deadline_subtracts_work_and_skips_missed_intervals():
    bot = object.__new__(SimpleMarketMaker)
    bot.cfg = run.make_cfg("usdt", mode="demo", session_id="shared")
    bot.cfg.time_step = 10
    sleeps = []
    monotonic_values = iter([2.0, 13.0, 25.0])
    bot.monotonic_provider = lambda: next(monotonic_values)
    bot.sleep_fn = sleeps.append

    target = bot._wait_for_next_round(0.0)
    assert target == 10.0
    target = bot._wait_for_next_round(target)
    assert target == 20.0
    target = bot._wait_for_next_round(target)
    assert target == 30.0
    assert sleeps == [8.0, 7.0, 5.0]


def test_monotonic_deadline_does_not_skip_when_work_ends_exactly_on_target():
    bot = object.__new__(SimpleMarketMaker)
    bot.cfg = run.make_cfg("usdt", mode="demo", session_id="shared")
    bot.cfg.time_step = 10
    bot.monotonic_provider = lambda: 10.0
    sleeps = []
    bot.sleep_fn = sleeps.append

    assert bot._wait_for_next_round(0.0) == 10.0
    assert sleeps == []
