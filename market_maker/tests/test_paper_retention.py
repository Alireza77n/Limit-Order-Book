import json
from datetime import datetime, timedelta, timezone

import pytest

from paper import PaperEvent, PaperLedger


T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def make_ledger(tmp_path, *, limit=2, archive_max_bytes=10_000):
    return PaperLedger(
        session_id="session",
        symbol="USDTIRT",
        initial_inventory=10,
        initial_cash=10_000,
        commission_perc=0.001,
        valuation_price=100,
        state_path=tmp_path / "paper.json",
        fee_state_path=tmp_path / "fees.json",
        archive_path=tmp_path / "paper-archive.jsonl",
        hot_terminal_order_limit=limit,
        archive_max_bytes=archive_max_bytes,
    )


def submit(ledger, order_id, now, *, side="buy"):
    return ledger.submit_order(
        side=side,
        limit_price=100,
        amount=1,
        selected_level=2,
        client_order_id=order_id,
        now=now,
    )


def terminal(ledger, order_id, now):
    submit(ledger, order_id, now)
    return ledger.cancel_order(order_id, reason="test", now=now + timedelta(seconds=1))


def archive_records(tmp_path):
    records = []
    for path in sorted(tmp_path.glob("paper-archive*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return records


def test_compaction_archives_old_terminal_but_keeps_active_pending_and_recent(tmp_path):
    ledger = make_ledger(tmp_path, limit=2)
    for number in range(5):
        terminal(ledger, f"done-{number}", T0 + timedelta(minutes=number))
    active = submit(ledger, "active", T0 + timedelta(minutes=10))
    pending = terminal(ledger, "pending", T0 + timedelta(minutes=11))
    ledger.get_order("pending").telegram_pending_event = {
        **PaperEvent(order_id="pending", status="cancelled", reason="test").__dict__
    }
    ledger._persist()

    assert ledger.compact() == 3

    assert set(ledger.order_history) == {"done-3", "done-4", "active", "pending"}
    assert set(ledger.active_orders) == {"active"}
    assert {record["order_id"] for record in archive_records(tmp_path)} == {
        "done-0", "done-1", "done-2"
    }


def test_archive_failure_leaves_hot_state_unchanged(monkeypatch, tmp_path):
    ledger = make_ledger(tmp_path, limit=0)
    terminal(ledger, "done", T0)
    monkeypatch.setattr(
        ledger,
        "_append_archive_record",
        lambda order: (_ for _ in ()).throw(OSError("archive unavailable")),
    )

    with pytest.raises(OSError, match="archive unavailable"):
        ledger.compact()

    assert "done" in ledger.order_history
    assert "done" in PaperLedger.load(tmp_path / "paper.json").order_history


def test_restart_after_archive_before_hot_persist_does_not_duplicate(monkeypatch, tmp_path):
    ledger = make_ledger(tmp_path, limit=0)
    terminal(ledger, "done", T0)
    original_write = ledger._atomic_json_write

    def fail_hot_write(path, document):
        if path == ledger.state_path:
            raise OSError("crash after archive")
        return original_write(path, document)

    monkeypatch.setattr(ledger, "_atomic_json_write", fail_hot_write)
    with pytest.raises(OSError, match="crash after archive"):
        ledger.compact()

    recovered = PaperLedger.load(tmp_path / "paper.json")
    assert recovered.compact() == 1
    assert [record["order_id"] for record in archive_records(tmp_path)] == ["done"]


def test_archive_rotates_at_configured_size(tmp_path):
    ledger = make_ledger(tmp_path, limit=0, archive_max_bytes=200)
    for number in range(3):
        terminal(ledger, f"done-{number}", T0 + timedelta(minutes=number))

    ledger.compact()

    assert len(list(tmp_path.glob("paper-archive*.jsonl"))) >= 2
    assert len(archive_records(tmp_path)) == 3


def test_fee_high_watermark_is_one_record_per_order_and_pruned_after_archive(tmp_path):
    ledger = make_ledger(tmp_path, limit=0)
    submit(ledger, "buy", T0)
    ledger.apply_cumulative_fill(
        "buy", cumulative_matched=0.4, fill_price=100,
        reason="trade", now=T0 + timedelta(seconds=1),
    )
    ledger.apply_cumulative_fill(
        "buy", cumulative_matched=1.0, fill_price=100,
        reason="trade", now=T0 + timedelta(seconds=2),
    )
    fees = json.loads((tmp_path / "fees.json").read_text())
    assert list(fees["order_high_watermarks"]) == ["session:USDTIRT:buy"]
    assert fees["order_high_watermarks"]["session:USDTIRT:buy"]["cumulative_amount"] == 1.0
    assert fees["sessions"]["session"]["USDTIRT"] == pytest.approx(0.1)

    assert ledger.compact() == 1
    fees = json.loads((tmp_path / "fees.json").read_text())
    assert fees["order_high_watermarks"] == {}
    assert fees["sessions"]["session"]["USDTIRT"] == pytest.approx(0.1)


def test_legacy_applied_fee_events_migrate_without_double_counting(tmp_path):
    fee_path = tmp_path / "fees.json"
    fee_path.write_text(json.dumps({
        "sessions": {"session": {"USDTIRT": 0.1}},
        "all_time": {"USDTIRT": 0.1},
        "applied_events": {"session:USDTIRT:buy:1": 0.1},
    }))
    ledger = make_ledger(tmp_path, limit=1)

    ledger._record_shared_fee(
        0.1, order_id="buy", cumulative_amount=1.0
    )

    migrated = json.loads(fee_path.read_text())
    assert "applied_events" not in migrated
    assert migrated["order_high_watermarks"]["session:USDTIRT:buy"] == {
        "cumulative_amount": 1.0,
        "fee_quote": 0.1,
    }
    assert migrated["sessions"]["session"]["USDTIRT"] == pytest.approx(0.1)
