from __future__ import annotations

import pandas as pd
import numpy as np
import json
import requests
import time
from datetime import datetime, timedelta
import math
from sklearn.linear_model import LinearRegression
import pytz
import os
from decimal import Decimal, InvalidOperation, getcontext
from typing import Dict, Any, Iterable, Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry





getcontext().prec = 28


def _norm(sym: str) -> str:
    return (sym or "").strip().upper()


def _wallet_currency_for_quote(quote: str) -> str:
    """Market quote is typically IRT (for pairs like BTCIRT),
       while the wallet fiat code is RLS. Map accordingly."""
    q = _norm(quote)
    return "RLS" if q in ("IRT", "RLS", "IRR") else q


def _market_quote_for_wallet(wallet_ccy: str) -> str:
    """In markets, Rial is IRT. Map wallet RLS/IRR -> IRT for pairs."""
    c = _norm(wallet_ccy)
    return "IRT" if c in ("RLS", "IRT", "IRR") else c


def _symbol_to_pair(base: str, quote: str = "IRT") -> Optional[str]:
    """Build a market pair like BTCIRT from base + quote.
       Returns None if base is same fiat as quote (no pair)."""
    b = _norm(base)
    q = _market_quote_for_wallet(quote)
    if b in ("RLS", "IRT", "IRR") and q == "IRT":
        return None
    return f"{b}{q}"

def _build_retrying_session(token: str, user_agent: str, timeout: int = 15) -> requests.Session:
    """Session with retries + closed connections to avoid stale keep-alives."""
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Token {token}",
        "User-Agent": user_agent,
        "content-type": "application/json",
        "Connection": "close",
    })
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    # Attach default timeout to all requests
    orig = s.request
    def _req(method, url, **kw):
        return orig(method, url, timeout=kw.pop("timeout", timeout), **kw)
    s.request = _req  # type: ignore[assignment]
    return s

