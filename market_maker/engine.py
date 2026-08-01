import json
import math
import time
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List, Set  # add Set
import pandas as pd

from config import MMConfig, RESULTS_DIR
from portfolio import Nobitex_portfolio
from strategy.base import Strategy
from strategy.algo1 import Algo1Strategy
from exchange.base import ExchangeClient
from safety import enforce_engine_safety
from telegram_lifecycle import PaperTelegramReporter, TelegramClient
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

# Telegram round notifications (optional -- no-ops if unset)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Displayed/logged timestamps use Tehran local time explicitly, regardless
# of the host or container's own system timezone (Docker containers
# default to UTC) -- so CSV rows, console logs, and Telegram messages
# always agree and always read in the operator's local time.
TEHRAN_TZ = pytz.timezone("Asia/Tehran")




class SimpleMarketMaker:
    def __init__(self, cfg: MMConfig, nobi_portfo: Nobitex_portfolio, exchange: ExchangeClient,
                 strategy: Optional[Strategy] = None, persist_to_db: bool = True):
        enforce_engine_safety(cfg.mode, exchange, persist_to_db)
        self.cfg = cfg
        self.recent_order_ids: List[str] = []
        self.order_history_ids: Set[str] = set()
        self.processed_matched_amounts: Dict[str, float] = {}
        self.processed_order_fees: Dict[str, float] = {}
        self.tracked_orders: Dict[str, Set[str]] = {"buy": set(), "sell": set()}
        self.csv_path = cfg.output_csv
        self.nobi_portfo = nobi_portfo
        self.algorithm_id = cfg.algorithm_id
        self.pair=cfg.pair
        self.inv_initial = cfg.inv_initial
        self.cash_initial = cfg.cash_initial
        self.session=cfg.session
        self.persist_to_db = persist_to_db
        self.mode_label = cfg.mode
        self.now_provider = lambda: datetime.now(timezone.utc)
        self.sleep_fn = time.sleep
        self.telegram_client = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.telegram_reporter = PaperTelegramReporter(self.telegram_client)

        # Connect to MySQL (use your existing DB) -- skipped entirely in
        # demo/paper-trading mode so a demo run never touches the real
        # trading DB and doesn't require a live MySQL server at all.
        if self.persist_to_db:
            self.conn = mysql.connector.connect(
                host=MYSQL_HOST,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DATABASE
            )
            self.cursor = self.conn.cursor()
        else:
            self.conn = None
            self.cursor = None
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
        if getattr(self.cfg, "continuous", False):
            return True
        return (self.horizon_dt - datetime.now()).total_seconds() > 0

    def seconds_to_horizon(self) -> int:
        return max(0, int((self.horizon_dt - datetime.now()).total_seconds()))
        

    # In SimpleMarketMaker

    def get_status_orders(self,status) -> List[Dict]:
        orders = self.exchange.get_order_list_status(
            status=status, details=2, page_size=100, max_pages=100
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

    def _active_order_ids(self) -> Set[str]:
        return set(self.recent_order_ids) | self.tracked_orders["buy"] | self.tracked_orders["sell"]

    def _prune_cancelled_active_ids(self, cancelled_ids) -> Set[str]:
        cancelled = {str(value) for value in (cancelled_ids or set())}
        if not cancelled:
            return set()
        self.recent_order_ids = [
            client_id for client_id in self.recent_order_ids if client_id not in cancelled
        ]
        self.tracked_orders["buy"] -= cancelled
        self.tracked_orders["sell"] -= cancelled
        return cancelled

    def _cancel_owned_open_orders(self, max_pages: int = 10) -> Set[str]:
        owned = self._active_order_ids()
        cancelled = self.exchange.cancel_all_open_orders(
            max_pages=max_pages,
            pair=self.cfg.pair,
            owned_client_order_ids=owned,
        )
        return self._prune_cancelled_active_ids(cancelled)

    def _cancel_owned_stale_orders(
        self, max_age_minutes: float, df: Optional[pd.DataFrame] = None
    ) -> Set[str]:
        owned = self._active_order_ids()
        cancelled = self.exchange.cancel_old_orders(
            max_age_minutes=max_age_minutes,
            df=df,
            tracked_orders=self.tracked_orders,
            pair=self.cfg.pair,
            owned_client_order_ids=owned,
            max_pages=100,
        )
        return self._prune_cancelled_active_ids(cancelled)

    


   

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

    def _emit_algo1_diagnostic(
        self, ob: Dict[str, Any], inv_current: float, *, round_no: int
    ) -> None:
        if not getattr(self.cfg, "algo1_diagnostics_enabled", False):
            return
        diagnostic_method = getattr(self.strategy, "diagnostics", None)
        if diagnostic_method is None:
            return
        document = diagnostic_method(
            ob, inv_current, round_no=round_no, session_id=self.session
        )
        path = self.cfg.algo1_diagnostics_path or os.path.join(
            RESULTS_DIR, f"{self.pair.lower()}_algo1_diagnostics.jsonl"
        )
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a") as diagnostic_file:
            diagnostic_file.write(json.dumps(document, sort_keys=True) + "\n")

    # ---------- Wallet / order endpoints ----------
    # NOTE: exchange-specific I/O now lives in exchange/nobitex.py (NobitexExchangeClient).
    def wallet_balance(self, currency: str) -> Optional[float]:
        return self.exchange.wallet_balance(currency)

    # ---------- Telegram notifications ----------
    def _notify_telegram(self, text: str) -> bool:
        """Best-effort push to Telegram. No-ops if unconfigured; never
        raises, so a network hiccup can't take down the trading loop.
        Returns True only when Telegram returns HTTP success, JSON ok=true,
        and a message identifier."""
        return self.telegram_client.send(text) is not None

    def _mode_tag(self) -> str:
        """Prominent, unmissable badge for the very first line of every
        Telegram message -- so demo/paper messages can never be mistaken
        for real ones (or vice versa) at a glance while scrolling a chat."""
        if self.mode_label == "demo":
            return "\U0001F9EA <b>DEMO MODE</b> — simulated, no real orders"
        return "\U0001F534 <b>LIVE MODE</b> — real orders, real money"

    # ---------- Startup message (sent once per pair+mode, not per restart) ----------
    def _startup_key(self) -> str:
        """Marker identity: one startup announcement per pair and mode, so
        Docker's `restart: unless-stopped` (which restarts the container
        every time a run finishes) doesn't spam a new "Market Maker
        Connected" message per session."""
        return f"{self.pair}_{self.mode_label}"

    def _startup_notified(self) -> bool:
        """True if this pair+mode already announced its startup. The marker
        file lives in results/ (the shared Docker volume), so it survives
        container restarts; delete results/startup_notified_*.json to
        re-enable the startup message on the next launch."""
        path = os.path.join(RESULTS_DIR, f"startup_notified_{self._startup_key()}.json")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    json.load(f)
                return True
        except Exception as e:
            print(f"Startup marker read failed: {e}")
        return False

    def _mark_startup_notified(self) -> None:
        path = os.path.join(RESULTS_DIR, f"startup_notified_{self._startup_key()}.json")
        try:
            os.makedirs(RESULTS_DIR, exist_ok=True)
            with open(path, "w") as f:
                json.dump({"notified_at": datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                           "session": self.session}, f)
        except Exception as e:
            print(f"Startup marker write failed: {e}")

    def _notify_startup(self) -> None:
        if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
            return
        if self._startup_notified():
            print(f"Startup notification already sent for {self._startup_key()}; skipping.")
            return
        sent = self._notify_telegram(
            f"{self._mode_tag()}\n\n"
            f"<b>Market Maker Connected</b>\n\n"
            f"Symbol: <b>{self.pair}</b>\n"
            f"Algorithm: <b>{self.strategy.__class__.__name__}</b> (profile: {self.algorithm_id})\n"
            f"Commission: <b>{self.cfg.commission_perc:.2%}</b>\n"
            f"Session: <code>{self.session}</code>"
        )
        if sent:
            self._mark_startup_notified()

    def _format_round_message(
        self, round_no: int, stamp: str,
        best_bid: float, best_ask: float, last_price: float,
        bid_price: Optional[float], ask_price: Optional[float],
        bid_lvl: Optional[int], ask_lvl: Optional[int],
        inv_current: float, cash_current: float, portfolio_wealth: float,
        buy_sent: int, buy_size_value: float, bid_price_sent: Optional[float], buy_id: Optional[str],
        sell_sent: int, sell_size_value: float, ask_price_sent: Optional[float], sell_id: Optional[str],
        round_fee_irt: float, symbol_total_fee_irt: float, combined_total_fee_irt: float,
    ) -> str:
        quote_line = (
            f"Bid: {bid_price:,.2f} (lvl {bid_lvl})   Ask: {ask_price:,.2f} (lvl {ask_lvl})"
            if bid_price is not None and ask_price is not None else "No quote this round"
        )
        buy_line = (
            f"\U0001F7E2 Buy sent: {buy_size_value:.8f} @ {bid_price_sent:,.2f} (id {buy_id})"
            if buy_sent else "\U0001F7E2 Buy: not sent"
        )
        sell_line = (
            f"\U0001F534 Sell sent: {sell_size_value:.8f} @ {ask_price_sent:,.2f} (id {sell_id})"
            if sell_sent else "\U0001F534 Sell: not sent"
        )
        return (
            f"{self._mode_tag()}\n"
            f"<b>{self.pair}</b> · Round {round_no} · \U0001F550 {stamp}\n\n"
            f"<b>Quote</b>\n"
            f"{quote_line}\n\n"
            f"<b>Market</b>\n"
            f"Best Bid: {best_bid:,.2f}\n"
            f"Best Ask: {best_ask:,.2f}\n"
            f"Last: {last_price:,.2f}\n\n"
            f"<b>Portfolio</b>\n"
            f"Inventory: {inv_current:.8f}\n"
            f"Cash: {cash_current:,.2f}\n"
            f"Wealth: {portfolio_wealth:,.2f}\n\n"
            f"<b>Orders</b>\n"
            f"{buy_line}\n"
            f"{sell_line}\n\n"
            f"<b>Costs</b> (commission {self.cfg.commission_perc:.2%})\n"
            f"This round: {round_fee_irt:,.2f} IRT\n"
            f"{self.pair} total: {symbol_total_fee_irt:,.2f} IRT\n"
            f"All symbols: {combined_total_fee_irt:,.2f} IRT"
        )



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
        Apply only the new delta from each cumulative exchange update.
        Active partials stay tracked; terminal orders leave active tracking while
        their cumulative checkpoints and immutable history remain available.
        """
        statuses = ("open", "close") if statuse == "all" else (statuse,)
        by_client_id: Dict[str, Dict[str, Any]] = {}
        for status in statuses:
            for order in self.get_status_orders(status):
                client_id = str(order.get("clientOrderId") or "")
                if client_id:
                    by_client_id[client_id] = order
        orders = list(by_client_id.values())
        print("Closed orders considered for update:", orders)

        inv_delta = inv_coin
        cash_delta = inv_cash
        self.last_round_fee_irt = 0.0

        for order in orders:
            order_type = str(order.get("type") or "").lower()
            clid = str(order.get("clientOrderId") or "")
            if not clid or clid not in self._active_order_ids():
                continue
            matched = max(0.0, float(order.get("matchedAmount") or 0.0))
            price = float(order.get("price", 0.0))
            cumulative_fee = max(0.0, float(order.get("_raw", {}).get("fee") or 0.0))
            processed_matched = self.processed_matched_amounts.get(clid, 0.0)
            processed_fee = self.processed_order_fees.get(clid, 0.0)
            matched_delta = max(0.0, matched - processed_matched)
            fee_delta = max(0.0, cumulative_fee - processed_fee)

            if order_type == "buy":
                inv_delta += matched_delta - fee_delta
                cash_delta -= matched_delta * price
                self.last_round_fee_irt += fee_delta * price
            elif order_type == "sell":
                inv_delta -= matched_delta
                cash_delta += matched_delta * price - fee_delta
                self.last_round_fee_irt += fee_delta

            self.processed_matched_amounts[clid] = max(processed_matched, matched)
            self.processed_order_fees[clid] = max(processed_fee, cumulative_fee)
            status = str(order.get("status") or "").lower()
            remaining = float(order.get("unmatchedAmount") or 0.0)
            if status in {"done", "close", "closed", "filled"} and remaining <= 1e-12:
                self._prune_cancelled_active_ids({clid})

        return inv_delta, cash_delta

    def _accumulate_cost(self, round_fee_irt: float) -> Tuple[float, float]:
        """Best-effort, cross-process running total of transaction costs
        (IRT), shared via a small local JSON file keyed by pair -- so a BTC
        run and a USDT run (each its own process, per usage.md) can both
        report a combined total. No locking: a lost update on a rare
        simultaneous write is an acceptable tradeoff for a demo notification,
        not something the trading logic depends on.

        Returns (this pair's cumulative cost, combined cost across all pairs).
        """
        path = os.path.join(RESULTS_DIR, f"costs_{self.mode_label}.json")
        os.makedirs(RESULTS_DIR, exist_ok=True)
        try:
            costs = {}
            if os.path.exists(path):
                with open(path) as f:
                    costs = json.load(f)
            costs[self.pair] = costs.get(self.pair, 0.0) + round_fee_irt
            with open(path, "w") as f:
                json.dump(costs, f)
            return costs[self.pair], sum(costs.values())
        except Exception as e:
            print(f"Cost tracking file failed: {e}")
            return round_fee_irt, round_fee_irt




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
            self.order_history_ids.add(result_id)
            if order_type == "buy":
                self.tracked_orders["buy"].add(result_id)
            else:
                self.tracked_orders["sell"].add(result_id)
        return result_id





    # ---------- Main loop (uses wallet_balance + send_order) ----------

    DEMO_COLUMNS = [
        "Session_id", "Algorithm", "Crypto", "Mode", "Round", "Timestamp",
        "Quote Status", "No Quote Reason", "Bid Level", "Ask Level",
        "inv_current", "cash_current", "Locked Cash", "Locked Inventory",
        "Available Cash", "Available Inventory", "Portfolio Wealth",
        "Best Bid", "Best Ask", "Last Price", "Bid Price", "Ask Price",
        "Buy Order ID", "Buy Status", "Buy Not Sent Reason", "Buy Original Amount",
        "Buy Matched Amount", "Buy Processed Matched Amount", "Buy Remaining Amount",
        "Buy Fill Reason", "Buy Selected Level", "Buy Limit Price", "Buy Locked Cash",
        "Buy Telegram Message ID",
        "Sell Order ID", "Sell Status", "Sell Not Sent Reason", "Sell Original Amount",
        "Sell Matched Amount", "Sell Processed Matched Amount", "Sell Remaining Amount",
        "Sell Fill Reason", "Sell Selected Level", "Sell Limit Price", "Sell Locked Inventory",
        "Sell Telegram Message ID",
        "Round Fee", "Session Pair Fee", "Session All Symbols Fee",
        "Pair All Time Fee", "All Symbols All Time Fee",
    ]

    @staticmethod
    def _demo_side_fields(side: str, order, not_sent_reason: Optional[str]) -> Dict[str, Any]:
        title = side.title()
        locked = 0.0
        if order is not None and order.status in {"open", "partially_filled"}:
            locked = order.remaining_amount * order.limit_price if side == "buy" else order.remaining_amount
        if order is not None:
            lifecycle_status = order.status
        elif not_sent_reason == "no_quote":
            lifecycle_status = "no_quote"
        elif not_sent_reason == "rejected":
            lifecycle_status = "rejected"
        elif not_sent_reason:
            lifecycle_status = f"not_sent_{not_sent_reason}"
        else:
            lifecycle_status = "submission_failed"
        fields = {
            f"{title} Order ID": order.client_order_id if order else None,
            f"{title} Status": lifecycle_status,
            f"{title} Not Sent Reason": None if order else not_sent_reason,
            f"{title} Original Amount": order.original_amount if order else 0.0,
            f"{title} Matched Amount": order.matched_amount if order else 0.0,
            f"{title} Processed Matched Amount": order.processed_matched_amount if order else 0.0,
            f"{title} Remaining Amount": order.remaining_amount if order else 0.0,
            f"{title} Fill Reason": order.fill_reason if order else None,
            f"{title} Selected Level": order.selected_level if order else None,
            f"{title} Limit Price": order.limit_price if order else None,
            f"{title} Telegram Message ID": order.telegram_submission_message_id if order else None,
        }
        fields[f"{title} Locked Cash" if side == "buy" else f"{title} Locked Inventory"] = locked
        return fields

    def _demo_queue_ahead(self, ob: Dict[str, Any], side: str, level: int) -> float:
        if not self.cfg.demo_queue_enabled:
            return 0.0
        book_side = "bids" if side == "buy" else "asks"
        try:
            return max(0.0, float(ob[book_side][int(level)][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def _demo_no_quote_reason(self, ob: Dict[str, Any], inventory: float) -> str:
        reason_method = getattr(self.strategy, "no_quote_reason", None)
        if reason_method is None:
            return "strategy_returned_no_quote"
        return str(reason_method(ob, inventory))

    @staticmethod
    def _write_demo_csv(order_df: pd.DataFrame, csv_path: str) -> bool:
        try:
            order_df.to_csv(csv_path, index=False)
            return True
        except (OSError, ValueError) as exc:
            print(f"Demo CSV persistence failed: {exc}")
            return False

    def _sync_demo_order_rows(
        self,
        order_df: pd.DataFrame,
        *,
        changed_order_ids=None,
        snapshot=None,
        scopes=None,
        round_fee: Optional[float] = None,
    ) -> None:
        ledger = self.exchange.ledger
        changed = set(changed_order_ids or set())
        # A first ``.loc`` append can let pandas narrow all-zero columns to
        # integer dtype. Lifecycle updates may then legitimately write fees or
        # textual statuses, so keep the explicit mixed CSV schema writable.
        for column in order_df.columns:
            if order_df[column].dtype != object:
                order_df[column] = order_df[column].astype(object)
        for side in ("buy", "sell"):
            title = side.title()
            id_column = f"{title} Order ID"
            if id_column not in order_df.columns:
                continue
            for index, order_id in order_df[id_column].items():
                if not isinstance(order_id, str) or not order_id:
                    continue
                try:
                    order = ledger.get_order(order_id)
                except KeyError:
                    continue
                fields = self._demo_side_fields(side, order, None)
                for column, value in fields.items():
                    order_df.at[index, column] = value
                if order_id in changed and snapshot is not None:
                    financial_fields = {
                        "inv_current": snapshot.inventory,
                        "cash_current": snapshot.cash,
                        "Locked Cash": snapshot.locked_cash,
                        "Locked Inventory": snapshot.locked_inventory,
                        "Available Cash": snapshot.available_cash,
                        "Available Inventory": snapshot.available_inventory,
                        "Portfolio Wealth": snapshot.wealth,
                    }
                    if round_fee is not None:
                        financial_fields["Round Fee"] = round_fee
                    if scopes is not None:
                        financial_fields.update({
                            "Session Pair Fee": scopes.session_pair,
                            "Session All Symbols Fee": scopes.session_all_symbols,
                            "Pair All Time Fee": scopes.pair_all_time,
                            "All Symbols All Time Fee": scopes.all_symbols_all_time,
                        })
                    for column, value in financial_fields.items():
                        order_df.at[index, column] = value

    def _run_demo(self) -> pd.DataFrame:
        csv_path = self.csv_path or os.path.join(RESULTS_DIR, "track_demo.csv")
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        if os.path.exists(csv_path):
            try:
                order_df = pd.read_csv(csv_path).astype(object)
                for column in self.DEMO_COLUMNS:
                    if column not in order_df.columns:
                        order_df[column] = None
                order_df = order_df[self.DEMO_COLUMNS]
            except Exception:
                order_df = pd.DataFrame(columns=self.DEMO_COLUMNS, dtype=object)
        else:
            order_df = pd.DataFrame(columns=self.DEMO_COLUMNS, dtype=object)
        self._write_demo_csv(order_df, csv_path)

        self._notify_startup()
        round_no = 0
        try:
            while self._within_horizon():
                round_no += 1
                if self.cfg.max_rounds and round_no > self.cfg.max_rounds:
                    break
                now = self.now_provider()
                retry_pending = getattr(self.telegram_reporter, "retry_pending", None)
                if retry_pending is not None:
                    retry_pending(self.exchange.ledger)
                try:
                    ob = self._get_orderbook()
                except Exception as exc:
                    print(f"Market data retrieval failed: {exc}")
                    self.sleep_fn(self.cfg.time_step)
                    continue
                if not ob:
                    self.sleep_fn(self.cfg.time_step)
                    continue
                try:
                    best_bid = float(ob["bids"][0][0])
                    best_ask = float(ob["asks"][0][0])
                    last_price = float(ob["lastTradePrice"])
                except (KeyError, IndexError, TypeError, ValueError):
                    self.sleep_fn(self.cfg.time_step)
                    continue

                trade_price = ob.get("tradePrice")
                traded_volume = ob.get("tradedVolume")
                try:
                    trade_price = float(trade_price) if trade_price is not None else None
                    traded_volume = float(traded_volume) if traded_volume is not None else None
                except (TypeError, ValueError):
                    trade_price = traded_volume = None
                fill_events = self.exchange.process_market(
                    best_bid=best_bid,
                    best_ask=best_ask,
                    last_price=last_price,
                    trade_price=trade_price,
                    traded_volume=traded_volume,
                    now=now,
                )
                cancel_events = self.exchange.ledger.cancel_stale(
                    max_age_seconds=self.cfg.cancel_after_minutes * 60,
                    now=now,
                )
                lifecycle_events = fill_events + cancel_events
                round_fee = sum(event.fee_quote for event in fill_events)
                ledger = self.exchange.ledger
                snapshot = ledger.snapshot(valuation_price=last_price)
                scopes = ledger.fee_scopes()
                self._sync_demo_order_rows(
                    order_df,
                    changed_order_ids={event.order_id for event in lifecycle_events},
                    snapshot=snapshot,
                    scopes=scopes,
                    round_fee=round_fee,
                )
                for event in lifecycle_events:
                    self.telegram_reporter.order_changed(self.exchange.ledger, event)

                self._emit_algo1_diagnostic(ob, snapshot.inventory, round_no=round_no)
                quote = self._decide_order(ob, snapshot.inventory)
                quote_status = "quote_generated" if quote is not None else "no_quote"
                no_quote_reason = (
                    None if quote is not None
                    else self._demo_no_quote_reason(ob, snapshot.inventory)
                )
                bid_price = float(quote["bid"]) if quote else None
                ask_price = float(quote["ask"]) if quote else None
                bid_level = int(quote["bid level"]) if quote else None
                ask_level = int(quote["ask level"]) if quote else None

                buy_order = sell_order = None
                buy_reason = sell_reason = "no_quote" if quote is None else None
                if quote is not None:
                    sell_amount = max(
                        0.0,
                        min(self.cfg.order_size, snapshot.available_inventory - float(self.cfg.inv_min)),
                    )
                    if sell_amount > 1e-10:
                        sell_id = (
                            f"{self.pair.lower()}-sell-{round_no}-{int(now.timestamp() * 1000)}"
                        )
                        result_id = self.exchange.send_order(
                            "sell", self.cfg.currency_1, self.cfg.currency_2,
                            sell_amount, ask_price, sell_id,
                            selected_level=ask_level,
                            queue_ahead=self._demo_queue_ahead(ob, "sell", ask_level),
                            round_no=round_no,
                            now=now,
                        )
                        if result_id:
                            sell_order = ledger.get_order(result_id)
                            self.telegram_reporter.order_submitted(ledger, sell_order)
                        else:
                            sell_reason = "rejected"
                    else:
                        sell_reason = "insufficient_inventory"

                    snapshot = ledger.snapshot(valuation_price=last_price)
                    buy_locked_amount = sum(
                        order.remaining_amount for order in ledger.active_orders.values()
                        if order.side == "buy"
                    )
                    affordable = snapshot.available_cash / bid_price if bid_price and bid_price > 0 else 0.0
                    buy_amount = max(0.0, min(self.cfg.order_size, affordable))
                    if buy_amount <= 1e-10:
                        buy_reason = "insufficient_cash"
                    elif buy_locked_amount + snapshot.inventory + self.cfg.order_size >= self.cfg.inv_max:
                        buy_reason = "inventory_cap"
                    else:
                        buy_id = (
                            f"{self.pair.lower()}-buy-{round_no}-{int(now.timestamp() * 1000)}"
                        )
                        result_id = self.exchange.send_order(
                            "buy", self.cfg.currency_1, self.cfg.currency_2,
                            buy_amount, bid_price, buy_id,
                            selected_level=bid_level,
                            queue_ahead=self._demo_queue_ahead(ob, "buy", bid_level),
                            round_no=round_no,
                            now=now,
                        )
                        if result_id:
                            buy_order = ledger.get_order(result_id)
                            self.telegram_reporter.order_submitted(ledger, buy_order)
                        else:
                            buy_reason = "rejected"

                snapshot = ledger.snapshot(valuation_price=last_price)
                scopes = ledger.fee_scopes()
                row = {
                    "Session_id": str(self.session),
                    "Algorithm": self.algorithm_id,
                    "Crypto": self.pair,
                    "Mode": "demo",
                    "Round": round_no,
                    "Timestamp": now.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "Quote Status": quote_status,
                    "No Quote Reason": no_quote_reason,
                    "Bid Level": bid_level,
                    "Ask Level": ask_level,
                    "inv_current": snapshot.inventory,
                    "cash_current": snapshot.cash,
                    "Locked Cash": snapshot.locked_cash,
                    "Locked Inventory": snapshot.locked_inventory,
                    "Available Cash": snapshot.available_cash,
                    "Available Inventory": snapshot.available_inventory,
                    "Portfolio Wealth": snapshot.wealth,
                    "Best Bid": best_bid,
                    "Best Ask": best_ask,
                    "Last Price": last_price,
                    "Bid Price": bid_price,
                    "Ask Price": ask_price,
                    "Round Fee": round_fee,
                    "Session Pair Fee": scopes.session_pair,
                    "Session All Symbols Fee": scopes.session_all_symbols,
                    "Pair All Time Fee": scopes.pair_all_time,
                    "All Symbols All Time Fee": scopes.all_symbols_all_time,
                }
                row.update(self._demo_side_fields("buy", buy_order, buy_reason))
                row.update(self._demo_side_fields("sell", sell_order, sell_reason))
                order_df.loc[len(order_df)] = [row.get(column) for column in self.DEMO_COLUMNS]
                self._write_demo_csv(order_df, csv_path)
                self.sleep_fn(self.cfg.time_step)
        except KeyboardInterrupt:
            print("Interrupted by user.")
        finally:
            shutdown_time = self.now_provider()
            shutdown_events = self.exchange.cancel_all_open_orders(now=shutdown_time)
            ledger = self.exchange.ledger
            shutdown_snapshot = ledger.snapshot(
                valuation_price=getattr(ledger, "valuation_price", 0.0)
            )
            shutdown_scopes = ledger.fee_scopes()
            self._sync_demo_order_rows(
                order_df,
                changed_order_ids={event.order_id for event in (shutdown_events or [])},
                snapshot=shutdown_snapshot,
                scopes=shutdown_scopes,
            )
            for event in shutdown_events or []:
                self.telegram_reporter.order_changed(self.exchange.ledger, event)
            self._write_demo_csv(order_df, csv_path)
        try:
            return pd.read_csv(csv_path)
        except (OSError, ValueError, pd.errors.EmptyDataError):
            return order_df

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
        
        if self.mode_label == "demo":
            return self._run_demo()

        round_no = 0
    
        # Ensure tracking containers exist
        if not hasattr(self, "tracked_orders"):
            self.tracked_orders = {"buy": set(), "sell": set()}
        if not hasattr(self, "recent_order_ids"):
            self.recent_order_ids = []

        if not hasattr(self, "session"):
            self.session = int(datetime.now().timestamp())

        self._notify_startup()

        # Initial inventory & cash vectors
        inv_coins, inv_cash = [self.inv_initial], [self.cash_initial]


        

        # Insert new session (skipped in demo/paper-trading mode)
        if self.persist_to_db:
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
        csv_path = getattr(self, "csv_path", getattr(self.cfg, "output_csv", None)) \
            or os.path.join(RESULTS_DIR, "track.csv")
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
                stamp = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")
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
                    statuse='all',
                    inv_coin=inv_coins[-1],
                    inv_cash=inv_cash[-1]
                )
                inv_coins.append(inv_delta)
                inv_cash.append(cash_delta)
                inv_current, cash_current = inv_delta, cash_delta
                portfolio_wealth = inv_current * last_price + cash_current

                round_fee_irt = getattr(self, "last_round_fee_irt", 0.0)
                symbol_total_fee_irt, combined_total_fee_irt = self._accumulate_cost(round_fee_irt)

                print(f"[{stamp}] Inventory={inv_current:.6f}, Cash={cash_current:.2f}, Wealth={portfolio_wealth:.2f}")
    
                # ---- (C2) Cancel stale orders & reflect cancellations in DF/tracking ----
                self._cancel_owned_stale_orders(
                    max_age_minutes=self.cfg.cancel_after_minutes,
                    df=order_df,
                )
    
                # ---- (D) Decide & place orders ----
                bid_lvl = ask_lvl = None
                bid_price = ask_price = None
                bid_price_sent = ask_price_sent = None
                buy_sent = sell_sent = 0
                buy_id = sell_id = None
                buy_size_value = 0.0  # NEW: will store actual submitted buy size
                sell_size_value = 0.0 # NEW: will store actual submitted sell size
    
                self._emit_algo1_diagnostic(ob, float(inv_current), round_no=round_no)
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

                self._notify_telegram(self._format_round_message(
                    round_no, stamp, best_bid, best_ask, last_price,
                    bid_price, ask_price, bid_lvl, ask_lvl,
                    inv_current, cash_current, portfolio_wealth,
                    buy_sent, buy_size_value, bid_price_sent, buy_id,
                    sell_sent, sell_size_value, ask_price_sent, sell_id,
                    round_fee_irt, symbol_total_fee_irt, combined_total_fee_irt,
                ))

                # ---- (F) Persist: rewrite full CSV so prior status edits are saved ----
                order_df.to_csv(csv_path, index=False)
    
                print(order_df.tail())
                time.sleep(self.cfg.time_step)
    
        except KeyboardInterrupt:
            print("Interrupted by user.")
            # Replace NaN with None so MySQL gets NULL instead of "nan"
            order_df = order_df.where(pd.notnull(order_df), None)

            if self.persist_to_db:
                self.save_to_db(order_df)
        finally:
            print("Cancelling all open orders before exit...")
            try:
                self._cancel_owned_open_orders(max_pages=10)
                

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

        if self.persist_to_db:
            self.save_to_db(order_df)
        return order_df
