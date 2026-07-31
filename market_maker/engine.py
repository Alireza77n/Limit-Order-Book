import math
import time
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List, Set  # add Set
import pandas as pd

from config import MMConfig
from portfolio import Nobitex_portfolio
from strategy.base import Strategy
from strategy.algo1 import Algo1Strategy
from exchange.base import ExchangeClient
import pytz
from datetime import datetime, timedelta
import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

# DB connection: credentials come from the environment (.env) -- see
# .env.example. Host/database keep their previous defaults since they
# aren't sensitive, only user/password are secret and have no fallback.
MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_USER = os.environ.get("MYSQL_USER")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "trading_bot")




class SimpleMarketMaker:
    def __init__(self, cfg: MMConfig, nobi_portfo: Nobitex_portfolio, exchange: ExchangeClient,
                 strategy: Optional[Strategy] = None):
        self.cfg = cfg
        self.recent_order_ids: List[str] = []
        self.tracked_orders: Dict[str, Set[str]] = {"buy": set(), "sell": set()}
        self.csv_path = cfg.output_csv
        self.nobi_portfo = nobi_portfo
        self.algorithm_id = cfg.algorithm_id 
        self.pair=cfg.pair
        self.inv_initial = cfg.inv_initial
        self.cash_initial = cfg.cash_initial
        self.session=cfg.session
        
         #Connect to MySQL (use your existing DB)
        self.conn = mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE
        )
        self.cursor = self.conn.cursor()
        self.exchange = exchange
        self.strategy = strategy if strategy is not None else Algo1Strategy(self.cfg)
        if self.cfg.exp_ret_min is None:
            self.cfg.exp_ret_min = self.cfg.commission_perc
        if self.cfg.order_size is None:
            self.cfg.order_size = self.cfg.inv_max / 100.0
        self.horizon_dt = self._compute_horizon_midnight()

    # ---------- Time horizon ----------
    def _compute_horizon_midnight(self) -> datetime:
        now = datetime.now()
        target = now + timedelta(days=self.cfg.time_horizon_days)
        return target.replace(hour=0, minute=0, second=0, microsecond=0)

    def _within_horizon(self) -> bool:
        return (self.horizon_dt - datetime.now()).total_seconds() > 0

    def seconds_to_horizon(self) -> int:
        return max(0, int((self.horizon_dt - datetime.now()).total_seconds()))
        

    # In SimpleMarketMaker

    def get_status_orders(self,status) -> List[Dict]:
        orders = self.exchange.get_order_list_status(
            status=status, details=2, page_size=100, max_pages=1
        )
    
        want_pair = str(self.cfg.pair).upper()
    
        filtered = []
        for o in orders:
            clid = str(o.get("clientOrderId") or "")
            pair = str(o.get("pair") or "").upper()
    
            # ✅ Only include orders that we sent recently
            if clid in self.recent_order_ids and pair == want_pair:
                filtered.append(o)
    
        return filtered

    


   

    def get_open_order_totals(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        Returns two elements:
          buy_totals  = {"amount": total_base_locked_on_buy,  "cash": total_quote_locked_on_buy}
          sell_totals = {"amount": total_base_locked_on_sell, "cash": total_quote_value_on_sell}
    
        Only counts orders:
          - with status 'open'
          - whose clientOrderId is one we sent (recent_order_ids or algo prefix)
          - matching self.cfg.pair
        """
        orders = self.get_status_orders('open')
        want_pair = str(self.cfg.pair).upper()
    
        buy_amount_locked  = 0.0
        buy_cash_locked    = 0.0
        sell_amount_locked = 0.0
        sell_cash_value    = 0.0
    
        # Helper to parse floats safely
        def _f(x: Any) -> float:
            try:
                return float(x)
            except Exception:
                return 0.0
    
        for o in orders:
            clid = str(o.get("clientOrderId") or "")
            pair = str(o.get("pair") or "").upper()
            side = str(o.get("type") or "").lower()
            if not clid or pair != want_pair:
                continue
    
            # Count only our orders: either we tracked them recently or they carry our algo prefix
            is_ours = (clid in getattr(self, "recent_order_ids", []))
            if not is_ours:
                continue
    
            amt   = _f(o.get("unmatchedAmount") or o.get("amount"))  # prefer remaining/unmatched
            price = _f(o.get("price"))
    
            if side == "buy":
                buy_amount_locked += amt
                buy_cash_locked   += amt * price
            elif side == "sell":
                sell_amount_locked += amt
                sell_cash_value    += amt * price
    
        buy_totals  = {"amount": buy_amount_locked,  "cash": buy_cash_locked}
        sell_totals = {"amount": sell_amount_locked, "cash": sell_cash_value}
        return buy_totals, sell_totals




                


    # ---------- Market data ----------
    # NOTE: exchange-specific I/O now lives in exchange/nobitex.py (NobitexExchangeClient).
    def _get_orderbook(self, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.exchange.get_orderbook(symbol or self.cfg.pair)

    # ---------- Microstructure helpers ----------
    # NOTE: the actual quoting logic lives in Algo1Strategy (strategy/algo1.py).
    # These wrappers exist so existing internal call sites (_tick_size(), _depth_cap(ob),
    # etc.) and the baseline characterization tests keep working unchanged.
    def _depth_cap(self, ob: Dict[str, Any]) -> int:
        return self.strategy.depth_cap(ob)

    def _tick_size(self) -> float:
        return self.strategy.tick_size()

    def _spread_ok(self, ob: Dict[str, Any], bid_idx: int, ask_idx: int) -> bool:
        return self.strategy.spread_ok(ob, bid_idx, ask_idx)

    def _leg_pairs(self, inv_diff: int, depth_max: int) -> List[Tuple[int, int]]:
        return self.strategy.leg_pairs(inv_diff, depth_max)

    # ---------- Quote selection (Algo1) ----------
    def _algo1_bid_ask_prices(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        return self.strategy.algo1_bid_ask_prices(ob, inv_current)

    def _decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        return self.strategy.decide_order(ob, inv_current)

    # ---------- Wallet / order endpoints ----------
    # NOTE: exchange-specific I/O now lives in exchange/nobitex.py (NobitexExchangeClient).
    def wallet_balance(self, currency: str) -> Optional[float]:
        return self.exchange.wallet_balance(currency)



    def save_to_db(self, order_df):
    
        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE
        )
        cursor = conn.cursor()
    
        # 1. Rename DataFrame columns to match DB schema
        order_df = order_df.where(pd.notnull(order_df), None)

        order_df.rename(columns={
            "Session_id": "session_id",
            "Algorithm": "algorithm_id",
            "Crypto": "crypto",
            "Timestamp": "ts",
            "Bid Level": "bid_level",
            "Ask Level": "ask_level",
            "inv_current": "inv_current",
            "cash_current": "cash_current",
            "Portfolio Wealth": "portfolio_wealth",
            "Best Bid": "best_bid",
            "Best Ask": "best_ask",
            "Last Price": "last_price",
            "Bid Price Sent": "bid_price_sent",
            "Ask Price Sent": "ask_price_sent",
            "Buy Size": "buy_size",
            "Sell Size": "sell_size",
            "Buy Sent": "buy_sent",
            "Sell Sent": "sell_sent",
            "Buy Status": "buy_status",
            "Sell Status": "sell_status"
        }, inplace=True)
    
        # 2. Replace NaN with None so MySQL stores NULL
        order_df = order_df.astype(object).where(pd.notnull(order_df), None)

    
        # 3. Build insert query
        insert_query = """
        INSERT INTO logs (
            session_id, algorithm_id, crypto, ts,
            bid_level, ask_level, inv_current, cash_current, portfolio_wealth,
            best_bid, best_ask, last_price, bid_price_sent, ask_price_sent,
            buy_size, sell_size, buy_sent, sell_sent, buy_status, sell_status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
    
        # 4. Prepare data
        data = []
        #print('aaaaaaaaaaa:',order_df.columns)
        for _, row in order_df.iterrows():
            
           
            
            data.append((
                row["session_id"], row["algorithm_id"], row["crypto"], row["ts"],
                row["bid_level"], row["ask_level"], row["inv_current"], row["cash_current"], row["portfolio_wealth"],
                row["best_bid"], row["best_ask"], row["last_price"], row["bid_price_sent"], row["ask_price_sent"],
                row["buy_size"], row["sell_size"], row["buy_sent"], row["sell_sent"],
                row["buy_status"], row["sell_status"]
            ))
    
        # 5. Insert into DB
        
        cursor.executemany(insert_query, data)
        conn.commit()
    
        print("✅ Data saved to logs table")
        
    
        cursor.close()
        conn.close()






    def update_filled_amounts(
        self,
        order_df: pd.DataFrame,
        ids_to_check: set,
        filled_history: Optional[Dict[str, List[float]]] = None
    ) -> Dict[str, List[float]]:
        """
        Update Buy/Sell cells by side using tracked IDs.
        - Append matched amounts to filled_history for all found orders.
        - Update the corresponding DataFrame cell (Buy or Sell) with latest matchedAmount.
        - Remove the ID from the matching tracked set ONLY if fully filled.
        """
    
        if filled_history is None:
            filled_history = {}
    
        # Fetch closed orders (Nobitex uses 'close'/'done' for fully filled;
        # if your API also returns partials here, they'll be handled accordingly)
        try:
            resp = self.get_status_orders('close')
        except Exception as e:
            print(f"Status fetch error for close: {e}")
            resp = []
    
        # Map clid -> info to avoid re-parsing
        info_map: Dict[str, Dict[str, Any]] = {}
        for o in resp or []:
            clid = str(o.get("clientOrderId") or "")
            if clid in ids_to_check:
                matched = float(o.get("matchedAmount") or 0.0)
                remaining = float(o.get("unmatchedAmount") or 0.0)
                status = str(o.get("status") or "").lower()  # e.g. "done"
                side = str(o.get("type") or o.get("_raw", {}).get("type") or "").lower()  # "buy"/"sell"
                info_map[clid] = {"matched": matched, "remaining": remaining, "status": status, "side": side}
    
                # Always record matched history
                filled_history.setdefault(clid, []).append(matched)
    
        # First update cells (by side), then decide removals
        to_remove_buy: Set[str] = set()
        to_remove_sell: Set[str] = set()
    
        # Update tracked BUY side only in Buy Status column
        for clid in list(self.tracked_orders["buy"]):
            if clid in info_map:
                amt = info_map[clid]["matched"]
                order_df.loc[order_df["Buy Status"] == clid, "Buy Status"] = amt
                # remove only if fully filled
                if info_map[clid]["status"] == "done":
                    to_remove_buy.add(clid)
    
        # Update tracked SELL side only in Sell Status column
        for clid in list(self.tracked_orders["sell"]):
            if clid in info_map:
                amt = info_map[clid]["matched"]
                order_df.loc[order_df["Sell Status"] == clid, "Sell Status"] = amt
                # remove only if fully filled
                if info_map[clid]["status"] == "done" :
                    to_remove_sell.add(clid)
    
        # Now shrink tracked sets
        self.tracked_orders["buy"] -= to_remove_buy
        self.tracked_orders["sell"] -= to_remove_sell
    
        return filled_history




    def update_inventory_and_cash(self,statuse, inv_coin: float, inv_cash: float) -> Tuple[float, float]:
        """
        Adjust balances for *executed* orders (status=close/Done).
        Deduct trading fees accordingly.
        Only process each order once (remove clientOrderId after update).
        """
        orders = self.get_status_orders(statuse)  # fetch only our own tracked closed orders
        print("Closed orders considered for update:", orders)
    
        inv_delta = inv_coin
        cash_delta = inv_cash
    
        # (optional) get last price just in case you need it
        ob = self._get_orderbook()
        last_price = float(ob.get("lastTradePrice")) if ob else 0.0
    
        for order in orders:
            order_type = order.get("type")
            clid = str(order.get("clientOrderId") or "")
            matched = float(order.get("matchedAmount", 0.0))  # executed volume
            price = float(order.get("price", 0.0))
            fee = float(order.get("_raw", {}).get("fee", 0.0))  # may be str
    
            if order_type == "buy":
                # Fee is deducted from the base asset
                inv_delta += (matched - fee)
                cash_delta -= matched * price
            elif order_type == "sell":
                # Fee is deducted from the quote asset
                inv_delta -= matched
                cash_delta += matched * price - fee
    
            # ✅ Remove this order ID so it won't be processed again
            if clid in self.recent_order_ids:
                self.recent_order_ids.remove(clid)
    
        return inv_delta, cash_delta




    def send_order(self, order_type: str, srcCurrency: str, dstCurrency: str,
               amount: float, price: float, clientOrderId: Optional[str] = None) -> Optional[str]:
        # The actual HTTP call now lives in exchange/nobitex.py (NobitexExchangeClient);
        # order-tracking bookkeeping stays here since it's SimpleMarketMaker's own state.
        result_id = self.exchange.send_order(
            order_type, srcCurrency, dstCurrency, amount, price, clientOrderId
        )
        if result_id:
            # Track for both PnL close-processing and UI status updates
            self.recent_order_ids.append(result_id)
            if order_type == "buy":
                self.tracked_orders["buy"].add(result_id)
            else:
                self.tracked_orders["sell"].add(result_id)
        return result_id





    # ---------- Main loop (uses wallet_balance + send_order) ----------

    def run(self) -> pd.DataFrame:
        """
        Main market-making loop.
        - Logs inventory/cash/wealth and quotes.
        - Places/cancels orders.
        - Tracks per-side order status in 'Buy Status' / 'Sell Status':
            * initially stores clientOrderId
            * later updated to amount (for full fills) or 'cancelled'
        - Persists updates by rewriting the CSV each round.
        """
        
        round_no = 0
    
        # Ensure tracking containers exist
        if not hasattr(self, "tracked_orders"):
            self.tracked_orders = {"buy": set(), "sell": set()}
        if not hasattr(self, "recent_order_ids"):
            self.recent_order_ids = []

        if not hasattr(self, "session"):
            self.session = int(datetime.now().timestamp())    
    
        # Initial inventory & cash vectors
        inv_coins, inv_cash = [self.inv_initial], [self.cash_initial]


        

        # Insert new session
        self.cursor.execute("""
            INSERT INTO sessions (session_id, algorithm_id, crypto)
            VALUES (%s, %s, %s)
        """, (self.session, self.algorithm_id, self.pair))
        self.conn.commit()
    
        # Columns (now include order size columns)
        columns = [
            "Session_id",
            "Algorithm",
            "Crypto",
            "Timestamp",
            "Bid Level",
            "Ask Level",
            "inv_current",
            "cash_current",
            "Portfolio Wealth",
            "Best Bid",
            "Best Ask",
            "Last Price",
            "Bid Price Sent",
            "Ask Price Sent",
            "Buy Size",
            "Sell Size",
            "Buy Sent",
            "Sell Sent",
            "Buy Status",
            "Sell Status",
        ]

    
        # CSV path
        csv_path = getattr(self, "csv_path", getattr(self.cfg, "output_csv", "/root/Nouri/Algo1/btc_track.csv"))
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    
        # Load or initialize DataFrame and ensure all columns exist
        if os.path.exists(csv_path):
            try:
                order_df = pd.read_csv(csv_path)
                for col in columns:
                    if col not in order_df.columns:
                        order_df[col] = None
                order_df = order_df[columns]
            except Exception:
                order_df = pd.DataFrame(columns=columns)
        else:
            order_df = pd.DataFrame(columns=columns)
            order_df.to_csv(csv_path, index=False)
    
    
        quote_sym = "IRT" if self.cfg.currency_2.lower() in ("rls", "irt", "irr") else self.cfg.currency_2.upper()
    
        try:
            while self._within_horizon():
                round_no += 1
                if self.cfg.max_rounds and round_no > self.cfg.max_rounds:
                    
                    #self.save_to_db(order_df)
                    break

                    
                
                # ---- (A) Update statuses of previously tracked order IDs ----
                ids_to_check = set(self.tracked_orders["buy"]) | set(self.tracked_orders["sell"])
                if ids_to_check:
                    self.update_filled_amounts(order_df, ids_to_check)
    
                # ---- (B) Fetch market data ----
                ob = self._get_orderbook()
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n--- Round {round_no} ({stamp}) ---")
    
                if not ob:
                    print("Orderbook fetch failed.")
                    order_df.to_csv(csv_path, index=False)
                    time.sleep(self.cfg.time_step)
                    continue
    
                try:
                    best_bid = float(ob["bids"][0][0])
                    best_ask = float(ob["asks"][0][0])
                    last_price = float(ob.get("lastTradePrice")) if ob.get("lastTradePrice") is not None else 0.0
                except Exception:
                    print("Orderbook missing best bid/ask/last.")
                    order_df.to_csv(csv_path, index=False)
                    time.sleep(self.cfg.time_step)
                    continue
    
                # ---- (C) Update inventory/cash based on newly closed orders (PnL) ----
                inv_delta, cash_delta = self.update_inventory_and_cash(
                    statuse='close',
                    inv_coin=inv_coins[-1],
                    inv_cash=inv_cash[-1]
                )
                inv_coins.append(inv_delta)
                inv_cash.append(cash_delta)
                inv_current, cash_current = inv_delta, cash_delta
                portfolio_wealth = inv_current * last_price + cash_current
    
                print(f"[{stamp}] Inventory={inv_current:.6f}, Cash={cash_current:.2f}, Wealth={portfolio_wealth:.2f}")
    
                # ---- (C2) Cancel stale orders & reflect cancellations in DF/tracking ----
                self.exchange.cancel_old_orders(
                    max_age_minutes=self.cfg.cancel_after_minutes,
                    df=order_df,
                    tracked_orders=self.tracked_orders
                )
    
                # ---- (D) Decide & place orders ----
                bid_lvl = ask_lvl = None
                bid_price = ask_price = None
                bid_price_sent = ask_price_sent = None
                buy_sent = sell_sent = 0
                buy_id = sell_id = None
                buy_size_value = 0.0  # NEW: will store actual submitted buy size
                sell_size_value = 0.0 # NEW: will store actual submitted sell size
    
                res = self._decide_order(ob, float(inv_current))
                if res is not None:
                    bid_price = float(res["bid"])
                    ask_price = float(res["ask"])
                    bid_lvl = res["bid level"]
                    ask_lvl = res["ask level"]
    
                    eps = 1e-10
    
                    # Current locked totals from our open orders
                    buy_locked, sell_locked = self.get_open_order_totals()
    
                    # SELL: cap by available (inventory minus already-locked sells)
                    sell_size = max(0.0, min(self.cfg.order_size, (inv_current - sell_locked['amount']) - float(self.cfg.inv_min)))
                    if sell_size > eps:
                        sell_id = self.send_order(
                            order_type="sell",
                            srcCurrency=self.cfg.currency_1,
                            dstCurrency=self.cfg.currency_2,
                            amount=float(sell_size),
                            price=float(ask_price),
                            clientOrderId=f"sell-{round_no}-{int(time.time()*1000)}"
                        )
                        if sell_id:
                            sell_sent = 1
                            sell_size_value = float(sell_size)     # NEW: record size actually sent
                            ask_price_sent = ask_price
                            self.tracked_orders["sell"].add(sell_id)
    
                    # BUY: cap by available quote after buy-locked cash, and by inv_max
                    if bid_price and bid_price > 0:
                        buy_affordable_base = (cash_current - buy_locked['cash']) / bid_price
                    else:
                        buy_affordable_base = 0.0
    
                    buy_size = max(0.0, min(self.cfg.order_size, buy_affordable_base))
                    if (buy_size > eps) and (buy_locked['amount'] + inv_current + self.cfg.order_size < self.cfg.inv_max):
                        buy_id = self.send_order(
                            order_type="buy",
                            srcCurrency=self.cfg.currency_1,
                            dstCurrency=self.cfg.currency_2,
                            amount=float(buy_size),
                            price=float(bid_price),
                            clientOrderId=f"buy-{round_no}-{int(time.time()*1000)}"
                        )
                        if buy_id:
                            buy_sent = 1
                            buy_size_value = float(buy_size)       # NEW: record size actually sent
                            bid_price_sent = bid_price
                            self.tracked_orders["buy"].add(buy_id)
    
                # ---- (E) Build the row: status cells start with ID or 'not_sent' ----
                buy_status_cell = buy_id if (buy_sent and buy_id) else "not_sent"
                sell_status_cell = sell_id if (sell_sent and sell_id) else "not_sent"
    
                new_row = [
                    self.session,
                    self.algorithm_id,
                    self.pair,
                    stamp, bid_lvl, ask_lvl,
                    inv_current, cash_current,
                    portfolio_wealth,
                    best_bid, best_ask, last_price,
                    bid_price_sent, ask_price_sent,
                    buy_size_value, sell_size_value,
                    buy_sent, sell_sent,
                    buy_status_cell, sell_status_cell,
                ]

    
                order_df.loc[len(order_df)] = new_row
    
                # ---- (F) Persist: rewrite full CSV so prior status edits are saved ----
                order_df.to_csv(csv_path, index=False)
    
                print(order_df.tail())
                time.sleep(self.cfg.time_step)
    
        except KeyboardInterrupt:
            print("Interrupted by user.")
            # Replace NaN with None so MySQL gets NULL instead of "nan"
            order_df = order_df.where(pd.notnull(order_df), None)


            self.save_to_db(order_df)
        finally:
            print("Cancelling all open orders before exit...")
            try:
                self.exchange.cancel_all_open_orders(max_pages=10)
                

                # Optional: final reconciliation
                # (guard if ids_to_check may be undefined)
                try:
                    ids_to_check = set(self.tracked_orders["buy"]) | set(self.tracked_orders["sell"])
                except Exception:
                    ids_to_check = set()
                if ids_to_check:
                    self.update_filled_amounts(order_df, ids_to_check)
            except Exception as e:
                print(f"Bulk cancel failed: {e}")

        order_df=pd.read_csv(csv_path)
        # Replace NaN with None so MySQL gets NULL instead of "nan"
        order_df = order_df.where(pd.notnull(order_df), None)
        #print("Current DataFrame columns:", order_df.columns.tolist())


        self.save_to_db(order_df)
        return order_df