class Nobitex_portfolio:
    """Token-only Nobitex client focused on balances and portfolio value."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://apiv2.nobitex.ir",
        user_agent: str = "PortfolioBot/TokenOnly",
        timeout: int = 15
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = _build_retrying_session(token, user_agent=user_agent, timeout=timeout)

    # ---------- Low-level ----------
    def _get(self, path: str, **params) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = self.session.get(url, params=params or None)
        self._raise_for_api_errors(r)
        return r.json()

    def _post(self, path: str, **json_data) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = self.session.post(url, json=json_data or None)
        self._raise_for_api_errors(r)
        return r.json()

    @staticmethod
    def _raise_for_api_errors(r: requests.Response) -> None:
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            return
        if "status" in data and data["status"] not in ("ok", "success"):
            raise requests.HTTPError(f"Nobitex API error: {data}", response=r)

    # ---------- Balances ----------
    def get_balance(self, currency: str) -> Decimal:
        """Exact balance for a single currency (e.g., 'rls', 'btc')."""
        try:
            data = self._post("/users/wallets/balance", currency=currency.lower())
            return Decimal(str(data.get("balance", "0")))
        except (InvalidOperation, TypeError, requests.RequestException) as e:
            # Log and degrade gracefully
            print(f"[get_balance] error for {currency}: {e}")
            return Decimal("0")

    def get_balances(self, currencies: Iterable[str]) -> Dict[str, Decimal]:
        return { (c or "").strip().upper(): self.get_balance(c) for c in currencies }

    # ---------- Prices ----------
    def get_last_trade_price(self, pair: str) -> Optional[Decimal]:
        """Best-effort last trade price via /v2/trades/<PAIR>?limit=1."""
        try:
            data = self._get(f"/v2/trades/{pair}", limit=1)
            trades = data.get("trades") or data.get("items") or []
            if not trades:
                return None
            p = trades[0].get("price")
            return Decimal(str(p))
        except Exception as e:
            print(f"[get_last_trade_price] {pair} error: {e}")
            return None

    def get_mid_price_from_depth(self, pair: str) -> Optional[Decimal]:
        """Mid-price from top-of-book via /v2/depth/<PAIR>."""
        try:
            ob = self._get(f"/v2/depth/{pair}")
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if not bids or not asks:
                return None
            best_bid = Decimal(str(bids[0][0]))
            best_ask = Decimal(str(asks[0][0]))
            return (best_bid + best_ask) / Decimal("2")
        except Exception as e:
            print(f"[get_mid_price_from_depth] {pair} error: {e}")
            return None

    def get_best_price(self, pair: str) -> Decimal:
        """Best-effort price for any pair (e.g., 'BTCIRT')."""
        p = self.get_last_trade_price(pair)
        if p is None:
            p = self.get_mid_price_from_depth(pair)
        if p is None:
            raise RuntimeError(f"Could not fetch price for {pair}")
        return p

    # ---------- Portfolio summary (multi-coin) ----------
    def portfolio_snapshot_multi(
        self,
        coins: Iterable[str],
        *,
        quote: str = "IRT",
        include_cash: bool = True
    ) -> Dict[str, Any]:
        """
        Multi-coin snapshot valued in <quote> (default IRT).

        Returns:
          {
            'quote': 'IRT',
            'cash_wallet_ccy': 'RLS',
            'cash_balance': Decimal(...),
            'assets': {  # dict keyed by lowercase symbol
                'btc': {
                    'symbol': 'BTC',
                    'pair': 'BTCIRT',
                    'balance': Decimal(...),
                    'price_irt': Decimal(...),
                    'value_irt': Decimal(...),
                    'price_available': True,
                },
                ...
            },
            'assets_list': [...],  # optional list format
            'total_value_irt': Decimal(...),
          }
        """
        def _symkey(s: str) -> str:
            return (s or "").strip().lower()

        coins_upper: List[str] = [_norm(s) for s in coins]
        quote_wallet_ccy = _wallet_currency_for_quote(quote)  # e.g., IRT -> RLS
        quote_mkt = _market_quote_for_wallet(quote).lower()   # e.g., "irt"

        # Fetch balances (assets) and optional cash (fiat)
        unique_wallet_ccys = set(coins_upper)
        if include_cash:
            unique_wallet_ccys.add(quote_wallet_ccy)

        balances: Dict[str, Decimal] = {ccy: self.get_balance(ccy) for ccy in unique_wallet_ccys}

        # Prices per base
        price_map: Dict[str, Optional[Decimal]] = {}
        for base in coins_upper:
            pair = _symbol_to_pair(base, quote)
            if pair is None:
                price_map[base] = Decimal("1")  # valuing fiat in its own quote
                continue
            try:
                price_map[base] = self.get_best_price(pair)
            except Exception:
                price_map[base] = None

        # Build assets dict + list
        assets: Dict[str, Dict[str, Any]] = {}
        assets_list: List[Dict[str, Any]] = []
        total_value = Decimal("0")

        for base in coins_upper:
            key = _symkey(base)
            bal = balances.get(base, Decimal("0"))
            price = price_map.get(base)
            pair = _symbol_to_pair(base, quote)

            value = None
            if price is not None:
                try:
                    value = bal * price
                    total_value += value
                except (InvalidOperation, TypeError):
                    value = None

            row = {
                "symbol": base,
                "pair": pair,
                "balance": bal,
                f"price_{quote_mkt}": price,
                f"value_{quote_mkt}": value,
                "price_available": price is not None,
            }
            assets[key] = row
            assets_list.append(row)

        # Include cash (fiat) if requested
        cash_balance = balances.get(quote_wallet_ccy, Decimal("0"))
        if include_cash:
            total_value += cash_balance

        return {
            "quote": _market_quote_for_wallet(quote),
            "cash_wallet_ccy": quote_wallet_ccy,
            "cash_balance": cash_balance,
            "assets": assets,            # dict keyed by 'btc', 'usdt', ...
            "assets_list": assets_list,  # optional legacy list
            f"total_value_{quote_mkt}": total_value,
        }

    # ---------- Printer ----------
    def print_portfolio(
        self,
        coins: Optional[Iterable[str]] = None,
        *,
        quote: str = "IRT"
    ) -> Dict[str, Any]:
        """
        Always returns the multi-coin snapshot.
        If coins is None, defaults to ['btc'] for convenience.
        """
        coins = list(coins) if coins else ["btc"]
        return self.portfolio_snapshot_multi(coins, quote=quote, include_cash=True)


'''
# ---------------------- Example usage ----------------------
if __name__ == "__main__":
    client = Nobitex_portfolio(TOKEN)

    # === Multi-coin: pass the desired coins and get wallet info + values in IRT ===
    desired_coins = ["btc", "usdt", "eth"]  # customize as you like
    info = client.print_portfolio(coins=desired_coins, quote="IRT")
    print(info)

    '''