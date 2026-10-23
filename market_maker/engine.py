import json
import math
import signal
import threading
import time
from collections import deque
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List, Set  # add Set
import pandas as pd

from config import MMConfig, RESULTS_DIR
from portfolio import Nobitex_portfolio
from strategy.base import Strategy
from strategy.catalog import algorithm_label, build_strategy, strategy_key
from exchange.base import ExchangeClient
from safety import enforce_engine_safety
from telegram_lifecycle import PaperTelegramReporter, TelegramClient
from no_quote import NoQuoteDecision, NoQuoteMonitor
from result_store import DemoResultStore
import pytz
from datetime import datetime, timedelta
import os
import mysql.connector
from dotenv import load_dotenv

if os.environ.get("LOB_DISABLE_DOTENV") != "1":
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

# Floor for an order whose size has been clamped by the balance available,
# as a fraction of the configured order_size. Expressed as a fraction rather
# than an absolute figure so it holds for both pairs at once (BTC trades
# 0.000015 per order, USDT trades 1). See SimpleMarketMaker._min_tradable_size.
MIN_ORDER_SIZE_FRACTION = 0.1

# Statuses that mean "this order is resting on the book". Nobitex reports an
# open order as "Active" (or "New" just after it is accepted); the paper
# exchange and the test doubles say "open". Compared case-insensitively.
# Anything else in an open-order listing is still treated as unknown exposure.
OPEN_ORDER_STATUSES = frozenset({"open", "active", "new"})


