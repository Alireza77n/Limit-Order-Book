"""Single entry point for running the market-making bot.

Replaces the old demo_btc.py / demo_usdt.py (near-duplicate scripts that
only differed in their MMConfig values) with one script parameterized by
--profile, --algorithm, --exchange, and --mode. Adding a new currency pair
means adding a TRADING_PROFILES entry in config.py; adding a new algorithm
or exchange means adding one STRATEGIES/EXCHANGES entry below -- no new
files either way.

    python run.py --profile btc --mode real    # live trading (explicit mode required)
    python run.py --profile usdt --mode real   # live trading (explicit mode required)
    python run.py --profile btc --mode demo    # paper trading: real market data, simulated fills, no real orders

README.md (Run, test, deploy) has the commands; docs/MM_algo3.md section 7.6
describes the demo lanes.

NOTE: --exchange only selects the ExchangeClient used for quotes/orders.
Nobitex_portfolio (portfolio.py) is still Nobitex-specific and isn't
behind an exchange interface, so portfolio valuation doesn't yet follow
--exchange. A known limitation, stated here rather than glossed over.
"""

import argparse
import json
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from config import (
    DEMO_STATE_DIR,
    MMConfig,
    TRADING_PROFILES,
    demo_csv_path,
    demo_state_path,
)
from portfolio import Nobitex_portfolio
from engine import CoordinatedShutdown, SimpleMarketMaker
from exchange.nobitex import NobitexExchangeClient
from exchange.demo import DemoExchangeClient
from strategy.catalog import SELECTABLE_ALGORITHMS, STRATEGIES, build_strategy
from session import WorkerLease, load_stack_session
from paper import PaperLedger
from demo_config import (
    RuntimeConfigSynchronizer,
    apply_overrides,
    load_desired_config,
)
from forward_config import desired_config as load_forward_desired

# STRATEGIES (the --algorithm choices) comes from strategy/catalog.py: the
# implemented algorithms plus the Algorithm 2 stub -- see strategy/catalog.py.

EXCHANGES = {
    "nobitex": NobitexExchangeClient,
}


def _shutdown_signal_handler(signum, frame):
    """Route container stop signals through the engine's cleanup path."""
    raise CoordinatedShutdown(signum)


def install_shutdown_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _shutdown_signal_handler)
    signal.signal(signal.SIGINT, _shutdown_signal_handler)


def apply_lane_paths(cfg: MMConfig, lane: str) -> MMConfig:
    """Give a demo lane (one algorithm of a pair's worker) its own files and
    pin its algorithm: <pair>_track_demo_<lane>.csv (+ .sqlite3 next to it),
    <pair>_track_demo_<lane>_diagnostics.jsonl, and the paper ledger
    paper_<session>_<pair>_<lane>.json (+ _archive.jsonl). The fee-scopes
    file stays shared by every lane and pair."""
    cfg.lane = lane
    cfg.algorithm = lane
    stem, extension = os.path.splitext(cfg.output_csv)
    cfg.output_csv = f"{stem}_{lane}{extension}"
    cfg.algo1_diagnostics_path = f"{stem}_{lane}_diagnostics.jsonl"
    safe_session = str(cfg.session).replace("/", "_")
    cfg.demo_state_path = demo_state_path(f"paper_{safe_session}_{cfg.pair.lower()}_{lane}.json")
    cfg.demo_archive_path = demo_state_path(f"paper_{safe_session}_{cfg.pair.lower()}_{lane}_archive.jsonl")
    return cfg


