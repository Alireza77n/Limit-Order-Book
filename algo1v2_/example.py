"""Run Algorithm 1 v2 on the example book of the specification.

    python example.py
"""
from Algo1v2 import Algo1Strategy
from Config import MMConfig, PROFILES


def example_book():
    bids = [[2_659_990, "20000"], [2_659_900, "300"], [2_658_000, "1500"], [2_655_000, "8000"],
            [2_650_000, "40000"], [2_640_000, "10000"], [2_630_000, "5000"], [2_620_000, "5000"],
            [2_610_000, "5000"], [2_600_000, "5000"], [2_590_000, "5000"]]
    asks = [[2_660_010, "5000"], [2_660_100, "400"], [2_662_000, "2000"], [2_665_000, "6000"],
            [2_670_000, "30000"], [2_680_000, "12000"], [2_690_000, "5000"], [2_700_000, "5000"],
            [2_710_000, "5000"], [2_720_000, "5000"], [2_730_000, "5000"]]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2660000"}


if __name__ == "__main__":
    cfg = MMConfig(**PROFILES["usdt"], skew_perc=0.0)
    strategy = Algo1Strategy(cfg)
    for inventory in (25.0, 48.0, 2.0):
        strategy.reset_memory()
        quote = strategy.decide_order(example_book(), inventory)
        print(f"inventory {inventory:>5}: ", {k: quote[k] for k in (
            "quote mode", "bid", "ask", "bid level", "ask level", "net return", "expected value")})
    document = strategy.diagnostics(example_book(), 25.0, round_no=1, session_id="example")
    print("pairs scored:", len(document["candidate_results"]), "| reason:", document["selection_reason"])
