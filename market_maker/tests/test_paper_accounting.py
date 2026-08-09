import json
from datetime import datetime, timedelta, timezone

import pytest

from paper import PaperLedger


T0 = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def make_ledger(**overrides):
    values = {
        "session_id": "shared",
        "symbol": "USDTIRT",
        "initial_inventory": 20.0,
        "initial_cash": 2_000.0,
        "commission_perc": 0.01,
        "valuation_price": 100.0,
        "level1_fast_fill_enabled": False,
    }
    values.update(overrides)
    return PaperLedger(**values)


def test_cumulative_partial_fills_apply_deltas_four_then_six_exactly_once():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=10.0, selected_level=3,
        client_order_id="buy", now=T0,
    )

    first = ledger.apply_cumulative_fill(
        "buy", cumulative_matched=4.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=1),
    )
    duplicate = ledger.apply_cumulative_fill(
        "buy", cumulative_matched=4.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=2),
    )
    second = ledger.apply_cumulative_fill(
        "buy", cumulative_matched=10.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=3),
    )

    assert first.new_fill == 4.0
    assert duplicate is None
    assert second.new_fill == 6.0
    order = ledger.get_order("buy")
    assert order.matched_amount == 10.0
    assert order.processed_matched_amount == 10.0
    assert order.remaining_amount == 0.0
    assert order.status == "filled"

    snapshot = ledger.snapshot()
    assert snapshot.cash == pytest.approx(1_000.0)
    assert snapshot.inventory == pytest.approx(29.9)
    assert snapshot.fees_quote == pytest.approx(10.0)
    assert snapshot.locked_cash == 0


def test_partial_buy_reduces_lock_to_remaining_quantity():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=10.0, selected_level=2,
        client_order_id="buy", now=T0,
    )

    ledger.apply_cumulative_fill(
        "buy", cumulative_matched=4.0, fill_price=95.0,
        reason="price_touch", now=T0 + timedelta(seconds=1),
    )

    snapshot = ledger.snapshot()
    assert snapshot.locked_cash == 600.0
    assert snapshot.cash == 1_620.0
    assert snapshot.available_cash == 1_020.0


def test_sell_fill_fee_is_quote_asset_and_wealth_uses_last_trade():
    ledger = make_ledger(valuation_price=100.0)
    ledger.submit_order(
        side="sell", limit_price=110.0, amount=2.0, selected_level=2,
        client_order_id="sell", now=T0,
    )

    event = ledger.apply_cumulative_fill(
        "sell", cumulative_matched=2.0, fill_price=110.0,
        reason="price_touch", now=T0 + timedelta(seconds=1),
    )

    assert event.fee == pytest.approx(2.2)
    snapshot = ledger.snapshot(valuation_price=120.0)
    assert snapshot.inventory == 18.0
    assert snapshot.cash == pytest.approx(2_217.8)
    assert snapshot.fees_quote == pytest.approx(2.2)
    assert snapshot.wealth == pytest.approx(18 * 120 + 2_217.8)


def test_average_fill_price_is_notional_weighted():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=110.0, amount=10.0, selected_level=3,
        client_order_id="buy", now=T0,
    )
    ledger.apply_cumulative_fill(
        "buy", cumulative_matched=4.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=1),
    )
    ledger.apply_cumulative_fill(
        "buy", cumulative_matched=10.0, fill_price=110.0,
        reason="trade_volume", now=T0 + timedelta(seconds=2),
    )

    assert ledger.get_order("buy").average_fill_price == pytest.approx(106.0)


def test_duplicate_client_order_id_is_rejected():
    ledger = make_ledger()
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=1.0, selected_level=2,
        client_order_id="same", now=T0,
    )

    with pytest.raises(ValueError, match="already exists"):
        ledger.submit_order(
            side="buy", limit_price=100.0, amount=1.0, selected_level=2,
            client_order_id="same", now=T0,
        )


def test_state_recovers_open_order_locks_and_processed_fill_without_duplication(tmp_path):
    state_path = tmp_path / "usdt-state.json"
    ledger = make_ledger(state_path=state_path)
    ledger.submit_order(
        side="buy", limit_price=100.0, amount=10.0, selected_level=3,
        client_order_id="buy", now=T0,
    )
    ledger.apply_cumulative_fill(
        "buy", cumulative_matched=4.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=1),
    )

    recovered = PaperLedger.load(state_path)
    duplicate = recovered.apply_cumulative_fill(
        "buy", cumulative_matched=4.0, fill_price=100.0,
        reason="trade_volume", now=T0 + timedelta(seconds=2),
    )

    assert duplicate is None
    assert recovered.get_order("buy").status == "partially_filled"
    assert recovered.get_order("buy").processed_matched_amount == 4.0
    assert recovered.snapshot().locked_cash == 600.0
    assert recovered.snapshot().cash == pytest.approx(1_600.0)
    assert recovered.snapshot().inventory == pytest.approx(23.96)