def make_cfg(
    profile: str, mode: str = "real", session_id=None, continuous: bool = False,
    lane: Optional[str] = None,
) -> MMConfig:
    """Build the MMConfig for a profile (config.TRADING_PROFILES), applying
    demo-mode's adjustments (configurable starting inventory, separate CSV,
    algorithm_id suffix) when mode == "demo", and a demo lane's own files
    when ``lane`` (an algorithm key) is given. Pulled out of main() so tests
    exercise the exact same setup the real CLI does, instead of a
    hand-duplicated (and easily drifting) copy of it."""
    session_file = os.environ.get("DEMO_SESSION_FILE") if mode == "demo" else None
    if session_id is None and session_file:
        session_id = load_stack_session(session_file)
    session_id = datetime.now().timestamp() if session_id is None else session_id
    cfg = MMConfig(
        session=session_id,
        mode=mode,
        profile_name=profile,
        demo_config_state_dir=DEMO_STATE_DIR if mode == "demo" else None,
        **TRADING_PROFILES[profile],
    )
    cfg.continuous = bool(continuous)
    if cfg.continuous:
        cfg.max_rounds = None
    if mode == "demo":
        cfg.algorithm_id = f"{cfg.algorithm_id}-demo"
        cfg.output_csv = demo_csv_path(cfg.output_csv)
        # Demo starting inventory is a per-profile config value (see
        # config.TRADING_PROFILES' demo_inv_initial) -- paper trading
        # starts flat by default, no inherited real position.
        cfg.inv_initial = cfg.demo_inv_initial
        safe_session = str(cfg.session).replace("/", "_")
        cfg.demo_state_path = demo_state_path(
            f"paper_{safe_session}_{cfg.pair.lower()}.json"
        )
        cfg.demo_fee_state_path = demo_state_path("paper_fee_scopes.json")
        cfg.demo_archive_path = demo_state_path(
            f"paper_{safe_session}_{cfg.pair.lower()}_archive.jsonl"
        )
        # Admin-editable per-pair overlay (market_maker/demo_config.py) --
        # Demo mode only, applied last so it can tune the profile above.
        desired = load_desired_config(profile, DEMO_STATE_DIR, TRADING_PROFILES)
        apply_overrides(cfg, desired)
        if lane:
            if lane not in SELECTABLE_ALGORITHMS:
                raise ValueError(f"unknown demo lane: {lane}")
            apply_lane_paths(cfg, lane)
    elif os.environ.get("LOB_FORWARD_CONFIG_DIR"):
        # Live forward overlays are deliberately separate from Demo state.
        # They are loaded only when an explicitly configured forward runtime
        # is used by the operator's worker service.
        desired = load_forward_desired(profile, TRADING_PROFILES)
        apply_overrides(cfg, desired)
    return cfg


def make_bot(cfg: MMConfig, algorithm: Optional[str] = None, exchange_name: str = "nobitex",
             mode: str = "real") -> SimpleMarketMaker:
    """Build the SimpleMarketMaker for an already-constructed cfg (see
    make_cfg). Also pulled out of main() so it can be reused verbatim.

    ``algorithm`` (the CLI's --algorithm) overrides cfg.algorithm, the value
    the profile and the Demo/live configuration overlay asked for; None
    keeps that value."""
    is_demo = mode == "demo"
    if algorithm is not None:
        cfg.algorithm = algorithm
    is_forward = bool(os.environ.get("LOB_FORWARD_CONFIG_DIR"))

    adapter_api_key = None if is_demo else cfg.api_key
    adapter_secret_key = None if is_demo else cfg.secret_key
    nobi_portfo = Nobitex_portfolio(adapter_api_key, adapter_secret_key)
    real_exchange = EXCHANGES[exchange_name](
        api_key=adapter_api_key,
        secret_key=adapter_secret_key,
        url_send_order=None if is_demo else cfg.url_send_order,
        url_wallet_balance=None if is_demo else cfg.url_wallet_balance,
        algorithm_id=cfg.algorithm_id,
        trading_enabled=not is_demo,
    )
    if is_demo:
        if cfg.demo_state_path and os.path.exists(cfg.demo_state_path):
            ledger = PaperLedger.load(cfg.demo_state_path)
            if ledger.session_id != str(cfg.session) or ledger.symbol != cfg.pair.upper():
                raise RuntimeError("Demo Mode state file does not match configured session and symbol")
            cfg.commission_perc = ledger.commission_perc
            ledger.hot_terminal_order_limit = cfg.demo_hot_terminal_order_limit
            ledger.archive_max_bytes = cfg.demo_archive_max_bytes
            if cfg.demo_archive_path:
                ledger.archive_path = Path(cfg.demo_archive_path)
                ledger._archived_order_ids = ledger._load_archived_order_ids()
        else:
            ledger = PaperLedger(
                session_id=str(cfg.session),
                symbol=cfg.pair,
                initial_inventory=cfg.demo_inv_initial,
                initial_cash=cfg.cash_initial,
                commission_perc=cfg.commission_perc,
                valuation_price=0.0,
                level1_fast_fill_enabled=cfg.demo_level1_fast_fill_enabled,
                level1_fill_delay_seconds=cfg.demo_level1_fill_delay_seconds,
                level1_fill_fraction=cfg.demo_level1_fill_fraction,
                queue_enabled=cfg.demo_queue_enabled,
                max_fill_per_observation=(
                    float(cfg.order_size)
                    * float(cfg.demo_max_fill_per_observation_orders)
                ),
                adverse_fill_enabled=cfg.demo_adverse_fill_enabled,
                adverse_fill_probability=cfg.demo_adverse_fill_probability,
                non_adverse_fill_probability=cfg.demo_non_adverse_fill_probability,
                state_path=cfg.demo_state_path,
                fee_state_path=cfg.demo_fee_state_path,
                archive_path=cfg.demo_archive_path,
                hot_terminal_order_limit=cfg.demo_hot_terminal_order_limit,
                archive_max_bytes=cfg.demo_archive_max_bytes,
            )
        exchange = DemoExchangeClient(
            market_data=real_exchange,
            pair=cfg.pair,
            commission_perc=cfg.commission_perc,
            ledger=ledger,
        )
    else:
        exchange = real_exchange
    strategy = build_strategy(cfg)

    bot = SimpleMarketMaker(
        cfg,
        nobi_portfo,
        exchange,
        strategy=strategy,
        # The isolated forward-test stack persists its rounds to its own CSV
        # result directory.  It must not require or connect to the legacy
        # production MySQL database.
        persist_to_db=not (is_demo or is_forward),
    )
    if is_demo:
        bot.config_sync = RuntimeConfigSynchronizer(
            cfg=cfg,
            ledger=ledger,
            pair=cfg.profile_name,
            state_dir=cfg.demo_config_state_dir,
            profiles=TRADING_PROFILES,
            lane=getattr(cfg, "lane", None),
        )
    return bot


