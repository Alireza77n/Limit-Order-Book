"""Regression tests for the incident in
reports/batch-fill-definitive-root-cause.md, kept for Algorithm 1 v2.

Historical incident: at zero inventory the version-1 scan fell back to an
untested (depth_max, depth_max) pair and rested a level-10 BUY that
accumulated with near-identical orders and filled together. The fix at the
time was to return no quote instead of the untested pair. Version 2 has no
level scan and no fallback pair at all; what must still hold is:

* every quote that is returned clears the fee bar on the exact prices sent;
* zero inventory is a bid-only situation, never a two-sided one;
* the same price is not re-posted every round (hysteresis + the engine's
  resting-order guard), which is what let the orders accumulate.
"""

from datetime import datetime, timedelta, timezone

from config import MMConfig
from strategy.algo1 import Algo1Strategy


def _usdt_cfg(**overrides):
    base = dict(
        depth_max=10, inv_min=0.0, inv_max=50.0,
        commission_perc=0.0010, exp_ret_min=0.0010, pair="USDTIRT",
        skew_perc=0.0,
    )
    base.update(overrides)
    return MMConfig(**base)


def _narrow_book():
    """The incident's book shape: 200-IRT bid steps, 500-IRT ask steps."""
    bids = [[str(2_000_000 - 200 * i), "1.0"] for i in range(11)]
    asks = [[str(2_000_020 + 500 * i), "1.0"] for i in range(11)]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2000010"}


def test_zero_inventory_never_produces_a_two_sided_quote():
    strategy = Algo1Strategy(_usdt_cfg())
    quote = strategy.decide_order(_narrow_book(), 0.0)
    assert quote is None or quote["quote mode"] == "bid_only"
    if quote is not None:
        assert quote["ask"] is None
        # a lone bid clears the bar against the centre on its own
        reference = quote["reference price"]
        assert (1 - 0.001) ** 2 * reference / quote["bid"] - 1 > 0.001


def test_a_returned_pair_always_clears_the_bar_on_the_submitted_prices():
    strategy = Algo1Strategy(_usdt_cfg())
    for inventory in (10.0, 25.0, 40.0):
        quote = strategy.decide_order(_narrow_book(), inventory)
        strategy.reset_memory()
        if quote is not None and quote["quote mode"] == "pair":
            assert strategy.net_return(quote["bid"], quote["ask"]) > strategy.min_required_return()


def test_no_quote_has_an_explicit_reason():
    strategy = Algo1Strategy(_usdt_cfg(commission_perc=0.2, exp_ret_min=0.2))
    assert strategy.decide_order(_narrow_book(), 0.0) is None
    reason = strategy.no_quote_reason(_narrow_book(), 0.0)
    assert reason in {"no_side_clears_bar", "expected_value_below_floor"}
    diagnostic = strategy.diagnostics(_narrow_book(), 0.0, round_no=1, session_id="regression")
    assert diagnostic["fallback_detected"] is False
    assert diagnostic["selected_bid_level"] is None
    assert diagnostic["selected_ask_level"] is None
    assert diagnostic["selection_reason"] == reason


def test_wide_book_quotes_near_the_touch_not_at_the_deepest_level():
    """Positive control: a wide, profitable book gets a tight quote, not the
    deepest level the old scan could reach."""
    strategy = Algo1Strategy(_usdt_cfg())
    bids = [[str(2_000_000 - 1_000 * i), "1.0"] for i in range(11)]
    asks = [[str(2_000_020 + 1_000 * i), "1.0"] for i in range(11)]
    ob = {"bids": bids, "asks": asks, "lastTradePrice": "2000010"}
    quote = strategy.decide_order(ob, 25.0)
    assert quote is not None and quote["quote mode"] == "pair"
    assert quote["bid level"] is not None and 1 <= quote["bid level"] <= 8
    assert quote["ask level"] is not None and 1 <= quote["ask level"] <= 8


def test_same_price_is_not_re_quoted_every_round():
    """The accumulation mechanism: with hysteresis on, the second round
    returns the resting price marked as kept, so the engine's guard can
    skip the re-post."""
    clock = {"t": 0.0}
    strategy = Algo1Strategy(_usdt_cfg(hysteresis_ticks=5, refresh_seconds=600.0), clock=lambda: clock["t"])
    first = strategy.decide_order(_narrow_book(), 25.0)
    clock["t"] = 10.0
    second = strategy.decide_order(_narrow_book(), 25.0)
    assert first is not None and second is not None
    assert second["bid"] == first["bid"] and second["bid kept"] is True
    assert second["ask"] == first["ask"] and second["ask kept"] is True


def test_level1_order_still_fast_fills_when_the_matcher_option_is_on():
    """paper.py's level1_fast_fill is untouched by the strategy change; it is
    now off in the profiles (v2 quotes by distance, so a level index says
    nothing about the touch) but still works when enabled explicitly."""
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
