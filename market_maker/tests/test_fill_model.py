"""fill_model.py: the measured table, the parametric fallback, freshness and
sample-size rules."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from config import MMConfig
from fill_model import FillModel, FillTable, formula_probability, write_fill_table


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _cfg(**overrides):
    base = dict(pair="USDTIRT", fill_kappa=400.0, fill_lambda_tau=1.0,
                fill_table_enabled=True, fill_table_max_age_hours=48.0, fill_table_min_samples=50)
    base.update(overrides)
    return MMConfig(**base)


def _write(tmp_path, buckets, *, updated_at=NOW, name="fill_table_usdtirt.json"):
    path = tmp_path / name
    write_fill_table(str(path), pair="USDTIRT", horizon_seconds=600, method="test",
                     buckets=buckets, updated_at=updated_at)
    return path


BUCKETS = [
    {"distance": 0.001, "p_bid": 0.60, "p_ask": 0.58, "n_bid": 300, "n_ask": 300},
    {"distance": 0.002, "p_bid": 0.38, "p_ask": 0.36, "n_bid": 300, "n_ask": 300},
    {"distance": 0.004, "p_bid": 0.19, "p_ask": 0.17, "n_bid": 120, "n_ask": 120},
    {"distance": 0.008, "p_bid": 0.04, "p_ask": 0.03, "n_bid": 20, "n_ask": 20},
]


# ---------- formula ----------

def test_formula_is_one_at_zero_distance_with_large_intensity_and_decreases():
    import math
    assert formula_probability(0.0, 400.0, 50.0) == pytest.approx(1.0, abs=1e-12)
    values = [formula_probability(d, 400.0, 1.0) for d in (0.0, 0.001, 0.0025, 0.005, 0.02)]
    assert values == sorted(values, reverse=True)
    # kappa = 400: at 0.25 % the intensity is e^-1 of its touch value
    assert formula_probability(0.0025, 400.0, 1.0) == pytest.approx(1 - math.exp(-math.exp(-1.0)))
    assert formula_probability(0.0, 400.0, 1.0) == pytest.approx(1 - math.exp(-1.0))


def test_formula_clamps_bad_inputs():
    assert formula_probability(-1.0, 400.0, 1.0) == formula_probability(0.0, 400.0, 1.0)
    assert formula_probability(0.01, -5.0, 1.0) == formula_probability(0.01, 0.0, 1.0)
    assert formula_probability(0.01, 400.0, -3.0) == 0.0


# ---------- table ----------

def test_table_interpolates_between_buckets_and_clamps_at_the_ends():
    table = FillTable.from_document({"pair": "USDTIRT", "horizon_seconds": 600, "buckets": BUCKETS})
    assert table.lookup(0.0015, "bid", 50) == pytest.approx(0.49)
    assert table.lookup(0.0001, "bid", 50) == pytest.approx(0.60)
    assert table.lookup(0.05, "ask", 50) == pytest.approx(0.17)   # 0.008 has too few samples
    assert table.lookup(0.002, "ask", 50) == pytest.approx(0.36)


def test_table_ignores_buckets_below_the_sample_floor():
    table = FillTable.from_document({"pair": "USDTIRT", "buckets": BUCKETS})
    assert table.lookup(0.008, "bid", 50) == pytest.approx(0.19)   # falls back to the last usable bucket
    assert table.lookup(0.008, "bid", 10) == pytest.approx(0.04)
    assert table.lookup(0.008, "bid", 1000) is None


def test_table_rejects_malformed_documents():
    with pytest.raises(ValueError):
        FillTable.from_document({"buckets": []})
    with pytest.raises(ValueError):
        FillTable.from_document({"buckets": [{"distance": 0, "p_bid": 0.5}]})
    with pytest.raises(ValueError):
        FillTable.from_document([])


# ---------- model ----------

def test_model_uses_the_table_when_fresh_and_the_formula_otherwise(tmp_path):
    path = _write(tmp_path, BUCKETS)
    cfg = _cfg(fill_table_path=str(path))
    model = FillModel(cfg, now_provider=lambda: NOW)
    assert model.probability(0.001, "bid") == pytest.approx(0.60)
    assert model.source(0.001, "bid") == "table"
    assert model.probability(0.008, "ask") == pytest.approx(0.17)      # few samples -> last usable bucket
    assert model.describe()["table_fresh"] is True

    stale = FillModel(cfg, now_provider=lambda: NOW + timedelta(hours=72))
    assert stale.source(0.001, "bid") == "formula"
    assert stale.probability(0.001, "bid") == pytest.approx(formula_probability(0.001, 400.0, 1.0))
    assert stale.describe()["table_fresh"] is False


def test_model_falls_back_without_a_table_and_when_disabled(tmp_path):
    cfg = _cfg(fill_table_path=str(tmp_path / "missing.json"))
    model = FillModel(cfg, now_provider=lambda: NOW)
    assert model.source(0.002, "bid") == "formula"
    described = model.describe()
    assert described["table_loaded"] is False and described["table_buckets"] == 0

    path = _write(tmp_path, BUCKETS)
    cfg = _cfg(fill_table_path=str(path), fill_table_enabled=False)
    assert FillModel(cfg, now_provider=lambda: NOW).source(0.002, "bid") == "formula"


def test_model_reloads_when_the_file_changes(tmp_path):
    path = _write(tmp_path, BUCKETS)
    cfg = _cfg(fill_table_path=str(path))
    model = FillModel(cfg, now_provider=lambda: NOW)
    assert model.probability(0.001, "bid") == pytest.approx(0.60)
    changed = [dict(b, p_bid=0.9) for b in BUCKETS]
    import os, time
    time.sleep(0.01)
    write_fill_table(str(path), pair="USDTIRT", horizon_seconds=600, method="test", buckets=changed, updated_at=NOW)
    os.utime(path, None)
    model._table_mtime = None   # mtime resolution on some filesystems is 1 s
    assert model.probability(0.001, "bid") == pytest.approx(0.9)


def test_model_default_path_uses_the_results_dir_and_pair(tmp_path):
    model = FillModel(_cfg(), results_dir=str(tmp_path))
    assert model.table_path() == str(tmp_path / "fill_table_usdtirt.json")


def test_model_tolerates_a_corrupt_table(tmp_path):
    path = tmp_path / "fill_table_usdtirt.json"
    path.write_text("{not json")
    model = FillModel(_cfg(fill_table_path=str(path)), now_provider=lambda: NOW)
    assert model.source(0.002, "ask") == "formula"
    assert model.describe()["table_error"]


def test_write_fill_table_round_trips(tmp_path):
    path = tmp_path / "t.json"
    document = write_fill_table(str(path), pair="BTCIRT", horizon_seconds=600, method="unit",
                                buckets=BUCKETS, updated_at=NOW, extra={"samples": 5})
    loaded = json.loads(path.read_text())
    assert loaded == document
    assert loaded["samples"] == 5 and loaded["pair"] == "BTCIRT"
    assert FillTable.from_document(loaded).updated_at == NOW
