from datetime import datetime, timedelta, timezone

import pytest

from paper import PaperLedger


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def make_ledger(**overrides):
    values = {
        "session_id": "shared-session",
        "symbol": "USDTIRT",
        "initial_inventory": 10.0,
        "initial_cash": 1_000.0,
        "commission_perc": 0.01,
        "valuation_price": 100.0,
        "level1_fast_fill_enabled": True,
        "level1_fill_delay_seconds": 2.0,
        "level1_fill_fraction": 1.0,
        "queue_enabled": False,
    }
    values.update(overrides)
    return PaperLedger(**values)


def test_new_buy_order_is_open_and_locks_cash_without_changing_balances():
    ledger = make_ledger()

    order = ledger.submit_order(
        side="buy", limit_price=90.0, amount=2.0, selected_level=3,
        client_order_id="buy-1", now=T0,
    )

    assert order.status == "open"
    assert order.matched_amount == 0
    assert order.processed_matched_amount == 0
    assert order.remaining_amount == 2.0
    assert order.session_id == "shared-session"
    assert order.symbol == "USDTIRT"
    assert order.selected_level == 3
    snapshot = ledger.snapshot()
    assert snapshot.inventory == 10.0
    assert snapshot.cash == 1_000.0
    assert snapshot.locked_cash == 180.0
    assert snapshot.available_cash == 820.0
    assert snapshot.fees_quote == 0.0


def test_new_sell_order_locks_inventory_and_symbol_state_is_local():
    ledger = make_ledger()

    order = ledger.submit_order(
        side="sell", limit_price=110.0, amount=4.0, selected_level=2,
        client_order_id="sell-1", now=T0,
    )

    assert order.status == "open"
    snapshot = ledger.snapshot()
    assert snapshot.locked_inventory == 4.0
    assert snapshot.available_inventory == 6.0
    assert snapshot.cash == 1_000.0


def test_orders_exceeding_available_balance_are_rejected_without_locks():
    ledger = make_ledger(initial_inventory=1.0, initial_cash=50.0)

    buy = ledger.submit_order(
        side="buy", limit_price=100.0, amount=1.0, selected_level=2,
        client_order_id="buy-rejected", now=T0,
    )
    sell = ledger.submit_order(
        side="sell", limit_price=100.0, amount=2.0, selected_level=2,
        client_order_id="sell-rejected", now=T0,
    )

    assert buy.status == "rejected"
    assert sell.status == "rejected"
    assert ledger.active_orders == {}
    assert set(ledger.order_history) == {"buy-rejected", "sell-rejected"}
    assert ledger.snapshot().locked_cash == 0
    assert ledger.snapshot().locked_inventory == 0


def test_deep_order_does_not_fill_merely_because_time_or_round_passed():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=90.0, amount=2.0, selected_level=3,
        client_order_id="deep-buy", now=T0,
    )

    events = ledger.process_market(
        best_bid=99.0, best_ask=100.0, last_price=100.0,
        now=T0 + timedelta(minutes=5),
    )

    assert events == []
    assert ledger.get_order("deep-buy").status == "open"
    assert ledger.get_order("deep-buy").matched_amount == 0


def test_deep_buy_and_sell_fill_only_when_market_touches_them():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=90.0, amount=2.0, selected_level=3,
        client_order_id="deep-buy", now=T0,
    )
    ledger.submit_order(
        side="sell", limit_price=110.0, amount=2.0, selected_level=3,
        client_order_id="deep-sell", now=T0,
    )

    buy_events = ledger.process_market(
        best_bid=89.0, best_ask=90.0, last_price=90.0,
        now=T0 + timedelta(seconds=1),
    )
    assert [(event.order_id, event.reason) for event in buy_events] == [("deep-buy", "price_touch")]
    assert ledger.get_order("deep-buy").status == "filled"
    assert ledger.get_order("deep-sell").status == "open"

    sell_events = ledger.process_market(
        best_bid=110.0, best_ask=111.0, last_price=110.0,
        now=T0 + timedelta(seconds=2),
    )
    assert [(event.order_id, event.reason) for event in sell_events] == [("deep-sell", "price_touch")]
    assert ledger.get_order("deep-sell").status == "filled"


