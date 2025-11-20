import requests
from typing import Optional, Dict, Any, Tuple, List, Set  # add Set

from datetime import datetime, timedelta
import time
import pytz
from datetime import datetime, timedelta
import pandas as pd



# Order Status Check class
class OrderStatusChecker:
    def __init__(self, token: str, base_url: str = "https://apiv2.nobitex.ir", timeout: int = 20):
        self.token = token
        self.base_url = base_url
        self.timeout = timeout

    # In Order_Statuse.py (or wherever this function lives)

    def get_order_list_status(
        self,
        status: str = "open",
        details: int = 2,            # keep 2 to get richer fields
        page_size: int = 100,
        max_pages: int = 1,
    ) -> List[Dict]:
        headers = {"Authorization": f"Token {self.token}", "User-Agent": "TraderBot/Example"}
        params = {"status": status, "details": details, "page": 1, "pageSize": page_size}
        out: List[Dict] = []
    
        for _ in range(max_pages):
            try:
                r = requests.get(f"{self.base_url}/market/orders/list",
                                 headers=headers, params=params, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                if data.get("status") != "ok":
                    raise Exception(f"API error: {data}")
    
                for o in data.get("orders", []):
                    # Raw fields (API may vary slightly; use .get with fallbacks)
                    oid   = o.get("id")
                    st    = o.get("status") or o.get("state")
                    c_at  = o.get("created_at") or o.get("createdAt")
                    exec_ = o.get("execution") or o.get("executionType")  # "Limit", etc.
                    side  = o.get("type") or o.get("side")                # "buy"/"sell"
                    clid  = o.get("clientOrderId") or o.get("client_id") or o.get("clientOrderID")
                    price = o.get("price")
                    amt   = o.get("amount")
                    filled= o.get("matchedAmount") or o.get("filledAmount")
                    remain= o.get("unmatchedAmount") or o.get("remainingAmount")
                    mkt   = o.get("market") or o.get("pair")
    
                    # Normalize numeric (best-effort)
                    def _f(x):
                        try:
                            return float(x)
                        except Exception:
                            return None
    
                    # Normalize pair: e.g., "BTC-RLS" -> "BTCIRT", keep "BTCUSDT" as-is
                    pair_norm = None
                    if mkt:
                        s = str(mkt).upper().replace("-", "")
                        pair_norm = s.replace("RLS", "IRT").replace("IRR", "IRT")
    
                    out.append({
                        "id": oid,
                        "status": st,
                        "created_at": c_at,
                        "execution": exec_,
                        "type": (str(side).lower() if side else None),        # "buy" / "sell"
                        "clientOrderId": clid,
                        "price": _f(price),
                        "amount": _f(amt),
                        "matchedAmount": _f(filled),
                        "unmatchedAmount": _f(remain),
                        "market": mkt,
                        "pair": pair_norm,  # normalized (e.g., BTCIRT)
                        "_raw": o,          # keep raw for debugging if you want
                    })
    
                if len(data.get("orders", [])) < page_size:
                    break
                params["page"] += 1
    
            except Exception as e:
                print(f"Error fetching order status: {e}")
                break
    
        return out


    def get_order_done_time(self, order_id: int) -> Optional[str]:
        headers = {"Authorization": f"Token {self.token}", "User-Agent": "TraderBot/Example"}
        params = {"orderId": order_id}
        try:
            r = requests.get(f"{self.base_url}/market/trades/list", headers=headers, params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "ok":
                raise Exception(f"API error: {data}")
            for trade in data.get("trades", []):
                if trade.get("orderId") == order_id:
                    return trade.get("timestamp")
            return None
        except Exception as e:
            print(f"Error fetching trade details: {e}")
            return None

# Order Cancel class
class OrderCanceller:
    def __init__(self, token: str, base_url: str = "https://apiv2.nobitex.ir", timeout: int = 10):
        self.token = token
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {self.token}",
            "User-Agent": "TraderBot/CancelDemo/1.0",
            "content-type": "application/json",
        })


    def cancel_all_open_orders(self, max_pages: int = 10) -> None:
        """
        Fetch all OPEN orders and cancel them one by one.
        Uses /market/orders/list then /market/orders/update-status.
        """
        checker = OrderStatusChecker(self.token, base_url=self.base_url, timeout=self.timeout)
        orders = checker.get_order_list_status(status="open", max_pages=max_pages)
        if not orders:
            print("No open orders to cancel.")
            return

        print(f"Cancelling {len(orders)} open orders...")
        for o in orders:
            oid = o.get("id")
            if not oid:
                continue
            try:
                resp = self.cancel_order(oid)  # already posts {"order": oid, "status": "canceled"}
                ok = isinstance(resp, dict) and (resp.get("status") in ("ok", "success"))
                print(f"  - {oid}: {'OK' if ok else resp}")
            except Exception as e:
                print(f"  - {oid}: error -> {e}")

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        payload = {"status": "canceled", "order": order_id}
        try:
            r = self.session.post(f"{self.base_url}/market/orders/update-status", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"Error cancelling order {order_id}: {e}")
            return {}

   

    def cancel_old_orders(
        self,
        max_age_minutes: float,
        df: Optional[pd.DataFrame] = None,
        tracked_orders: Optional[Dict[str, Set[str]]] = None
    ) -> None:
        """
        Cancel all open orders older than max_age_minutes.
        If a DataFrame is provided, update its Buy/Sell Status cells.
        If tracked_orders dict is provided, update it too.
        """
        orders = OrderStatusChecker(self.token).get_order_list_status(status="open")
        current_time = datetime.now(pytz.utc)
    
        cancelled_ids = []  # collect clientOrderIds
    
        for order in orders:
            try:
                created_at = datetime.strptime(order["created_at"], "%Y-%m-%dT%H:%M:%S.%f%z")
                age_minutes = (current_time - created_at).total_seconds() / 60.0
    
                if age_minutes > max_age_minutes:
                    print(f"Order {order['id']} older than {max_age_minutes} minutes. Cancelling.")
                    result = self.cancel_order(order["id"])
                    if result.get("status") == "ok":
                        clid = result["order"].get("clientOrderId")
                        if clid:
                            cancelled_ids.append(clid)
    
            except ValueError as e:
                print(f"Error parsing timestamp for order {order['id']}: {e}")
    
        print("cancelled_ids:", cancelled_ids)
    
        # --- Update DataFrame if provided ---
        if df is not None and cancelled_ids:
            for clid in cancelled_ids:
                mask_buy = df["Buy Status"] == clid
                df.loc[mask_buy, "Buy Status"] = "cancelled"
    
                mask_sell = df["Sell Status"] == clid
                df.loc[mask_sell, "Sell Status"] = "cancelled"
    
        # --- Update tracked_orders if provided ---
        if tracked_orders is not None and cancelled_ids:
            tracked_orders["buy"] -= set(cancelled_ids)
            tracked_orders["sell"] -= set(cancelled_ids)

        return tracked_orders    

            
