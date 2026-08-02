"""Regression tests for the batch-fill defect.

Before this, PaperLedger.process_market() filled EVERY eligible resting
order to its full remaining amount from a single market observation, with
no shared volume budget. In real recorded data that made 8 buy orders fill
at one instant, moving inventory 0 -> 7.354113 USDT in a single round; see
reports/demo-portfolio-jump-investigation.md for the traced timeline.

These tests pin the corrected behavior: one observation carries a finite,
per-side liquidity budget, allocated in price-time priority.
"""

from datetime import datetime, timedelta, timezone

import pytest

from paper import PaperLedger

T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def make_ledger(**overrides):
    values = {
        "session_id": "shared-session",
        "symbol": "USDTIRT",
        "initial_inventory": 100.0,
        "initial_cash": 100_000.0,
        "commission_perc": 0.0,
        "valuation_price": 100.0,
        "level1_fast_fill_enabled": False,
        "level1_fill_delay_seconds": 0.0,
        "level1_fill_fraction": 1.0,
        "queue_enabled": False,
        "max_fill_per_observation": 1.0,
    }
    values.update(overrides)
    return PaperLedger(**values)


def _submit_buys(ledger, count, *, price=90.0, amount=1.0, start=T0):
    for i in range(count):
        ledger.submit_order(
            side="buy", limit_price=price, amount=amount, selected_level=3,
            client_order_id=f"buy-{i}", now=start + timedelta(seconds=i),
        )


def test_one_observation_cannot_fill_every_resting_order():
    """THE regression test: 8 resting buys at the same price, one touch.

    Old behavior filled all 8 completely (8.0 inventory). With a budget of
    one order_size, only ~1.0 may fill.
    """
    ledger = make_ledger()
    _submit_buys(ledger, 8)

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        now=T0 + timedelta(seconds=30),
    )

    assert sum(e.new_fill for e in events) == pytest.approx(1.0)
    assert ledger.inventory == pytest.approx(101.0)  # 100.0 starting + 1.0 filled
    assert len(ledger.active_orders) == 7  # the rest are still resting


def test_repeated_observations_drain_the_book_gradually():
    """Fills still happen -- they are paced, not disabled."""
    ledger = make_ledger()
    _submit_buys(ledger, 4)

    for i in range(4):
        ledger.process_market(
            best_bid=89.0, best_ask=90.0, last_price=90.0,
            now=T0 + timedelta(seconds=60 + i),
        )

    assert ledger.inventory == pytest.approx(104.0)  # 100.0 starting + 4.0 filled
    assert ledger.active_orders == {}


def test_budget_is_per_side_not_shared_between_buy_and_sell():
    """A resting buy fills against incoming sell flow and vice versa, so
    the two sides draw on separate liquidity, not one shared pool."""
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=1.0, selected_level=3,
        client_order_id="b1", now=T0,
    )
    ledger.submit_order(
        side="sell", limit_price=100.0, amount=1.0, selected_level=3,
        client_order_id="s1", now=T0,
    )

    events = ledger.process_market(
        best_bid=100.0, best_ask=100.0, last_price=100.0,
        now=T0 + timedelta(seconds=1),
    )

    assert {e.order_id for e in events} == {"b1", "s1"}
    assert ledger.get_order("b1").status == "filled"
    assert ledger.get_order("s1").status == "filled"


def test_fill_allocated_in_price_time_priority():
    """Best price first; among equal prices, oldest first."""
    ledger = make_ledger(max_fill_per_observation=1.0)
    # Worst price, but submitted first.
    ledger.submit_order(side="buy", limit_price=88.0, amount=1.0, selected_level=3,
                        client_order_id="worst-price", now=T0)
    # Best price, submitted last -- must still win.
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=3,
                        client_order_id="best-price", now=T0 + timedelta(seconds=5))

    events = ledger.process_market(
        best_bid=87.0, best_ask=88.0, last_price=88.0,
        now=T0 + timedelta(seconds=10),
    )

    assert [e.order_id for e in events] == ["best-price"]
    assert ledger.get_order("worst-price").status == "open"