def test_level1_fast_fill_waits_for_delay_and_supports_partial_fraction():
    ledger = make_ledger(level1_fill_fraction=0.5)
    ledger.submit_order(
        side="buy", limit_price=90.0, amount=4.0, selected_level=1,
        client_order_id="level1-buy", now=T0,
    )

    assert ledger.process_market(
        best_bid=99, best_ask=100, last_price=100,
        now=T0 + timedelta(seconds=1),
    ) == []

    first = ledger.process_market(
        best_bid=99, best_ask=100, last_price=100,
        now=T0 + timedelta(seconds=2),
    )
    assert first[0].reason == "level1_fast_fill"
    assert first[0].new_fill == 2.0
    assert ledger.get_order("level1-buy").status == "partially_filled"

    second = ledger.process_market(
        best_bid=99, best_ask=100, last_price=100,
        now=T0 + timedelta(seconds=3),
    )
    assert second[0].new_fill == 2.0
    assert ledger.get_order("level1-buy").status == "filled"


def test_trade_cross_is_preferred_over_price_touch_reason():
    ledger = make_ledger()
    ledger.submit_order(
        side="sell", limit_price=110.0, amount=1.0, selected_level=4,
        client_order_id="trade-sell", now=T0,
    )

    events = ledger.process_market(
        best_bid=109.0, best_ask=110.0, last_price=111.0,
        trade_price=111.0, now=T0 + timedelta(seconds=1),
    )

    assert events[0].reason == "trade_cross"
    assert ledger.get_order("trade-sell").status == "filled"


def test_queue_volume_is_consumed_before_our_order_fills():
    ledger = make_ledger(queue_enabled=True)
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=10.0, selected_level=4,
        client_order_id="queued-buy", now=T0, queue_ahead=100.0,
    )

    first = ledger.process_market(
        best_bid=99, best_ask=100, last_price=100, trade_price=99,
        traded_volume=40, now=T0 + timedelta(seconds=1),
    )
    assert first == []
    assert ledger.get_order("queued-buy").queue_ahead == 60

    second = ledger.process_market(
        best_bid=99, best_ask=100, last_price=100, trade_price=99,
        traded_volume=70, now=T0 + timedelta(seconds=2),
    )
    assert second[0].new_fill == 10
    assert second[0].reason == "queue_consumed"


def test_price_touch_consumes_queue_ahead_before_filling_with_synthetic_budget():
    ledger = make_ledger(queue_enabled=True, max_fill_per_observation=10.0)
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=10.0, selected_level=1,
        client_order_id="touch-queued-buy", now=T0, queue_ahead=6.0,
    )

    events = ledger.process_market(
        best_bid=99, best_ask=100, last_price=100,
        now=T0 + timedelta(seconds=1),
    )

    assert events[0].new_fill == pytest.approx(4.0)
    assert events[0].reason == "queue_consumed"
    order = ledger.get_order("touch-queued-buy")
    assert order.queue_ahead == pytest.approx(0.0)
    assert order.remaining_amount == pytest.approx(6.0)


def test_synthetic_observation_budget_limits_price_touch_fill_without_reported_volume():
    ledger = make_ledger(max_fill_per_observation=3.0)
    ledger.submit_order(
        side="sell", limit_price=100.0, amount=8.0, selected_level=1,
        client_order_id="budgeted-sell", now=T0,
    )

    events = ledger.process_market(
        best_bid=100, best_ask=101, last_price=100,
        now=T0 + timedelta(seconds=1),
    )

    assert events[0].new_fill == pytest.approx(3.0)
    assert events[0].reason == "price_touch"
    assert ledger.get_order("budgeted-sell").remaining_amount == pytest.approx(5.0)


def test_observation_budget_is_shared_by_same_side_orders_in_price_time_priority():
    ledger = make_ledger(max_fill_per_observation=6.0)
    ledger.submit_order(
        side="buy", limit_price=99.0, amount=5.0, selected_level=2,
        client_order_id="older-worse-buy", now=T0,
    )
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=5.0, selected_level=1,
        client_order_id="newer-better-buy", now=T0 + timedelta(milliseconds=1),
    )

    events = ledger.process_market(
        best_bid=99, best_ask=99, last_price=99,
        now=T0 + timedelta(seconds=1),
    )

    assert [(event.order_id, event.new_fill) for event in events] == [
        ("newer-better-buy", 5.0),
        ("older-worse-buy", 1.0),
    ]
    assert ledger.get_order("newer-better-buy").status == "filled"
    assert ledger.get_order("older-worse-buy").status == "partially_filled"


def test_adverse_fill_probability_allows_adverse_touch():
    ledger = make_ledger(
        level1_fast_fill_enabled=False,
        adverse_fill_enabled=True,
        adverse_fill_probability=1.0,
        non_adverse_fill_probability=0.0,
    )
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=2.0, selected_level=1,
        client_order_id="adverse-buy", now=T0,
    )

    events = ledger.process_market(
        best_bid=99, best_ask=100, last_price=99,
        now=T0 + timedelta(seconds=1),
    )

    assert events[0].order_id == "adverse-buy"
    assert events[0].reason == "adverse_price_touch"
    assert ledger.get_order("adverse-buy").status == "filled"


