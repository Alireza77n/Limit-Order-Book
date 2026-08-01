"""Single entry point for running the market-making bot.

Replaces the old demo_btc.py / demo_usdt.py (near-duplicate scripts that
only differed in their MMConfig values) with one script parameterized by
--profile, --algorithm, --exchange, and --mode. Adding a new currency pair
means adding a TRADING_PROFILES entry in config.py; adding a new algorithm
or exchange means adding one STRATEGIES/EXCHANGES entry below -- no new
files either way.

    python run.py --profile btc                # real trading, equivalent to the old `python demo_btc.py`
    python run.py --profile usdt               # real trading, equivalent to the old `python demo_usdt.py`
    python run.py --profile btc --mode demo    # paper trading: real market data, simulated fills, no real orders

See usage.md for the full real-vs-demo breakdown.

NOTE: --exchange only selects the ExchangeClient used for quotes/orders.
Nobitex_portfolio (portfolio.py) is still Nobitex-specific and isn't
behind an exchange interface, so portfolio valuation doesn't yet follow
--exchange. Flagged in README.md, not silently glossed over.
"""

import argparse
import os
import signal
from datetime import datetime
from pathlib import Path

from config import MMConfig, RESULTS_DIR, TRADING_PROFILES, demo_csv_path
from portfolio import Nobitex_portfolio
from engine import CoordinatedShutdown, SimpleMarketMaker
from exchange.nobitex import NobitexExchangeClient
from exchange.demo import DemoExchangeClient
from strategy.algo1 import Algo1Strategy
from strategy.algo2 import Algo2Strategy
from session import WorkerLease, load_stack_session
from paper import PaperLedger

STRATEGIES = {
    "algo1": Algo1Strategy,
    "algo2": Algo2Strategy,  # not implemented yet -- see README.md
}

EXCHANGES = {
    "nobitex": NobitexExchangeClient,
}


def _shutdown_signal_handler(signum, frame):
    """Route container stop signals through the engine's cleanup path."""
    raise CoordinatedShutdown(signum)


def install_shutdown_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _shutdown_signal_handler)
    signal.signal(signal.SIGINT, _shutdown_signal_handler)


def make_cfg(
    profile: str, mode: str = "real", session_id=None, continuous: bool = False
) -> MMConfig:
    """Build the MMConfig for a profile (config.TRADING_PROFILES), applying
    demo-mode's adjustments (configurable starting inventory, separate CSV,
    algorithm_id suffix) when mode == "demo". Pulled out of main() so tests
    exercise the exact same setup the real CLI does, instead of a
    hand-duplicated (and easily drifting) copy of it."""
    session_file = os.environ.get("DEMO_SESSION_FILE") if mode == "demo" else None
    if session_id is None and session_file:
        session_id = load_stack_session(session_file)
    session_id = datetime.now().timestamp() if session_id is None else session_id
    cfg = MMConfig(session=session_id, mode=mode, **TRADING_PROFILES[profile])
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
        cfg.demo_state_path = os.path.join(
            RESULTS_DIR, f"paper_{safe_session}_{cfg.pair.lower()}.json"
        )
        cfg.demo_fee_state_path = os.path.join(RESULTS_DIR, "paper_fee_scopes.json")
        cfg.demo_archive_path = os.path.join(
            RESULTS_DIR, f"paper_{safe_session}_{cfg.pair.lower()}_archive.jsonl"
        )
    return cfg


def make_bot(cfg: MMConfig, algorithm: str = "algo1", exchange_name: str = "nobitex",
             mode: str = "real") -> SimpleMarketMaker:
    """Build the SimpleMarketMaker for an already-constructed cfg (see
    make_cfg). Also pulled out of main() so it can be reused verbatim."""
    is_demo = mode == "demo"

    adapter_token = None if is_demo else cfg.token
    nobi_portfo = Nobitex_portfolio(adapter_token)
    real_exchange = EXCHANGES[exchange_name](
        token=adapter_token,
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
            ledger.hot_terminal_order_limit = cfg.demo_hot_terminal_order_limit
            ledger.archive_max_bytes = cfg.demo_archive_max_bytes
            if cfg.demo_archive_path:
                ledger.archive_path = Path(cfg.demo_archive_path)
                ledger._archived_order_ids = ledger._load_archived_order_ids()
        else:
            ledger = PaperLedger(
                session_id=str(cfg.session),
                symbol=cfg.pair,
                initial_inventory=cfg.inv_initial,
                initial_cash=cfg.cash_initial,
                commission_perc=cfg.commission_perc,
                valuation_price=0.0,
                level1_fast_fill_enabled=cfg.demo_level1_fast_fill_enabled,
                level1_fill_delay_seconds=cfg.demo_level1_fill_delay_seconds,
                level1_fill_fraction=cfg.demo_level1_fill_fraction,
                queue_enabled=cfg.demo_queue_enabled,
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
    strategy = STRATEGIES[algorithm](cfg)

    return SimpleMarketMaker(cfg, nobi_portfo, exchange, strategy=strategy,
                              persist_to_db=not is_demo)


def main():
    install_shutdown_signal_handlers()
    parser = argparse.ArgumentParser(description="Run the market-making bot.")
    parser.add_argument("--profile", choices=sorted(TRADING_PROFILES), required=True,
                         help="Currency-pair configuration to run.")
    parser.add_argument("--algorithm", choices=sorted(STRATEGIES), default="algo1",
                         help="Quoting strategy to use (default: algo1).")
    parser.add_argument("--exchange", choices=sorted(EXCHANGES), default="nobitex",
                         help="Exchange to trade on (default: nobitex).")
    parser.add_argument("--mode", choices=["real", "demo"], default="real",
                         help="real: places real orders, needs live MySQL (default). "
                              "demo: real market data, simulated fills, no real orders, "
                              "no MySQL, writes to a separate _demo.csv.")
    parser.add_argument(
        "--continuous", action="store_true",
        help="Run until intentionally stopped; ignore profile round/time horizon limits.",
    )
    args = parser.parse_args()

    cfg = make_cfg(args.profile, args.mode, continuous=args.continuous)
    bot = make_bot(cfg, args.algorithm, args.exchange, args.mode)
    if args.mode == "demo":
        lease_path = os.path.join(RESULTS_DIR, f".worker_{cfg.pair.lower()}.lock")
        with WorkerLease(lease_path, identity=f"{cfg.pair}-demo"):
            bot.run()
    else:
        bot.run()


if __name__ == "__main__":
    main()
