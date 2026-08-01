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
    with pytest.raises(KeyboardInterrupt):
        installed[signal.SIGTERM](signal.SIGTERM, None)
