from Config import MMConfig as cfg

from Portfolio import Nobitex_portfolio
from Algo1 import SimpleMarketMaker
from exchange.nobitex import NobitexExchangeClient
from datetime import datetime, timedelta






cfg = cfg(
commission_perc=0.0025,
cancel_after_minutes=10,
algorithm_id = '1btc', 
depth_max=10,
inv_max=0.000472,   #0.000442,                      #0.0001933901,   # not used for one-off buy
inv_min=0.0,
pair="BTCIRT",          # market
currency_1="btc",       # base
currency_2="rls",       # quote (cash)
time_step=60,
time_horizon_days=1,
max_rounds=500,
inv_initial = 0.0001888,
order_size=0.000015,
cash_initial =30000000 ,
session=datetime.now().timestamp(),
url_wallet_balance="https://apiv2.nobitex.ir/users/wallets/balance",
url_send_order="https://apiv2.nobitex.ir/market/orders/add",
output_csv = r"/root/Nouri/Algo1/btc_track.csv"

)

nobi_portfo = Nobitex_portfolio(cfg.token)
exchange = NobitexExchangeClient(
    token=cfg.token,
    url_send_order=cfg.url_send_order,
    url_wallet_balance=cfg.url_wallet_balance,
    algorithm_id=cfg.algorithm_id,
)
bot = SimpleMarketMaker(cfg, nobi_portfo, exchange)
a = bot.run()