def test_equal_price_fills_oldest_first():
    ledger = make_ledger(max_fill_per_observation=1.0)
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=3,
                        client_order_id="older", now=T0)
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=3,
                        client_order_id="newer", now=T0 + timedelta(seconds=5))

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        now=T0 + timedelta(seconds=10),
    )

    assert [e.order_id for e in events] == ["older"]


def test_traded_volume_is_the_budget_when_reported():
    """An explicit traded volume overrides the configured fallback."""
    ledger = make_ledger(max_fill_per_observation=10.0)
    _submit_buys(ledger, 5)

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        trade_price=90.0, traded_volume=2.0,
        now=T0 + timedelta(seconds=30),
    )

    assert sum(e.new_fill for e in events) == pytest.approx(2.0)


def test_queue_volume_is_consumed_across_orders_not_reset_per_order():
    """Latent defect: `volume` used to be re-read from traded_volume inside
    the per-order loop, so every order independently received the full
    traded volume even with the queue enabled."""
    ledger = make_ledger(queue_enabled=True, max_fill_per_observation=10.0)
    for i in range(3):
        ledger.submit_order(
            side="buy", limit_price=90.0, amount=1.0, selected_level=3,
            client_order_id=f"q{i}", now=T0 + timedelta(seconds=i),
            queue_ahead=0.0,
        )

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        trade_price=90.0, traded_volume=1.5,
        now=T0 + timedelta(seconds=30),
    )

    assert sum(e.new_fill for e in events) == pytest.approx(1.5)


def test_partial_fill_when_budget_smaller_than_order():
    ledger = make_ledger(max_fill_per_observation=0.4)
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=3,
                        client_order_id="partial", now=T0)

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        now=T0 + timedelta(seconds=1),
    )

    assert events[0].new_fill == pytest.approx(0.4)
    assert ledger.get_order("partial").status == "partially_filled"
    assert ledger.get_order("partial").remaining_amount == pytest.approx(0.6)


def test_level1_fast_fill_draws_from_the_same_budget():
    """Level-1 is served first (front of queue) but does not get free size
    on top of the budget."""
    ledger = make_ledger(level1_fast_fill_enabled=True, max_fill_per_observation=1.0)
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=1,
                        client_order_id="l1", now=T0)
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=3,
                        client_order_id="deep", now=T0)

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        now=T0 + timedelta(seconds=1),
    )

    assert sum(e.new_fill for e in events) == pytest.approx(1.0)
    assert ledger.get_order("l1").status == "filled"
    assert ledger.get_order("deep").status == "open"


def test_unbounded_when_not_configured_preserves_legacy_behavior():
    """A directly constructed ledger with no budget keeps the original
    semantics -- the budget is opt-in, supplied by run.py in Demo mode."""
    ledger = make_ledger(max_fill_per_observation=None)
    _submit_buys(ledger, 3)

    events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        now=T0 + timedelta(seconds=30),
    )

    assert sum(e.new_fill for e in events) == pytest.approx(3.0)


def test_budget_survives_persist_and_reload(tmp_path):
    state = tmp_path / "ledger.json"
    ledger = make_ledger(max_fill_per_observation=0.25, state_path=state)
    ledger.submit_order(side="buy", limit_price=90.0, amount=1.0, selected_level=3,
                        client_order_id="b", now=T0)

    reloaded = PaperLedger.load(state)

    assert reloaded.max_fill_per_observation == pytest.approx(0.25)


def test_legacy_ledger_without_budget_field_loads(tmp_path):
    """Ledgers persisted before this field existed must still load."""
    import json
    state = tmp_path / "legacy.json"
    ledger = make_ledger(max_fill_per_observation=1.0, state_path=state)
    ledger._persist()
    document = json.loads(state.read_text())
    del document["config"]["max_fill_per_observation"]
    state.write_text(json.dumps(document))

    reloaded = PaperLedger.load(state)

    assert reloaded.max_fill_per_observation == pytest.approx(1.0)


def test_invalid_budget_rejected():
    with pytest.raises(ValueError):
        make_ledger(max_fill_per_observation=0.0)