def test_state_persists_immutable_session_seed_across_balance_changes(tmp_path):
    state_path = tmp_path / "seed-state.json"
    ledger = make_ledger(state_path=state_path)
    ledger.submit_order(
        side="buy", limit_price=100, amount=1, selected_level=1,
        client_order_id="seed-buy", now=T0,
    )
    ledger.apply_cumulative_fill(
        "seed-buy", cumulative_matched=1, fill_price=100,
        reason="price_touch", now=T0 + timedelta(seconds=1),
    )

    document = json.loads(state_path.read_text())
    recovered = PaperLedger.load(state_path)

    assert document["session_seed"] == {
        "inventory": 20.0,
        "cash": 2_000.0,
        "commission_perc": 0.01,
    }
    assert (
        recovered.initial_inventory,
        recovered.initial_cash,
        recovered.initial_commission_perc,
    ) == (20.0, 2_000.0, 0.01)
    assert (recovered.inventory, recovered.cash) == pytest.approx((20.99, 1_900))


def test_legacy_state_without_session_seed_loads_current_account_as_seed(tmp_path):
    state_path = tmp_path / "legacy-state.json"
    ledger = make_ledger(state_path=state_path)
    ledger.submit_order(
        side="sell", limit_price=100, amount=1, selected_level=1,
        client_order_id="legacy-sell", now=T0,
    )
    ledger.apply_cumulative_fill(
        "legacy-sell", cumulative_matched=1, fill_price=100,
        reason="price_touch", now=T0 + timedelta(seconds=1),
    )
    document = json.loads(state_path.read_text())
    document.pop("session_seed", None)
    document["version"] = 2
    state_path.write_text(json.dumps(document))

    recovered = PaperLedger.load(state_path)

    assert (
        recovered.initial_inventory,
        recovered.initial_cash,
        recovered.initial_commission_perc,
    ) == pytest.approx(
        (recovered.inventory, recovered.cash, recovered.commission_perc)
    )


def test_fee_scopes_separate_round_session_pair_and_all_time(tmp_path):
    fee_path = tmp_path / "fees.json"
    btc = PaperLedger(
        session_id="session-1", symbol="BTCIRT", initial_inventory=2,
        initial_cash=10_000, commission_perc=0.01, valuation_price=100,
        level1_fast_fill_enabled=False, fee_state_path=fee_path,
    )
    usdt = PaperLedger(
        session_id="session-1", symbol="USDTIRT", initial_inventory=2,
        initial_cash=10_000, commission_perc=0.01, valuation_price=100,
        level1_fast_fill_enabled=False, fee_state_path=fee_path,
    )
    btc.submit_order(
        side="sell", limit_price=100, amount=1, selected_level=2,
        client_order_id="btc-sell", now=T0,
    )
    usdt.submit_order(
        side="sell", limit_price=200, amount=1, selected_level=2,
        client_order_id="usdt-sell", now=T0,
    )

    btc_event = btc.apply_cumulative_fill(
        "btc-sell", cumulative_matched=1, fill_price=100,
        reason="price_touch", now=T0 + timedelta(seconds=1),
    )
    usdt_event = usdt.apply_cumulative_fill(
        "usdt-sell", cumulative_matched=1, fill_price=200,
        reason="price_touch", now=T0 + timedelta(seconds=1),
    )

    assert btc_event.fee_quote == pytest.approx(1.0)
    assert usdt_event.fee_quote == pytest.approx(2.0)
    btc_scopes = btc.fee_scopes()
    usdt_scopes = usdt.fee_scopes()
    assert btc_scopes.session_pair == pytest.approx(1.0)
    assert usdt_scopes.session_pair == pytest.approx(2.0)
    assert btc_scopes.session_all_symbols == pytest.approx(3.0)
    assert usdt_scopes.session_all_symbols == pytest.approx(3.0)
    assert btc_scopes.pair_all_time == pytest.approx(1.0)
    assert usdt_scopes.pair_all_time == pytest.approx(2.0)
    assert btc_scopes.all_symbols_all_time == pytest.approx(3.0)

    next_session = PaperLedger(
        session_id="session-2", symbol="BTCIRT", initial_inventory=2,
        initial_cash=10_000, commission_perc=0.01, valuation_price=100,
        level1_fast_fill_enabled=False, fee_state_path=fee_path,
    )
    scopes = next_session.fee_scopes()
    assert scopes.session_pair == 0
    assert scopes.session_all_symbols == 0
    assert scopes.pair_all_time == pytest.approx(1.0)
    assert scopes.all_symbols_all_time == pytest.approx(3.0)


def test_shared_fee_event_is_idempotent_across_crash_before_pair_state_persist(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    fee_path = tmp_path / "fees.json"
    ledger = PaperLedger(
        session_id="session", symbol="USDTIRT", initial_inventory=0,
        initial_cash=1_000, commission_perc=0.001, valuation_price=100,
        level1_fast_fill_enabled=False, state_path=state_path, fee_state_path=fee_path,
    )
    ledger.submit_order(
        side="buy", limit_price=100, amount=1, selected_level=2,
        client_order_id="buy", now=T0,
    )
    monkeypatch.setattr(
        ledger, "_persist", lambda: (_ for _ in ()).throw(RuntimeError("crash"))
    )

    with pytest.raises(RuntimeError, match="crash"):
        ledger.apply_cumulative_fill(
            "buy", cumulative_matched=1, fill_price=100,
            reason="price_touch", now=T0 + timedelta(seconds=1),
        )

    recovered = PaperLedger.load(state_path)
    recovered.apply_cumulative_fill(
        "buy", cumulative_matched=1, fill_price=100,
        reason="price_touch", now=T0 + timedelta(seconds=2),
    )
    assert recovered.fee_scopes().session_pair == pytest.approx(0.1)