LANE_POLL_SECONDS = 10.0
LANE_RETRY_SECONDS = 60.0
LANE_STOP_TIMEOUT_SECONDS = 90.0


def _lane_event(event: str, profile: str, lane: str, **extra) -> None:
    print(json.dumps({"event": event, "profile": profile, "lane": lane,
                      "timestamp": datetime.now().astimezone().isoformat(), **extra},
                     sort_keys=True, default=str), flush=True)


class LaneRunner:
    """The demo worker of one pair as lanes: one SimpleMarketMaker per
    enabled algorithm (the overlay's algo1_enabled / algo3_enabled / algo4_enabled), each in
    its own thread with its own paper ledger and result files
    (apply_lane_paths), all under the pair's one worker lease. The runner
    re-reads the overlay every ``poll_seconds`` and starts or stops lanes to
    match it; a lane that dies is restarted after LANE_RETRY_SECONDS. A
    shutdown signal (CoordinatedShutdown in this, the main, thread) stops
    every lane, which cancels its open paper orders as a stopped worker does."""

    def __init__(self, profile: str, exchange_name: str = "nobitex", continuous: bool = True, *,
                 poll_seconds: float = LANE_POLL_SECONDS, sleep=time.sleep, clock=time.monotonic):
        self.profile = profile
        self.exchange_name = exchange_name
        self.continuous = continuous
        self.poll_seconds = float(poll_seconds)
        self.sleep = sleep
        self.clock = clock
        self.lanes: Dict[str, dict] = {}
        self.failed_at: Dict[str, float] = {}

    def wanted(self):
        desired = load_desired_config(self.profile, DEMO_STATE_DIR, TRADING_PROFILES)
        return [key for key in SELECTABLE_ALGORITHMS if desired.get(f"{key}_enabled", False)]

    def build(self, lane: str) -> SimpleMarketMaker:
        cfg = make_cfg(self.profile, "demo", continuous=self.continuous, lane=lane)
        return make_bot(cfg, lane, self.exchange_name, "demo")

    def _run_lane(self, lane: str, bot: SimpleMarketMaker) -> None:
        try:
            bot.exchange.ledger.persist_initial_state()
            initial_reload = bot.config_sync.reload_at_round_boundary()
            if not initial_reload.ok:
                bot._record_config_reload_error(initial_reload.error)
            bot.run()
            _lane_event("demo_lane_stopped", self.profile, lane)
        except BaseException as exc:  # noqa: BLE001 -- one lane's failure must not take the worker down
            self.failed_at[lane] = float(self.clock())
            _lane_event("demo_lane_failed", self.profile, lane,
                        error_type=type(exc).__name__, error=str(exc))

    def start_lane(self, lane: str) -> None:
        bot = self.build(lane)
        stop = threading.Event()
        bot.stop_event = stop
        thread = threading.Thread(target=self._run_lane, args=(lane, bot), name=f"lane-{self.profile}-{lane}", daemon=True)
        self.lanes[lane] = {"bot": bot, "thread": thread, "stop": stop}
        _lane_event("demo_lane_starting", self.profile, lane, output_csv=bot.cfg.output_csv)
        thread.start()

    def stop_lane(self, lane: str, timeout: float = LANE_STOP_TIMEOUT_SECONDS) -> None:
        entry = self.lanes.pop(lane, None)
        if entry is None:
            return
        entry["stop"].set()
        entry["thread"].join(timeout)
        _lane_event("demo_lane_stop_requested", self.profile, lane, alive=entry["thread"].is_alive())

    def reconcile(self) -> None:
        try:
            wanted = self.wanted()
        except (OverlayLoadError, OverlayValidationError, KeyError, OSError) as exc:
            _lane_event("demo_lanes_config_unreadable", self.profile, "*", error=str(exc))
            return
        for lane in list(self.lanes):
            if lane not in wanted or not self.lanes[lane]["thread"].is_alive():
                self.stop_lane(lane)
        for lane in wanted:
            if lane in self.lanes:
                continue
            failed = self.failed_at.get(lane)
            if failed is not None and float(self.clock()) - failed < LANE_RETRY_SECONDS:
                continue
            self.start_lane(lane)

    def run(self) -> None:
        try:
            while True:
                self.reconcile()
                self.sleep(self.poll_seconds)
        except (CoordinatedShutdown, KeyboardInterrupt) as exc:
            _lane_event("demo_lanes_shutdown", self.profile, "*", signal=getattr(exc, "signum", None))
        finally:
            for lane in list(self.lanes):
                self.stop_lane(lane)


