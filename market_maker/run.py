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
from datetime import datetime

from config import MMConfig, TRADING_PROFILES, demo_csv_path
from portfolio import Nobitex_portfolio
from engine import SimpleMarketMaker
from exchange.nobitex import NobitexExchangeClient
from exchange.demo import DemoExchangeClient
from strategy.algo1 import Algo1Strategy
from strategy.algo2 import Algo2Strategy

STRATEGIES = {
    "algo1": Algo1Strategy,
    "algo2": Algo2Strategy,  # not implemented yet -- see README.md
}

EXCHANGES = {
    "nobitex": NobitexExchangeClient,
}


def make_cfg(profile: str, mode: str = "real") -> MMConfig:
    """Build the MMConfig for a profile (config.TRADING_PROFILES), applying
    demo-mode's adjustments (configurable starting inventory, separate CSV,
    algorithm_id suffix) when mode == "demo". Pulled out of main() so tests
    exercise the exact same setup the real CLI does, instead of a
    hand-duplicated (and easily drifting) copy of it."""
    cfg = MMConfig(session=datetime.now().timestamp(), **TRADING_PROFILES[profile])
    if mode == "demo":
        cfg.algorithm_id = f"{cfg.algorithm_id}-demo"
        cfg.output_csv = demo_csv_path(cfg.output_csv)
        # Demo starting inventory is a per-profile config value (see
        # config.TRADING_PROFILES' demo_inv_initial) -- paper trading
        # starts flat by default, no inherited real position.
        cfg.inv_initial = cfg.demo_inv_initial
    return cfg


def make_bot(cfg: MMConfig, algorithm: str = "algo1", exchange_name: str = "nobitex",
             mode: str = "real") -> SimpleMarketMaker:
    """Build the SimpleMarketMaker for an already-constructed cfg (see
    make_cfg). Also pulled out of main() so it can be reused verbatim."""
    is_demo = mode == "demo"

    nobi_portfo = Nobitex_portfolio(cfg.token)
    real_exchange = EXCHANGES[exchange_name](
        token=cfg.token,
        url_send_order=cfg.url_send_order,
        url_wallet_balance=cfg.url_wallet_balance,
        algorithm_id=cfg.algorithm_id,
    )
    exchange = DemoExchangeClient(market_data=real_exchange, pair=cfg.pair,
                                   commission_perc=cfg.commission_perc) if is_demo else real_exchange
    strategy = STRATEGIES[algorithm](cfg)

    return SimpleMarketMaker(cfg, nobi_portfo, exchange, strategy=strategy,
                              persist_to_db=not is_demo)


def main():
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
    args = parser.parse_args()

    cfg = make_cfg(args.profile, args.mode)
    bot = make_bot(cfg, args.algorithm, args.exchange, args.mode)
    bot.run()


if __name__ == "__main__":
    main()
