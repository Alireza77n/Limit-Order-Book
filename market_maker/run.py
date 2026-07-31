"""Single entry point for running the market-making bot.

Replaces the old demo_btc.py / demo_usdt.py (near-duplicate scripts that
only differed in their MMConfig values) with one script parameterized by
--profile, --algorithm, and --exchange. Adding a new currency pair means
adding a PROFILES entry; adding a new algorithm or exchange means adding
one STRATEGIES/EXCHANGES entry -- no new files.

    python run.py --profile btc     # equivalent to the old `python demo_btc.py`
    python run.py --profile usdt    # equivalent to the old `python demo_usdt.py`

NOTE: --exchange only selects the ExchangeClient used for quotes/orders.
Nobitex_portfolio (portfolio.py) is still Nobitex-specific and isn't
behind an exchange interface, so portfolio valuation doesn't yet follow
--exchange. Flagged in docs/PROJECT_REPORT.md, not silently glossed over.
"""

import argparse
from datetime import datetime

from config import MMConfig
from portfolio import Nobitex_portfolio
from engine import SimpleMarketMaker
from exchange.nobitex import NobitexExchangeClient
from strategy.algo1 import Algo1Strategy
from strategy.algo2 import Algo2Strategy

STRATEGIES = {
    "algo1": Algo1Strategy,
    "algo2": Algo2Strategy,  # not implemented yet -- see docs/PROJECT_REPORT.md
}

EXCHANGES = {
    "nobitex": NobitexExchangeClient,
}

# Values copied verbatim from the old demo_btc.py / demo_usdt.py.
PROFILES = {
    "btc": dict(
        commission_perc=0.0025,
        cancel_after_minutes=10,
        algorithm_id="1btc",
        depth_max=10,
        inv_max=0.000472,
        inv_min=0.0,
        pair="BTCIRT",
        currency_1="btc",
        currency_2="rls",
        time_step=60,
        time_horizon_days=1,
        max_rounds=500,
        inv_initial=0.0001888,
        order_size=0.000015,
        cash_initial=30000000,
        url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
        url_send_order="https://apiv2.nobitex.ir/market/orders/add",
        output_csv=r"/root/Nouri/Algo1/btc_track.csv",
    ),
    "usdt": dict(
        commission_perc=0.0025,
        cancel_after_minutes=10,
        algorithm_id="1usdt",
        depth_max=10,
        inv_max=50,
        inv_min=0.0,
        pair="USDTIRT",
        currency_1="usdt",
        currency_2="rls",
        time_step=10,
        time_horizon_days=3,
        max_rounds=50000,
        inv_initial=27.16,
        order_size=1,
        cash_initial=30000000,
        url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
        url_send_order="https://apiv2.nobitex.ir/market/orders/add",
        output_csv=r"/root/Nouri/Algo1/usdt_track.csv",
    ),
}


def main():
    parser = argparse.ArgumentParser(description="Run the market-making bot.")
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True,
                         help="Currency-pair configuration to run.")
    parser.add_argument("--algorithm", choices=sorted(STRATEGIES), default="algo1",
                         help="Quoting strategy to use (default: algo1).")
    parser.add_argument("--exchange", choices=sorted(EXCHANGES), default="nobitex",
                         help="Exchange to trade on (default: nobitex).")
    args = parser.parse_args()

    cfg = MMConfig(session=datetime.now().timestamp(), **PROFILES[args.profile])

    nobi_portfo = Nobitex_portfolio(cfg.token)
    exchange = EXCHANGES[args.exchange](
        token=cfg.token,
        url_send_order=cfg.url_send_order,
        url_wallet_balance=cfg.url_wallet_balance,
        algorithm_id=cfg.algorithm_id,
    )
    strategy = STRATEGIES[args.algorithm](cfg)

    bot = SimpleMarketMaker(cfg, nobi_portfo, exchange, strategy=strategy)
    bot.run()


if __name__ == "__main__":
    main()
