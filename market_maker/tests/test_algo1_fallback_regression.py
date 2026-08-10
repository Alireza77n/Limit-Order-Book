"""Regression tests for the root-cause fix in
reports/batch-fill-definitive-root-cause.md.

Historical incident: at low/zero inventory, if the loosest inventory-skewed
candidate pair failed `spread_ok`, `algo1_bid_ask_prices` used to fall back
silently to the untested, unskewed `(depth_max, depth_max)` pair -- and
return it as an ordinary quote whenever that pair happened to be profitable.
In the recorded incident this produced a zero-inventory BUY quote at level
10 (the loosest level in the book), which could never qualify for
`level1_fast_fill` (`selected_level == 1` is a strict check), so it rested
until price genuinely moved to it -- by which time several near-identical
orders had accumulated and filled together.

The fix: if the scan never assigns a level (whether `leg_pairs()` is empty
or its loosest candidate fails immediately), `algo1_bid_ask_prices` and
`decide_order` return `None` instead of the untested fallback -- a real
`no_quote` round, with the explicit reason
`no_profitable_inventory_skewed_candidate` from `no_quote_reason()`.
"""

from config import MMConfig
from strategy.algo1 import Algo1Strategy


def _usdt_cfg():
    return MMConfig(
        depth_max=10, inv_min=0.0, inv_max=50.0,
        commission_perc=0.0010, exp_ret_min=0.0010, pair="USDTIRT",
    )


def _narrow_book():
    """Book where the loosest d_q=-5 skewed candidate (5,10) fails, but the
    unskewed (10,10) fallback -- never a real candidate at d_q=-5 -- would
    have passed. Verified against the actual historical mechanism: this is
    exactly the shape that produced the recorded level-10 BUY quote."""
    bids = [[str(2_000_000 - 200 * i), "1.0"] for i in range(11)]
    asks = [[str(2_000_020 + 200 * i), "1.0"] for i in range(11)]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2000010"}


def test_first_candidate_failure_no_longer_falls_back_silently():
    """Prove the trap is real for this book/config before checking the fix:
    the loosest actual skewed candidate (5,10) fails, while the old fallback
    pair (10,10) -- never itself a member of leg_pairs(-5, 10) -- would have
    passed. This is the exact condition the root-cause fix closes."""
    strategy = Algo1Strategy(_usdt_cfg())
    ob = _narrow_book()

    assert strategy.leg_pairs(-5, 10) == [(5, 10), (4, 9), (3, 8), (2, 7), (1, 6)]
    assert strategy.spread_ok(ob, 5, 10) is False
    assert strategy.spread_ok(ob, 10, 10) is True


def test_zero_inventory_first_candidate_failure_returns_no_quote():
    """The actual fix: decide_order must return None here, not a level-10
    quote, now that the untested fallback is never used."""
    strategy = Algo1Strategy(_usdt_cfg())
    ob = _narrow_book()

    assert strategy.algo1_bid_ask_prices(ob, 0.0) is None
    assert strategy.decide_order(ob, 0.0) is None


def test_zero_inventory_first_candidate_failure_has_explicit_reason():
    strategy = Algo1Strategy(_usdt_cfg())
    ob = _narrow_book()

    assert strategy.no_quote_reason(ob, 0.0) == "no_profitable_inventory_skewed_candidate"

    diagnostic = strategy.diagnostics(ob, 0.0, round_no=1, session_id="regression")
    assert diagnostic["fallback_detected"] is True
    assert diagnostic["selected_bid_level"] is None
    assert diagnostic["selected_ask_level"] is None


def test_normal_zero_inventory_case_still_reaches_level_one():
    """Positive control: when the book is wide enough that every candidate
    from (5,10) down to (1,6) clears the profitability bar, the scan must
    still walk all the way to the tightest one -- level 1 -- exactly as
    before the fix. The fix only removes the untested fallback; it must not
    touch a scan that actually completes."""
    strategy = Algo1Strategy(_usdt_cfg())
    bids = [[str(2_000_000 - 1_000 * i), "1.0"] for i in range(11)]
    asks = [[str(2_000_020 + 1_000 * i), "1.0"] for i in range(11)]
    ob = {"bids": bids, "asks": asks, "lastTradePrice": "2000010"}

    quote = strategy.decide_order(ob, 0.0)
    assert quote is not None
    assert quote["bid level"] == 1
    assert quote["ask level"] == 6


def test_normal_scan_breaking_partway_still_selects_last_feasible_pair():
    """Positive control for the general case (not the trap): the scan should
    still stop at the last feasible pair and return it, unaffected by the
    fix, whenever at least one candidate passes."""
    strategy = Algo1Strategy(MMConfig(
        depth_max=10, inv_min=0.0, inv_max=1.0,
        commission_perc=0.05, exp_ret_min=0.05, pair="BTCUSDT",
    ))
    bids = [[str(100 - i), "1.0"] for i in range(11)]
    asks = [[str(101 + i), "1.0"] for i in range(11)]
    ob = {"bids": bids, "asks": asks, "lastTradePrice": "100"}

    quote = strategy.decide_order(ob, 0.5)
    assert quote is not None
    assert quote["bid level"] == 5
    assert quote["ask level"] == 5


def test_level1_order_still_fast_fills_after_the_fix():
    """The fix touches only level SELECTION (algo1.py). The Demo matcher's
    level1_fast_fill mechanism (paper.py) is untouched and must still fire
    for a genuine level-1 order on the very next market observation."""
    from datetime import datetime, timedelta, timezone

    from paper import PaperLedger

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ledger = PaperLedger(
        session_id="s", symbol="USDTIRT", initial_inventory=0.0,
        initial_cash=1_000_000.0, commission_perc=0.001, valuation_price=100.0,
        level1_fast_fill_enabled=True, level1_fill_delay_seconds=0.0,
        level1_fill_fraction=1.0,
    )
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=1.0, selected_level=1,
        client_order_id="buy-1", now=t0,
    )
    assert ledger.get_order("buy-1").status == "open"

    events = ledger.process_market(
        best_bid=90.0, best_ask=110.0, last_price=100.0, now=t0 + timedelta(seconds=10),
    )

    assert len(events) == 1
    assert events[0].reason == "level1_fast_fill"
    assert ledger.get_order("buy-1").status == "filled"