def main():
    install_shutdown_signal_handlers()
    parser = argparse.ArgumentParser(description="Run the market-making bot.")
    parser.add_argument("--profile", choices=sorted(TRADING_PROFILES), required=True,
                         help="Currency-pair configuration to run.")
    parser.add_argument("--algorithm", choices=sorted(STRATEGIES), default=None,
                         help="Quoting strategy to use. Live: default = the saved live configuration's "
                              "`algorithm` (algo1 when none). Demo: without this flag the worker runs every "
                              "algorithm enabled in the Demo configuration (algo1_enabled, algo3_enabled, algo4_enabled), "
                              "each in its own lane with its own files; with it, one algorithm in the "
                              "single-worker files (<pair>_track_demo.csv).")
    parser.add_argument("--exchange", choices=sorted(EXCHANGES), default="nobitex",
                         help="Exchange to trade on (default: nobitex).")
    parser.add_argument("--mode", choices=["real", "demo"], required=True,
                         help="real: places real orders; the isolated forward runtime uses CSV without MySQL. "
                              "demo: real market data, simulated fills, no real orders, "
                              "no MySQL, writes to a separate _demo.csv.")
    parser.add_argument(
        "--continuous", action="store_true",
        help="Run until intentionally stopped; ignore profile round/time horizon limits.",
    )
    args = parser.parse_args()

    if args.mode == "demo" and args.algorithm is None:
        # Lanes: every enabled algorithm at once, under the pair's one lease.
        pair = str(TRADING_PROFILES[args.profile]["pair"])
        lease_path = os.path.join(DEMO_STATE_DIR, f".worker_{pair.lower()}.lock")
        with WorkerLease(lease_path, identity=f"{pair}-demo"):
            LaneRunner(args.profile, args.exchange, args.continuous).run()
        return

    cfg = make_cfg(args.profile, args.mode, continuous=args.continuous)
    bot = make_bot(cfg, args.algorithm, args.exchange, args.mode)
    if args.mode == "demo":
        lease_path = os.path.join(DEMO_STATE_DIR, f".worker_{cfg.pair.lower()}.lock")
        with WorkerLease(lease_path, identity=f"{cfg.pair}-demo"):
            # Freeze the new-session seed only after this process owns the
            # worker lease. A concurrent loser must not write shared state.
            bot.exchange.ledger.persist_initial_state()
            initial_reload = bot.config_sync.reload_at_round_boundary()
            if not initial_reload.ok:
                bot._record_config_reload_error(initial_reload.error)
            bot.run()
    else:
        lock_dir = os.environ.get("LOB_FORWARD_LOCK_DIR")
        if lock_dir:
            lease_path = os.path.join(lock_dir, f".worker_live_{cfg.pair.lower()}.lock")
            with WorkerLease(lease_path, identity=f"{cfg.pair}-live"):
                bot.run()
        else:
            bot.run()


if __name__ == "__main__":
    main()