def test_non_adverse_fill_probability_can_leave_trade_cross_open():
    ledger = make_ledger(
        level1_fast_fill_enabled=False,
        adverse_fill_enabled=True,
        adverse_fill_probability=1.0,
        non_adverse_fill_probability=0.0,
    )
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=2.0, selected_level=1,
        client_order_id="non-adverse-buy", now=T0,
    )

    events = ledger.process_market(
        best_bid=100, best_ask=101, last_price=100, trade_price=100,
        traded_volume=10, now=T0 + timedelta(seconds=1),
    )

    assert events == []
    assert ledger.get_order("non-adverse-buy").status == "open"


def test_disabled_adverse_model_preserves_trade_cross_fill():
    ledger = make_ledger(
        level1_fast_fill_enabled=False,
        adverse_fill_enabled=False,
        adverse_fill_probability=0.0,
        non_adverse_fill_probability=0.0,
    )
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=2.0, selected_level=1,
        client_order_id="normal-buy", now=T0,
    )

    events = ledger.process_market(
        best_bid=100, best_ask=101, last_price=100, trade_price=100,
        traded_volume=10, now=T0 + timedelta(seconds=1),
    )

    assert events[0].order_id == "normal-buy"
    assert events[0].reason == "trade_cross"


def test_cancel_preserves_matched_amount_and_releases_remaining_lock():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=10.0, selected_level=4,
        client_order_id="partial-buy", now=T0,
    )
    ledger.apply_cumulative_fill(
        "partial-buy", cumulative_matched=4.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=1),
    )

    event = ledger.cancel_order(
        "partial-buy", reason="stale_timeout", now=T0 + timedelta(minutes=11)
    )

    order = ledger.get_order("partial-buy")
    assert event.cancelled_remainder == 6.0
    assert order.status == "cancelled"
    assert order.matched_amount == 4.0
    assert order.remaining_amount == 6.0
    assert "partial-buy" not in ledger.active_orders
    assert "partial-buy" in ledger.order_history
    assert ledger.snapshot().locked_cash == 0
    assert event.released_locked_amount == pytest.approx(600.0)
    assert event.released_locked_kind == "cash"
    assert event.released_locked_unit == "IRT"
    assert event.initiator == "paper_ledger"
    assert event.call_site == "PaperLedger.cancel_order"
    assert event.session_id == "shared-session"
    assert event.symbol == "USDTIRT"
    assert event.process_id > 0
    assert event.timestamp == (T0 + timedelta(minutes=11)).isoformat()


def test_one_usdt_buy_at_ninety_releases_ninety_irt():
    ledger = PaperLedger(
        session_id="shared-session",
        symbol="USDTIRT",
        initial_inventory=10.0,
        initial_cash=10_000.0,
        commission_perc=0.001,
        valuation_price=100.0,
        state_path=None,
    )
    ledger.submit_order(
        side="buy", limit_price=90.0, amount=1.0, selected_level=4,
        client_order_id="ninety-irt", now=T0,
    )

    event = ledger.cancel_order(
        "ninety-irt", reason="manual_cancel", now=T0 + timedelta(seconds=1),
        initiator="operator", call_site="test.manual_cancel",
    )

    assert event.released_locked_amount == pytest.approx(90.0)
    assert event.released_locked_kind == "cash"
    assert event.released_locked_unit == "IRT"
    assert event.initiator == "operator"
    assert event.call_site == "test.manual_cancel"
    assert ledger.get_order("ninety-irt").cancellation_context == {
        "reason": "manual_cancel",
        "initiator": "operator",
        "call_site": "test.manual_cancel",
        "session_id": "shared-session",
        "symbol": "USDTIRT",
        "process_id": event.process_id,
        "timestamp": (T0 + timedelta(seconds=1)).isoformat(),
        "released_locked_amount": 90.0,
        "released_locked_kind": "cash",
        "released_locked_unit": "IRT",
    }


def test_stale_cancellation_only_targets_expired_active_orders():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=90.0, amount=1.0, selected_level=4,
        client_order_id="old", now=T0,
    )
    ledger.submit_order(
        side="buy", limit_price=90.0, amount=1.0, selected_level=4,
        client_order_id="fresh", now=T0 + timedelta(minutes=9),
    )

    events = ledger.cancel_stale(max_age_seconds=600, now=T0 + timedelta(minutes=11))

    assert [event.order_id for event in events] == ["old"]
    assert ledger.get_order("old").status == "cancelled"
    assert ledger.get_order("fresh").status == "open"