class CoordinatedShutdown(KeyboardInterrupt):
    """Raised only by the installed SIGTERM/SIGINT coordination handler."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"coordinated shutdown signal {self.signum}")




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
        self.monotonic_provider = time.monotonic
        self.telegram_client = TelegramClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.telegram_reporter = PaperTelegramReporter(self.telegram_client)
        self.result_store = None
        self.no_quote_monitor = None
        # A demo lane (run.py's LaneRunner) is stopped through this event:
        # the next wait between rounds ends like a shutdown signal would.
        self.stop_event: Optional[threading.Event] = None
        self.stop_signum = int(signal.SIGTERM)
        # (time in seconds, mid) samples for the trend pause.
        self._mid_history: deque = deque()

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
        self.strategy = strategy if strategy is not None else build_strategy(self.cfg)
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

    def _wait_for_next_round(self, previous_target: float) -> float:
        interval = float(self.cfg.time_step)
        if interval <= 0:
            return previous_target
        target = previous_target + interval
        now = float(self.monotonic_provider())
        if now > target:
            missed = int((now - target) // interval) + 1
            target += missed * interval
        delay = max(0.0, target - now)
        if delay:
            self._sleep_checking_stop(delay)
        return target

    def _sleep_checking_stop(self, delay: float) -> None:
        """Sleep ``delay`` seconds, in slices of at most one second when a
        stop event is attached (a demo lane): a set event raises
        CoordinatedShutdown, so the loop ends the way a signal ends it."""
        event = getattr(self, "stop_event", None)       # a bot built without __init__ (tests) has none
        if event is None:
            self.sleep_fn(delay)
            return
        if event.is_set():
            raise CoordinatedShutdown(self.stop_signum)
        remaining = float(delay)
        while remaining > 0:
            step = min(1.0, remaining)
            self.sleep_fn(step)
            remaining -= step
            if event.is_set():
                raise CoordinatedShutdown(self.stop_signum)

    def _record_config_reload_error(self, error: Optional[str]) -> None:
        print(json.dumps({
            "event": "demo_config_reload_failed",
            "session_id": str(self.session),
            "symbol": self.pair,
            "error": str(error or "unknown configuration reload error"),
            "timestamp": self.now_provider().isoformat(),
        }, sort_keys=True), flush=True)
        

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

    def _cancel_live_order(self, record) -> Set[str]:
        """Live: cancel one resting order of ours (a record of
        get_open_order_totals: price, remaining, clientOrderId, exchange id)
        and forget it. Returns the cancelled client id, or nothing when the
        exchange refused."""
        if len(record) < 4 or record[3] is None:
            return set()
        client_id = str(record[2])
        try:
            self.exchange.cancel_order(record[3])
        except Exception as exc:  # noqa: BLE001 -- a refused cancel must not stop the round
            print(json.dumps({
                "event": "live_requote_cancel_failed", "client_order_id": client_id,
                "error": f"{type(exc).__name__}: {exc}", "symbol": self.pair,
            }, sort_keys=True), flush=True)
            return set()
        return self._prune_cancelled_active_ids({client_id})

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
          - whose clientOrderId is one we sent (recent_order_ids)
          - matching self.cfg.pair
        """
        # Inspect the unfiltered response: discarding an incomplete owned order
        # would make its locked balance look like zero to the live quote loop.
        orders = self.exchange.get_order_list_status(
            status='open', details=2, page_size=100, max_pages=100,
        )
        if not isinstance(orders, list):
            raise RuntimeError("open order exposure is unavailable")
        want_pair = str(self.cfg.pair).upper()
    
        buy_amount_locked  = 0.0
        buy_cash_locked    = 0.0
        sell_amount_locked = 0.0
        sell_cash_value    = 0.0
        # (price, remaining, clientOrderId, exchange id) of our resting
        # orders, for the re-post guard and the replacement rule.
        self._resting_orders = {"buy": [], "sell": []}
    
        def _f(x: Any) -> float:
            if isinstance(x, bool):
                raise RuntimeError("open order exposure has an invalid amount or price")
            try:
                value = float(x)
            except (TypeError, ValueError):
                raise RuntimeError("open order exposure has an invalid amount or price")
            if not math.isfinite(value):
                raise RuntimeError("open order exposure has a non-finite amount or price")
            return value
    
        for o in orders:
            if not isinstance(o, dict):
                raise RuntimeError("open order exposure has an invalid record")
            clid = str(o.get("clientOrderId") or "")
            pair = str(o.get("pair") or "").upper()
            side = str(o.get("type") or "").lower()
            if pair and pair != want_pair and clid not in self.recent_order_ids:
                continue
            if not clid or not pair:
                raise RuntimeError("open order exposure has unknown ownership or pair")
            if pair != want_pair:
                raise RuntimeError("open order exposure has an unexpected owned pair")
    
            # Count only our orders: either we tracked them recently or they carry our algo prefix
            is_ours = (clid in getattr(self, "recent_order_ids", []))
            if not is_ours:
                continue
    
            status = str(o.get("status") or "").strip().lower()
            if side not in {"buy", "sell"} or status not in OPEN_ORDER_STATUSES:
                raise RuntimeError("open order exposure has unknown side or status")
            amt   = _f(o.get("unmatchedAmount"))
            price = _f(o.get("price"))
            if amt < 0 or price <= 0:
                raise RuntimeError("open order exposure has an invalid amount or price")
    
            if side == "buy":
                buy_amount_locked += amt
                buy_cash_locked   += amt * price
            elif side == "sell":
                sell_amount_locked += amt
                sell_cash_value    += amt * price
            self._resting_orders[side].append((price, amt, clid, o.get("id")))
    
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
    # etc.) and the characterization tests keep working unchanged.
    def _depth_cap(self, ob: Dict[str, Any]) -> int:
        return self.strategy.depth_cap(ob)

    def _tick_size(self) -> float:
        method = getattr(self.strategy, "tick_size", None)
        if callable(method):
            return float(method())
        # Strategy stand-ins (tests, future algorithms) may not expose a tick.
        return 10.0 if str(self.cfg.pair).upper().endswith("IRT") else 0.01

    def _spread_ok(self, ob: Dict[str, Any], bid_idx: int, ask_idx: int) -> bool:
        return self.strategy.spread_ok(ob, bid_idx, ask_idx)

    # ---------- Quote selection ----------
    def _decide_order(self, ob: Dict[str, Any], inv_current: float) -> Optional[Dict[str, Any]]:
        return self.strategy.decide_order(ob, inv_current)

    def _strategy_name(self) -> str:
        """The catalog key written into every round row ("Strategy"), or the
        class name of a stand-in strategy."""
        return strategy_key(self.strategy) or self.strategy.__class__.__name__

    def _sync_strategy_with_config(self) -> None:
        """Demo hot reload: when the admin changes the ``algorithm`` field,
        build the new strategy at the round boundary (its memory starts
        empty; the ledger, inventory and session continue). A stand-in
        strategy without a catalog key (tests) is never replaced."""
        wanted = getattr(self.cfg, "algorithm", None)
        current = strategy_key(self.strategy)
        if current is None or not wanted or wanted == current:
            return
        try:
            replacement = build_strategy(self.cfg, wanted)
        except KeyError:
            print(json.dumps({
                "event": "unknown_algorithm_ignored", "algorithm": wanted,
                "session_id": str(self.session), "symbol": self.pair,
                "timestamp": self.now_provider().isoformat(),
            }, sort_keys=True), flush=True)
            return
        self.strategy = replacement
        print(json.dumps({
            "event": "algorithm_switched", "from": current, "to": wanted,
            "session_id": str(self.session), "symbol": self.pair,
            "timestamp": self.now_provider().isoformat(),
        }, sort_keys=True), flush=True)

    @staticmethod
    def _quote_side_price(quote: Optional[Dict[str, Any]], key: str) -> Optional[float]:
        """A v2 quote may carry one side only (``bid`` or ``ask`` is None)."""
        if not quote or quote.get(key) is None:
            return None
        return float(quote[key])

    @staticmethod
    def _quote_side_level(quote: Optional[Dict[str, Any]], key: str) -> Optional[int]:
        if not quote or quote.get(key) is None:
            return None
        try:
            return int(quote[key])
        except (TypeError, ValueError):
            return None

    def _requote_band(self, price: Optional[float]) -> float:
        """The band (in price units) within which a resting order of ours
        counts as "the same price": strategy/common.py's requote_band when
        the strategy has it, else the same rule from the settings."""
        if price is None:
            return 0.0
        method = getattr(self.strategy, "requote_band", None)
        if callable(method):
            try:
                return max(0.0, float(method(price)))
            except (TypeError, ValueError):
                pass
        ticks = max(0, int(getattr(self.cfg, "hysteresis_ticks", 0) or 0))
        perc = max(0.0, float(getattr(self.cfg, "requote_band_perc", 0.0) or 0.0))
        return max(ticks * self._tick_size(), perc * float(price))

    def _resting_order_blocks(self, price: Optional[float], resting) -> bool:
        """True when an order of ours already rests within the requote band
        of ``price``: re-posting it every round would only churn and lose
        queue position. ``resting`` is an iterable whose items start with
        (price, remaining amount)."""
        if price is None:
            return False
        band = self._requote_band(price)
        if band <= 0:
            return False
        for item in resting:
            resting_price, remaining = item[0], item[1]
            try:
                if float(remaining) > 0 and abs(float(resting_price) - float(price)) <= band + 1e-9:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _trend_paused_sides(self, mid: Optional[float], now_seconds: float) -> Set[str]:
        """The sides the trend pause blocks this round: "buy" after the mid
        fell by more than trend_pause_perc over the last trend_window_seconds,
        "sell" after it rose by as much. Nothing until the samples cover at
        least half the window; nothing when either setting is 0."""
        history = self._mid_history
        window = float(getattr(self.cfg, "trend_window_seconds", 0.0) or 0.0)
        pause = float(getattr(self.cfg, "trend_pause_perc", 0.0) or 0.0)
        try:
            mid_value = float(mid)
        except (TypeError, ValueError):
            return set()
        if not math.isfinite(mid_value) or mid_value <= 0:
            return set()
        history.append((float(now_seconds), mid_value))
        if window <= 0 or pause <= 0:
            while len(history) > 1:
                history.popleft()
            return set()
        while history and history[0][0] < now_seconds - window:
            history.popleft()
        if not history or history[0][0] > now_seconds - window / 2.0:
            return set()
        drift = mid_value / history[0][1] - 1.0
        if drift <= -pause:
            return {"buy"}
        if drift >= pause:
            return {"sell"}
        return set()

    def _cancel_paper_orders(self, orders, *, reason: str, now) -> list:
        """Demo: cancel the given paper orders of ours; the events back-fill
        the rows that created them."""
        events = []
        ledger = self.exchange.ledger
        for order in orders:
            if order.status not in {"open", "partially_filled"}:
                continue
            events.append(ledger.cancel_order(
                order.client_order_id, reason=reason, now=now,
                initiator="demo_engine", call_site=f"SimpleMarketMaker.{reason}",
            ))
        return events

    def _paper_orders_outside_band(self, side: str, price: Optional[float], orders) -> list:
        """The resting paper orders on ``side`` that a new ``price`` replaces:
        every one farther than the requote band from it (one order per side)."""
        if price is None:
            return []
        band = self._requote_band(price)
        return [
            order for order in orders
            if order.side == side and order.status in {"open", "partially_filled"}
            and abs(float(order.limit_price) - float(price)) > band + 1e-9
        ]

    def _emit_algo1_diagnostic(
        self, ob: Dict[str, Any], inv_current: float, *, round_no: int,
        document: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not getattr(self.cfg, "algo1_diagnostics_enabled", False):
            return
        diagnostic_method = getattr(self.strategy, "diagnostics", None)
        if diagnostic_method is None:
            return
        if document is None:
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
        self._rotate_diagnostics(path)

    def _rotate_diagnostics(self, path: str) -> None:
        """Keep the JSONL bounded: once it holds twice
        ``algo1_diagnostics_keep_rounds`` documents, rewrite it with the
        newest ``keep`` only. The dashboard reads the tail of this file."""
        keep = int(getattr(self.cfg, "algo1_diagnostics_keep_rounds", 0) or 0)
        if keep <= 0:
            return
        count = getattr(self, "_diagnostic_line_count", None)
        if count is None:
            try:
                with open(path, "rb") as stream:
                    count = sum(1 for _ in stream)
            except OSError:
                count = 0
        else:
            count += 1
        self._diagnostic_line_count = count
        if count <= 2 * keep:
            return
        # Stream the tail into a temporary file: the documents are large
        # (every scored pair), so the file must never be read into memory.
        skip = max(0, count - keep)
        temporary = f"{path}.{os.getpid()}.tmp"
        try:
            kept = 0
            with open(path, "rb") as source, open(temporary, "wb") as target:
                for index, line in enumerate(source):
                    if index < skip:
                        continue
                    target.write(line)
                    kept += 1
            os.replace(temporary, path)
            self._diagnostic_line_count = kept
        except OSError as exc:
            print(f"Diagnostics rotation failed: {exc}")
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _algo1_diagnostic_document(
        self, ob: Dict[str, Any], inv_current: float, *, round_no: int
    ) -> Dict[str, Any]:
        diagnostic_method = getattr(self.strategy, "diagnostics", None)
        if diagnostic_method is None:
            exp_ret_min = (
                self.cfg.commission_perc
                if self.cfg.exp_ret_min is None
                else self.cfg.exp_ret_min
            )
            return {
                "symbol": self.pair,
                "session_id": str(self.session),
                "round": int(round_no),
                "commission_perc": float(self.cfg.commission_perc),
                "exp_ret_min": float(exp_ret_min),
                "required_threshold": float(exp_ret_min),
                "min_required_return": float(exp_ret_min),
                "candidate_results": [],
            }
        return diagnostic_method(
            ob, inv_current, round_no=round_no, session_id=self.session
        )

    # ---------- Wallet / order endpoints ----------
    # NOTE: exchange-specific I/O now lives in exchange/nobitex.py (NobitexExchangeClient).
    def wallet_balance(self, currency: str) -> Optional[float]:
        return self.exchange.wallet_balance(currency)

    def _load_live_starting_balances(self) -> Tuple[float, float]:
        """Seed a live session from the current Nobitex spot-wallet balances.

        Static profile values are useful as historical defaults, but must not
        define the capital available to a real forward run. Refuse to start if
        either authenticated balance lookup fails so the worker cannot trade
        using stale or guessed cash/inventory values.
        """
        currencies = (self.cfg.currency_1, self.cfg.currency_2)
        balances = []
        for currency in currencies:
            value = self.wallet_balance(currency)
            if value is None or not math.isfinite(float(value)) or float(value) < 0:
                raise RuntimeError(
                    f"Unable to read a valid live Nobitex balance for {currency}; "
                    "refusing to start real trading."
                )
            balances.append(float(value))

        self.inv_initial, self.cash_initial = balances
        print(json.dumps({
            "event": "live_wallet_snapshot",
            "pair": self.pair,
            "base_currency": currencies[0],
            "base_balance": self.inv_initial,
            "quote_currency": currencies[1],
            "quote_balance": self.cash_initial,
            "timestamp": self.now_provider().isoformat(),
        }, sort_keys=True), flush=True)
        return self.inv_initial, self.cash_initial

    # ---------- Telegram notifications ----------
    def _notify_telegram(self, text: str) -> bool:
        """Best-effort push to Telegram. No-ops if unconfigured; never
        raises, so a network hiccup can't take down the trading loop.
        Returns True only when Telegram returns HTTP success, JSON ok=true,
        and a message identifier."""
        return self.telegram_client.send(text) is not None

    def _safe_reporter_call(self, method_name: str, *args, **kwargs):
        method = getattr(self.telegram_reporter, method_name, None)
        if method is None:
            return None
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            print(json.dumps({
                "event": "telegram_reporting_error",
                "method": method_name,
                "session_id": str(self.session),
                "symbol": self.pair,
                "process_id": os.getpid(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "timestamp": self.now_provider().isoformat(),
            }, sort_keys=True), flush=True)
            return None

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
            f"Algorithm: <b>{algorithm_label(strategy_key(self.strategy))}</b> "
            f"({self.strategy.__class__.__name__}, profile: {self.algorithm_id})\n"
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
        quote: Optional[Dict[str, Any]] = None,
    ) -> str:
        if bid_price is None and ask_price is None:
            quote_line = "No quote this round"
        else:
            bid_text = f"Bid: {bid_price:,.2f} (lvl {bid_lvl})" if bid_price is not None else "Bid: —"
            ask_text = f"Ask: {ask_price:,.2f} (lvl {ask_lvl})" if ask_price is not None else "Ask: —"
            quote_line = f"{bid_text}   {ask_text}"
        economics_line = ""
        if quote is not None:
            net_return = quote.get("net return")
            relative_spread = quote.get("relative quoted spread")
            if net_return is not None and relative_spread is not None:
                economics_line = (
                    f"Rel. quoted spread: {relative_spread:.2%}   "
                    f"Net return (after fees): {net_return:.3%}\n"
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
            f"Last: {last_price:,.2f}\n"
            f"{economics_line}"
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
    
        # The live worker loads this table with ``pd.read_csv``; since pandas 3
        # a column of client order ids is typed ``str`` and refuses the matched
        # amount (a float) written below. Keep both status columns object typed
        # so an id can become an amount (the BTC live worker exited on this
        # TypeError on 17 October 2026).
        for column in ("Buy Status", "Sell Status"):
            if column in order_df.columns and order_df[column].dtype != object:
                order_df[column] = order_df[column].astype(object)

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
        "Session_id", "Algorithm", "Strategy", "Crypto", "Mode", "Round", "Timestamp",
        "Quote Status", "No Quote Reason", "No Quote Consecutive Count",
        "No Quote Episode Seconds", "No Quote Alert State",
        "No Quote Candidate Diagnostics", "Bid Level", "Ask Level",
        "inv_current", "cash_current", "Locked Cash", "Locked Inventory",
        "Available Cash", "Available Inventory", "Portfolio Wealth",
        "Best Bid", "Best Ask", "Last Price", "Bid Price", "Ask Price",
        "Mid Price", "Relative Quoted Spread", "Gross Return", "Net Return",
        "Min Required Return",
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
        # The strategy's decision fields (dashboard "Quote decision" /
        # "Fill model" / "Algorithm" tabs); None on no-quote rounds, on the
        # absent side, and where the algorithm has no such number
        # (Algorithm 3 reports no fill probability, Algorithm 1 no imbalance).
        "Quote Mode", "Selection Reason", "Centre", "Bid Distance", "Ask Distance",
        "Bid Fill Prob", "Ask Fill Prob", "Expected Value", "Reference Price",
        "Imbalance", "Microprice",
    ]

    # The same fields, in the order the live CSV and the demo rows carry them.
    QUOTE_EXTRA_COLUMNS = [
        "Quote Mode", "Selection Reason", "Centre", "Bid Distance", "Ask Distance",
        "Bid Fill Prob", "Ask Fill Prob", "Expected Value", "Reference Price",
        "Imbalance", "Microprice",
    ]

    @staticmethod
    def _quote_extra_fields(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The strategy's decision numbers for the round row (all None when
        there was no quote or the strategy does not report them)."""
        keys = {
            "Quote Mode": "quote mode", "Selection Reason": "selection reason",
            "Centre": "centre", "Bid Distance": "bid distance", "Ask Distance": "ask distance",
            "Bid Fill Prob": "bid exec prob", "Ask Fill Prob": "ask exec prob",
            "Expected Value": "expected value", "Reference Price": "reference price",
            "Imbalance": "imbalance", "Microprice": "microprice",
        }
        if not quote:
            return {column: None for column in keys}
        return {column: quote.get(key) for column, key in keys.items()}

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

    def _min_tradable_size(self) -> float:
        """Smallest order size worth sending, in base units.

        Buy/sell sizes are clamped down to the balance actually available,
        which is correct until the remainder is a sliver: the old 1e-10 floor
        let a buy shrink to 0.0003 USDT (594 IRT, about one US cent) purely to
        consume the last of the cash. No exchange accepts orders that small,
        and it left the worker with zero cash and nothing to trade. Anything
        at or above this floor is still sized to fit, so ordinary partial
        capacity is unaffected.
        """
        return max(1e-10, abs(float(self.cfg.order_size)) * MIN_ORDER_SIZE_FRACTION)

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _inventory_sized_order_targets(
        self,
        inventory: float,
        open_buy_amount: float = 0.0,
        open_sell_amount: float = 0.0,
    ) -> Tuple[float, float]:
        """Linear inventory-based (buy, sell) target sizes, in base units.

        With x the position inside the [inv_min, inv_max] band (0 = empty,
        1 = full) and k = inventory_sizing_strength:

            buy  = order_size * (1 - k * (2x - 1))
            sell = order_size * (1 + k * (2x - 1))

        each floored at inventory_sizing_min_fraction * order_size. With
        k = 1 and no floor this is buy = 2*order_size*(1 - x) and
        sell = 2*order_size*x: buying fades to zero as inventory fills,
        selling fades to zero as it empties.

        Each side uses its own *effective* inventory, so resting orders
        count before they fill: the buy side sees inventory plus the amount
        already resting in open buys, the sell side sees inventory minus the
        amount already resting in open sells. Without this, a new order is
        sent every round at the same size until the first fill arrives.
        """
        base_size = float(self.cfg.order_size)
        if not getattr(self.cfg, "inventory_sizing_enabled", False):
            return base_size, base_size

        inv_min = float(self.cfg.inv_min)
        inv_max = float(self.cfg.inv_max)
        inventory_band = inv_max - inv_min
        if inventory_band <= 0:
            return base_size, base_size

        strength = self._clamp(
            float(getattr(self.cfg, "inventory_sizing_strength", 0.0)),
            0.0,
            1.0,
        )
        min_fraction = self._clamp(
            float(getattr(self.cfg, "inventory_sizing_min_fraction", 0.0)),
            0.0,
            1.0,
        )

        def pressure(effective_inventory: float) -> float:
            fraction = self._clamp(
                (effective_inventory - inv_min) / inventory_band, 0.0, 1.0
            )
            return 2.0 * fraction - 1.0

        buy_pressure = pressure(float(inventory) + max(0.0, float(open_buy_amount)))
        sell_pressure = pressure(float(inventory) - max(0.0, float(open_sell_amount)))
        buy_multiplier = self._clamp(
            1.0 - strength * buy_pressure,
            min_fraction,
            1.0 + strength,
        )
        sell_multiplier = self._clamp(
            1.0 + strength * sell_pressure,
            min_fraction,
            1.0 + strength,
        )
        return base_size * buy_multiplier, base_size * sell_multiplier

    @staticmethod
    def _locked_exposure_ratio(projected_locked: float, resource_total: float) -> float:
        """projected_locked / resource_total, safe when resource_total is 0.

        Zero resource with zero projected exposure is vacuously fine (ratio
        0.0, e.g. a disabled/never-funded account). Zero resource with any
        positive projected exposure can never be financed, so it is reported
        as unboundedly over any finite configured limit rather than raising.
        """
        if resource_total > 0:
            return projected_locked / resource_total
        return float("inf") if projected_locked > 0 else 0.0

    def _locked_exposure_guard_reason(
        self,
        *,
        side: str,
        enabled: bool,
        limit_ratio: float,
        locked_before: float,
        new_order_amount: float,
        resource_total: float,
        reason: str,
    ) -> Optional[str]:
        """Shared BUY-cash / SELL-inventory pre-trade exposure guard.

        Existing open orders are never touched -- this only ever decides
        whether to submit a NEW order, evaluated after every other sizing/cap
        check has already passed (see the call sites in _run_demo and the
        real-mode loop). Denominator is the account's total relevant
        resource before reservation (free + already locked for this side),
        so the ratio does not shrink just because capital moved from free to
        locked -- see MMConfig.buy_locked_cash_limit_ratio's docstring.
        """
        if not enabled:
            return None
        projected_locked = locked_before + new_order_amount
        ratio = self._locked_exposure_ratio(projected_locked, resource_total)
        if ratio <= limit_ratio:
            return None
        print(json.dumps({
            "event": "locked_exposure_order_rejected",
            "pair": self.cfg.pair,
            "side": side,
            "reason": f"not_sent_{reason}",
            "locked_before": locked_before,
            "new_order_amount": new_order_amount,
            "projected_locked": projected_locked,
            "resource_total": resource_total,
            "projected_ratio": ratio,
            "configured_limit": limit_ratio,
            "session_id": str(self.session),
            "process_id": os.getpid(),
            "timestamp": self.now_provider().isoformat(),
        }, sort_keys=True), flush=True)
        return reason

    def _locked_cash_guard_reason(
        self, *, locked_buy_cash: float, new_order_cash: float, cash_base: float,
    ) -> Optional[str]:
        return self._locked_exposure_guard_reason(
            side="BUY",
            enabled=bool(getattr(self.cfg, "buy_locked_cash_limit_enabled", False)),
            limit_ratio=float(getattr(self.cfg, "buy_locked_cash_limit_ratio", 1.0)),
            locked_before=locked_buy_cash,
            new_order_amount=new_order_cash,
            resource_total=cash_base,
            reason="locked_cash_limit",
        )

    def _locked_inventory_guard_reason(
        self, *, locked_sell_inventory: float, new_order_amount: float, inventory_base: float,
    ) -> Optional[str]:
        return self._locked_exposure_guard_reason(
            side="SELL",
            enabled=bool(getattr(self.cfg, "sell_locked_inventory_limit_enabled", False)),
            limit_ratio=float(getattr(self.cfg, "sell_locked_inventory_limit_ratio", 1.0)),
            locked_before=locked_sell_inventory,
            new_order_amount=new_order_amount,
            resource_total=inventory_base,
            reason="locked_inventory_limit",
        )

    def _demo_queue_ahead(self, ob: Dict[str, Any], side: str, level: Optional[int]) -> float:
        """Displayed volume at the chosen level; 0 for a synthetic level
        (None, beyond the displayed book) or when the queue model is off."""
        if not self.cfg.demo_queue_enabled or level is None:
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

    def _ensure_demo_operations(self, csv_path: str) -> None:
        if self.result_store is not None:
            return
        db_path = self.cfg.demo_result_db_path or f"{os.path.splitext(csv_path)[0]}.sqlite3"
        self.result_store = DemoResultStore(
            db_path,
            self.DEMO_COLUMNS,
            csv_export_path=csv_path,
            csv_export_max_rows=self.cfg.demo_csv_export_max_rows,
        )
        self.result_store.import_legacy_csv_once(csv_path)
        self.no_quote_monitor = NoQuoteMonitor(
            self.result_store,
            str(self.session),
            self.pair,
            alert_rounds=self.cfg.no_quote_alert_rounds,
            alert_seconds=self.cfg.no_quote_alert_seconds,
            cooldown_seconds=self.cfg.no_quote_alert_cooldown_seconds,
        )

    @staticmethod
    def _no_quote_diagnostics(document: Dict[str, Any]) -> Dict[str, Any]:
        candidates = []
        for candidate in document.get("candidate_results", []):
            candidates.append({
                "bid_level": candidate.get("bid_level"),
                "ask_level": candidate.get("ask_level"),
                "bid_price": candidate.get("bid_price"),
                "ask_price": candidate.get("ask_price"),
                "submitted_bid_price": candidate.get("submitted_bid_price"),
                "submitted_ask_price": candidate.get("submitted_ask_price"),
                "last_price": candidate.get("last_price"),
                "mid_price": candidate.get("mid_price"),
                "spread": candidate.get("spread_ratio"),
                "relative_quoted_spread": candidate.get("relative_quoted_spread"),
                "gross_return": candidate.get("gross_return"),
                "net_return": candidate.get("net_return"),
                "passed": candidate.get("passed"),
            })
        return {
            "commission": document.get("commission_perc"),
            "exp_ret_min": document.get("exp_ret_min"),
            "required_threshold": document.get("required_threshold"),
            "min_required_return": document.get("min_required_return"),
            "candidates": candidates,
        }

    def _deliver_no_quote_decision(self, decision: NoQuoteDecision) -> None:
        if decision is None or not decision.kind:
            return
        if decision.kind == "recovered":
            method = getattr(self.telegram_reporter, "no_quote_recovered", None)
        else:
            method = getattr(self.telegram_reporter, "no_quote_alert", None)
        if method is None:
            self.no_quote_monitor.suppress_pending()
            return
        try:
            message_id = method(decision)
        except Exception as exc:
            print(json.dumps({
                "event": "telegram_reporting_error",
                "method": getattr(method, "__name__", "no_quote_notification"),
                "session_id": str(self.session),
                "symbol": self.pair,
                "process_id": os.getpid(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "timestamp": self.now_provider().isoformat(),
            }, sort_keys=True), flush=True)
            return
        if message_id is not None:
            self.no_quote_monitor.mark_delivered(decision, message_id)
            return
        client = getattr(self.telegram_reporter, "client", None)
        if client is not None and not client.configured:
            self.no_quote_monitor.suppress_pending()

    def _update_demo_result_orders(
        self,
        events,
        *,
        snapshot,
        scopes,
        round_fee: Optional[float] = None,
    ) -> None:
        if self.result_store is None or not events:
            return
        lifecycle = {}
        for event in events:
            try:
                order = self.exchange.ledger.get_order(event.order_id)
            except KeyError:
                continue
            lifecycle[event.order_id] = self._demo_side_fields(order.side, order, None)
        # Only the ORDER's lifecycle fields are back-filled onto the round row
        # that created it. The portfolio snapshot columns (inv_current /
        # cash_current / locked+available / Portfolio Wealth / Round Fee / fee
        # scopes) are a per-round TIME SERIES and must keep the values observed
        # in that round.
        #
        # They used to be rewritten here with the CURRENT snapshot, and since
        # update_order_rows() locates the historical row by order id, an order
        # opened in round 2 and filled in round 9 stamped round 9's balances
        # onto round 2's row. The SQLite store is canonical and the CSV is
        # exported from it, so this corrupted both. Verified: engine inventory
        # rose a clean +0.999/round while the store recorded
        # 0.999, 7.992, 8.991, 4.995, ... -- the "large unexpected jumps" on
        # the dashboard, and the reason PnL/drawdown/strategy comparison could
        # not be trusted. See reports/demo-portfolio-jump-investigation.md.
        self.result_store.update_order_rows(lifecycle)

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
                # NOTE: only the ORDER's own lifecycle fields are refreshed on
                # a historical row. The portfolio snapshot columns
                # (inv_current / cash_current / locked+available / Portfolio
                # Wealth / Round Fee / fee scopes) are a per-round TIME SERIES
                # and must keep the values observed in the round that row
                # belongs to.
                #
                # They used to be rewritten here with the CURRENT snapshot
                # whenever an older order changed state, so an order opened in
                # round 2 and filled in round 9 stamped round 9's balances onto
                # round 2's row. Since this runs before the current round's row
                # is appended, every row it touched was a past round -- the
                # whole portfolio trajectory got scrambled (verified: engine
                # inventory rose a clean +0.999/round while the CSV recorded
                # 0.999, 7.992, 8.991, 4.995, ...). That is the "large
                # unexpected jumps" seen on the dashboard, and it made PnL,
                # drawdown and any strategy comparison meaningless.
                # See reports/demo-portfolio-jump-investigation.md.

    def _finish_demo_session(
        self,
        order_df: pd.DataFrame,
        *,
        reason: str,
        initiator: str,
        call_site: str,
    ) -> None:
        stamp = self.now_provider()
        events = self.exchange.cancel_all_open_orders(
            reason=reason,
            initiator=initiator,
            call_site=call_site,
            now=stamp,
        )
        ledger = self.exchange.ledger
        snapshot = ledger.snapshot(
            valuation_price=getattr(ledger, "valuation_price", 0.0)
        )
        scopes = ledger.fee_scopes()
        try:
            self._sync_demo_order_rows(
                order_df,
                changed_order_ids={event.order_id for event in events},
                snapshot=snapshot,
                scopes=scopes,
            )
            self._update_demo_result_orders(
                events,
                snapshot=snapshot,
                scopes=scopes,
            )
        except Exception as exc:
            print(json.dumps({
                "event": "demo_shutdown_result_update_error",
                "reason": reason,
                "session_id": str(self.session),
                "symbol": self.pair,
                "process_id": os.getpid(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "timestamp": stamp.isoformat(),
            }, sort_keys=True), flush=True)
        for event in events:
            self._safe_reporter_call("order_changed", ledger, event)

    def _safe_demo_flush(self) -> None:
        try:
            self.exchange.ledger.compact()
            self.result_store.export_bounded_csv()
        except Exception as exc:
            print(json.dumps({
                "event": "demo_final_flush_error",
                "session_id": str(self.session),
                "symbol": self.pair,
                "process_id": os.getpid(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "timestamp": self.now_provider().isoformat(),
            }, sort_keys=True), flush=True)

    def _run_demo(self) -> pd.DataFrame:
        csv_path = self.csv_path or os.path.join(RESULTS_DIR, "track_demo.csv")
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        self._ensure_demo_operations(csv_path)
        # The SQLite store is the source of truth and every lifecycle
        # back-fill goes there (_update_demo_result_orders). The in-memory
        # frame is only handed to _sync_demo_order_rows, which is a no-op on
        # an empty frame; reloading the whole store into it every round
        # decoded tens of MB of JSON per round on a long-lived store and
        # pushed the workers past their container memory limit.
        order_df = pd.DataFrame(columns=self.result_store.columns, dtype=object)
        self.result_store.export_bounded_csv()
        last_export = float(self.monotonic_provider())

        self._notify_startup()
        round_no = self.result_store.latest_round_no(str(self.session), self.pair)
        run_rounds = 0
        round_target = float(self.monotonic_provider())
        termination = None
        try:
            while self._within_horizon():
                reload_result = self.config_sync.reload_at_round_boundary()
                if not reload_result.ok:
                    self._record_config_reload_error(reload_result.error)
                else:
                    self._sync_strategy_with_config()
                run_rounds += 1
                round_no += 1
                if self.cfg.max_rounds and run_rounds > self.cfg.max_rounds:
                    break
                now = self.now_provider()
                retry_pending = getattr(self.telegram_reporter, "retry_pending", None)
                if retry_pending is not None:
                    self._safe_reporter_call("retry_pending", self.exchange.ledger)
                self._deliver_no_quote_decision(self.no_quote_monitor.pending_decision())
                try:
                    ob = self._get_orderbook()
                except Exception as exc:
                    print(f"Market data retrieval failed: {exc}")
                    round_target = self._wait_for_next_round(round_target)
                    continue
                if not ob:
                    round_target = self._wait_for_next_round(round_target)
                    continue
                try:
                    best_bid = float(ob["bids"][0][0])
                    best_ask = float(ob["asks"][0][0])
                    last_price = float(ob["lastTradePrice"])
                except (KeyError, IndexError, TypeError, ValueError):
                    round_target = self._wait_for_next_round(round_target)
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
                self._update_demo_result_orders(
                    lifecycle_events,
                    snapshot=snapshot,
                    scopes=scopes,
                    round_fee=round_fee,
                )
                for event in lifecycle_events:
                    self._safe_reporter_call(
                        "order_changed", self.exchange.ledger, event
                    )
                ledger.compact()

                diagnostic = self._algo1_diagnostic_document(
                    ob, snapshot.inventory, round_no=round_no
                )
                self._emit_algo1_diagnostic(
                    ob, snapshot.inventory, round_no=round_no, document=diagnostic
                )
                quote = self._decide_order(ob, snapshot.inventory)
                quote_status = "quote_generated" if quote is not None else "no_quote"
                no_quote_reason = (
                    None if quote is not None
                    else self._demo_no_quote_reason(ob, snapshot.inventory)
                )
                if quote is None:
                    no_quote_decision = self.no_quote_monitor.record_no_quote(
                        round_no,
                        now,
                        no_quote_reason,
                        self._no_quote_diagnostics(diagnostic),
                    )
                else:
                    no_quote_decision = self.no_quote_monitor.record_quote(round_no, now)
                self._deliver_no_quote_decision(no_quote_decision)
                no_quote_snapshot = self.no_quote_monitor.snapshot()
                bid_price = self._quote_side_price(quote, "bid")
                ask_price = self._quote_side_price(quote, "ask")
                bid_level = self._quote_side_level(quote, "bid level")
                ask_level = self._quote_side_level(quote, "ask level")

                buy_order = sell_order = None
                buy_reason = sell_reason = "no_quote" if quote is None else None
                replace_events = []
                paused_sides = self._trend_paused_sides((best_bid + best_ask) / 2.0, now.timestamp())
                if quote is not None:
                    resting_orders = [
                        order for order in ledger.active_orders.values()
                        if order.status in {"open", "partially_filled"}
                    ]
                    # One order per side: a new price outside the requote band
                    # replaces the resting order; a paused side rests nothing.
                    for side, price in (("sell", ask_price), ("buy", bid_price)):
                        if side in paused_sides:
                            doomed = [o for o in resting_orders if o.side == side]
                            replace_events += self._cancel_paper_orders(doomed, reason="trend_pause", now=now)
                        else:
                            doomed = self._paper_orders_outside_band(side, price, resting_orders)
                            replace_events += self._cancel_paper_orders(doomed, reason="replaced", now=now)
                    resting_orders = [
                        order for order in ledger.active_orders.values()
                        if order.status in {"open", "partially_filled"}
                    ]
                    resting_sells = [
                        (order.limit_price, order.remaining_amount)
                        for order in resting_orders if order.side == "sell"
                    ]
                    resting_buys = [
                        (order.limit_price, order.remaining_amount)
                        for order in resting_orders if order.side == "buy"
                    ]
                    buy_target, sell_target = self._inventory_sized_order_targets(
                        snapshot.inventory,
                        open_buy_amount=sum(
                            order.remaining_amount
                            for order in ledger.active_orders.values()
                            if order.side == "buy"
                        ),
                        open_sell_amount=snapshot.locked_inventory,
                    )
                    sell_amount = max(
                        0.0,
                        min(sell_target, snapshot.available_inventory - float(self.cfg.inv_min)),
                    )
                    sell_locked_reason = None
                    if ask_price is None:
                        # v2 quoted the bid side only (inventory low, or only
                        # the buy leg cleared the bar).
                        sell_reason = "one_sided_quote"
                    elif "sell" in paused_sides:
                        sell_reason = "trend_pause"
                    elif self._resting_order_blocks(ask_price, resting_sells):
                        sell_reason = "resting_order_kept"
                    elif sell_amount >= self._min_tradable_size() and (
                        sell_locked_reason := self._locked_inventory_guard_reason(
                            locked_sell_inventory=snapshot.locked_inventory,
                            new_order_amount=sell_amount,
                            inventory_base=snapshot.inventory,
                        )
                    ) is not None:
                        sell_reason = sell_locked_reason
                    elif sell_amount >= self._min_tradable_size():
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
                        else:
                            sell_reason = "rejected"
                    elif (
                        sell_target < self._min_tradable_size()
                        and snapshot.available_inventory - float(self.cfg.inv_min)
                        >= self._min_tradable_size()
                    ):
                        # There is stock free to sell, but the linear sizing
                        # rule scaled this side below the tradable minimum
                        # because inventory sits near the bottom of its band.
                        sell_reason = "inventory_sizing"
                    elif snapshot.locked_inventory > 0 and snapshot.inventory > float(self.cfg.inv_min):
                        # Holding stock, but every unit is already committed to
                        # resting sell orders. Reporting that as
                        # "insufficient_inventory" reads as "we hold nothing",
                        # which is the opposite of the truth and sends whoever
                        # is debugging looking for a phantom accounting bug.
                        sell_reason = "inventory_locked_in_open_orders"
                    else:
                        sell_reason = "insufficient_inventory"

                    snapshot = ledger.snapshot(valuation_price=last_price)
                    buy_locked_amount = sum(
                        order.remaining_amount for order in ledger.active_orders.values()
                        if order.side == "buy"
                    )
                    affordable = snapshot.available_cash / bid_price if bid_price and bid_price > 0 else 0.0
                    buy_amount = max(0.0, min(buy_target, affordable))
                    buy_locked_cash_reason = None
                    if bid_price is None:
                        # v2 quoted the ask side only (inventory high, or only
                        # the sell leg cleared the bar).
                        buy_reason = "one_sided_quote"
                    elif "buy" in paused_sides:
                        buy_reason = "trend_pause"
                    elif self._resting_order_blocks(bid_price, resting_buys):
                        buy_reason = "resting_order_kept"
                    elif buy_target < self._min_tradable_size():
                        # Inventory (plus resting buys) is at the top of its
                        # band, so the linear sizing rule scaled the buy below
                        # the tradable minimum. Not a cash shortage.
                        buy_reason = "inventory_sizing"
                    elif buy_amount < self._min_tradable_size():
                        buy_reason = "insufficient_cash"
                    elif buy_locked_amount + snapshot.inventory + buy_amount > self.cfg.inv_max:
                        buy_reason = "inventory_cap"
                    elif (buy_locked_cash_reason := self._locked_cash_guard_reason(
                        locked_buy_cash=snapshot.locked_cash,
                        new_order_cash=buy_amount * bid_price,
                        cash_base=snapshot.cash,
                    )) is not None:
                        buy_reason = buy_locked_cash_reason
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
                        else:
                            buy_reason = "rejected"

                snapshot = ledger.snapshot(valuation_price=last_price)
                scopes = ledger.fee_scopes()
                if replace_events:
                    self._sync_demo_order_rows(
                        order_df,
                        changed_order_ids={event.order_id for event in replace_events},
                        snapshot=snapshot,
                        scopes=scopes,
                        round_fee=round_fee,
                    )
                    self._update_demo_result_orders(
                        replace_events, snapshot=snapshot, scopes=scopes, round_fee=round_fee,
                    )
                    for event in replace_events:
                        self._safe_reporter_call("order_changed", ledger, event)
                round_message = self._format_round_message(
                    round_no,
                    now.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    best_bid,
                    best_ask,
                    last_price,
                    bid_price,
                    ask_price,
                    bid_level,
                    ask_level,
                    snapshot.inventory,
                    snapshot.cash,
                    snapshot.wealth,
                    int(buy_order is not None),
                    buy_order.original_amount if buy_order is not None else 0.0,
                    buy_order.limit_price if buy_order is not None else None,
                    buy_order.client_order_id if buy_order is not None else None,
                    int(sell_order is not None),
                    sell_order.original_amount if sell_order is not None else 0.0,
                    sell_order.limit_price if sell_order is not None else None,
                    sell_order.client_order_id if sell_order is not None else None,
                    round_fee,
                    scopes.pair_all_time,
                    scopes.all_symbols_all_time,
                    quote=quote,
                )
                round_parent_id = self._safe_reporter_call(
                    "round_summary", round_message
                )
                for submitted_order in (buy_order, sell_order):
                    if submitted_order is not None:
                        self._safe_reporter_call(
                            "order_submitted",
                            ledger,
                            submitted_order,
                            reply_to_message_id=round_parent_id,
                        )
                row = {
                    "Session_id": str(self.session),
                    "Algorithm": self.algorithm_id,
                    "Strategy": self._strategy_name(),
                    "Crypto": self.pair,
                    "Mode": "demo",
                    "Round": round_no,
                    "Timestamp": now.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "Quote Status": quote_status,
                    "No Quote Reason": no_quote_reason,
                    "No Quote Consecutive Count": no_quote_snapshot.consecutive_count,
                    "No Quote Episode Seconds": no_quote_snapshot.episode_seconds,
                    "No Quote Alert State": no_quote_snapshot.alert_state,
                    "No Quote Candidate Diagnostics": json.dumps(
                        no_quote_snapshot.diagnostics,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
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
                    "Mid Price": quote.get("mid price") if quote else None,
                    "Relative Quoted Spread": (
                        quote.get("relative quoted spread") if quote else None
                    ),
                    "Gross Return": quote.get("gross return") if quote else None,
                    "Net Return": quote.get("net return") if quote else None,
                    "Min Required Return": (
                        quote.get("min required return") if quote else None
                    ),
                    **self._quote_extra_fields(quote),
                    "Round Fee": round_fee,
                    "Session Pair Fee": scopes.session_pair,
                    "Session All Symbols Fee": scopes.session_all_symbols,
                    "Pair All Time Fee": scopes.pair_all_time,
                    "All Symbols All Time Fee": scopes.all_symbols_all_time,
                }
                row.update(self._demo_side_fields("buy", buy_order, buy_reason))
                row.update(self._demo_side_fields("sell", sell_order, sell_reason))
                self.result_store.append_round(row)
                self.result_store.compact(self.cfg.demo_result_retain_rows)
                # The CSV is a bounded export of the store (readers prefer
                # the SQLite file); rewriting it every round cost seconds of
                # CPU per round once the store held thousands of rows.
                interval = float(
                    getattr(self.cfg, "demo_csv_export_interval_seconds", 0.0) or 0.0
                )
                if interval <= 0 or float(self.monotonic_provider()) - last_export >= interval:
                    self.result_store.export_bounded_csv()
                    last_export = float(self.monotonic_provider())
                round_target = self._wait_for_next_round(round_target)
            termination = {
                "reason": "session_end",
                "initiator": "demo_engine",
                "call_site": "SimpleMarketMaker._run_demo.session_end",
            }
        except CoordinatedShutdown as exc:
            termination = {
                "reason": "coordinated_stack_shutdown",
                "initiator": f"signal:{exc.signum}",
                "call_site": "SimpleMarketMaker._run_demo.coordinated_signal",
            }
            print(f"Coordinated shutdown requested by signal {exc.signum}.")
        except KeyboardInterrupt as exc:
            # A module reload in an embedding process can create another
            # CoordinatedShutdown class identity. The explicit signal field,
            # not a generic KeyboardInterrupt, remains the shutdown proof.
            signum = getattr(exc, "signum", None)
            if signum is not None:
                termination = {
                    "reason": "coordinated_stack_shutdown",
                    "initiator": f"signal:{int(signum)}",
                    "call_site": "SimpleMarketMaker._run_demo.coordinated_signal",
                }
                print(f"Coordinated shutdown requested by signal {int(signum)}.")
            else:
                termination = {
                    "reason": "session_end",
                    "initiator": "interactive_keyboard_interrupt",
                    "call_site": "SimpleMarketMaker._run_demo.keyboard_interrupt",
                }
                print("Interrupted by user.")
        except Exception as exc:
            print(json.dumps({
                "event": "demo_worker_exit",
                "reason": "worker_fatal_exit",
                "initiator": "unhandled_exception",
                "call_site": "SimpleMarketMaker._run_demo",
                "session_id": str(self.session),
                "symbol": self.pair,
                "process_id": os.getpid(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "active_order_ids": sorted(self.exchange.ledger.active_orders),
                "timestamp": self.now_provider().isoformat(),
            }, sort_keys=True), flush=True)
            raise
        finally:
            if termination is not None:
                self._finish_demo_session(order_df, **termination)
            self._safe_demo_flush()
        return self.result_store.load_view()

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

        # Use a fresh exchange snapshot for each live session. The configured
        # profile's historical seeds must never become real-account capital.
        if self.mode_label != "demo":
            self._load_live_starting_balances()

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
            "Strategy",
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
            *self.QUOTE_EXTRA_COLUMNS,
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
                    bid_price = self._quote_side_price(res, "bid")
                    ask_price = self._quote_side_price(res, "ask")
                    bid_lvl = self._quote_side_level(res, "bid level")
                    ask_lvl = self._quote_side_level(res, "ask level")

                    # Current locked totals (and resting prices) from our open orders
                    buy_locked, sell_locked = self.get_open_order_totals()
                    resting = getattr(self, "_resting_orders", {"buy": [], "sell": []})
                    # One order per side (replacement) and the trend pause, as
                    # in the demo loop; a cancellation changes the locked totals.
                    paused_sides = self._trend_paused_sides((best_bid + best_ask) / 2.0, time.time())
                    replaced = set()
                    for side, price in (("sell", ask_price), ("buy", bid_price)):
                        band = self._requote_band(price)
                        for record in list(resting.get(side, [])):
                            if side in paused_sides or (price is not None and abs(float(record[0]) - float(price)) > band + 1e-9):
                                replaced |= self._cancel_live_order(record)
                    if replaced:
                        buy_locked, sell_locked = self.get_open_order_totals()
                        resting = getattr(self, "_resting_orders", {"buy": [], "sell": []})
                    if "sell" in paused_sides:
                        ask_price = None
                    if "buy" in paused_sides:
                        bid_price = None
                    buy_target, sell_target = self._inventory_sized_order_targets(
                        float(inv_current),
                        open_buy_amount=buy_locked['amount'],
                        open_sell_amount=sell_locked['amount'],
                    )

                    # SELL: cap by available (inventory minus already-locked sells)
                    sell_size = max(0.0, min(sell_target, (inv_current - sell_locked['amount']) - float(self.cfg.inv_min)))
                    if ask_price is None:
                        sell_size = 0.0  # one-sided quote: no ask this round
                    elif self._resting_order_blocks(ask_price, resting.get("sell", [])):
                        sell_size = 0.0  # an order of ours already rests at this price
                    if sell_size >= self._min_tradable_size() and self._locked_inventory_guard_reason(
                        locked_sell_inventory=sell_locked['amount'],
                        new_order_amount=sell_size,
                        inventory_base=inv_current,
                    ) is not None:
                        sell_size = 0.0  # blocked by the locked-inventory guard; do not submit
                    if sell_size >= self._min_tradable_size():
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
                            sell_size_value = float(sell_size)     # record size actually sent
                            ask_price_sent = ask_price
                            self.tracked_orders["sell"].add(sell_id)

                    # BUY: cap by available quote after buy-locked cash, and by inv_max
                    if bid_price and bid_price > 0:
                        buy_affordable_base = (cash_current - buy_locked['cash']) / bid_price
                    else:
                        buy_affordable_base = 0.0

                    buy_size = max(0.0, min(buy_target, buy_affordable_base))
                    if bid_price is None:
                        buy_size = 0.0  # one-sided quote: no bid this round
                    elif self._resting_order_blocks(bid_price, resting.get("buy", [])):
                        buy_size = 0.0  # an order of ours already rests at this price
                    if buy_size >= self._min_tradable_size() and self._locked_cash_guard_reason(
                        locked_buy_cash=buy_locked['cash'],
                        new_order_cash=buy_size * bid_price,
                        cash_base=cash_current,
                    ) is not None:
                        buy_size = 0.0  # blocked by the locked-cash guard; do not submit
                    if (buy_size >= self._min_tradable_size()) and (buy_locked['amount'] + inv_current + buy_size <= self.cfg.inv_max):
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
                            buy_size_value = float(buy_size)       # record size actually sent
                            bid_price_sent = bid_price
                            self.tracked_orders["buy"].add(buy_id)

                # ---- (E) Build the row: status cells start with ID or 'not_sent' ----
                buy_status_cell = buy_id if (buy_sent and buy_id) else "not_sent"
                sell_status_cell = sell_id if (sell_sent and sell_id) else "not_sent"
    
                new_row = [
                    self.session,
                    self.algorithm_id,
                    self._strategy_name(),
                    self.pair,
                    stamp, bid_lvl, ask_lvl,
                    inv_current, cash_current,
                    portfolio_wealth,
                    best_bid, best_ask, last_price,
                    bid_price_sent, ask_price_sent,
                    buy_size_value, sell_size_value,
                    buy_sent, sell_sent,
                    buy_status_cell, sell_status_cell,
                    *self._quote_extra_fields(res).values(),
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
