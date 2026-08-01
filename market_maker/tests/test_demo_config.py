import pytest

from demo_config import (
    OverlayValidationError,
    apply_overrides,
    has_previous,
    load_overrides,
    overlay_path,
    rollback_overrides,
    validate_overrides,
    write_overrides,
)
from run import make_cfg


def test_validate_accepts_known_fields():
    validated = validate_overrides({"order_size": 5, "demo_queue_enabled": True})
    assert validated == {"order_size": 5.0, "demo_queue_enabled": True}


@pytest.mark.parametrize("key", ["pair", "token", "url_send_order", "algorithm_id"])
def test_validate_rejects_identity_and_secret_fields(key):
    with pytest.raises(OverlayValidationError):
        validate_overrides({key: "x"})


def test_validate_rejects_unknown_field():
    with pytest.raises(OverlayValidationError):
        validate_overrides({"not_a_real_field": 1})


@pytest.mark.parametrize("payload", [
    {"commission_perc": -0.1},
    {"commission_perc": 1.5},
    {"depth_max": 0},
    {"demo_level1_fill_fraction": 2},
    {"order_size": -1},
])
def test_validate_rejects_out_of_range(payload):
    with pytest.raises(OverlayValidationError):
        validate_overrides(payload)


def test_validate_rejects_wrong_type():
    with pytest.raises(OverlayValidationError):
        validate_overrides({"order_size": "lots"})


def test_write_read_round_trip(tmp_path):
    path = overlay_path(tmp_path, "btc")
    write_overrides(path, {"order_size": 3, "depth_max": 5})
    assert load_overrides(path) == {"order_size": 3.0, "depth_max": 5}


def test_previous_created_and_restorable(tmp_path):
    path = overlay_path(tmp_path, "btc")
    assert not has_previous(path)
    write_overrides(path, {"order_size": 1})
    assert not has_previous(path)  # nothing to preserve on first write
    write_overrides(path, {"order_size": 2})
    assert has_previous(path)
    assert load_overrides(path) == {"order_size": 2.0}
    restored = rollback_overrides(path)
    assert restored == {"order_size": 1.0}
    assert load_overrides(path) == {"order_size": 1.0}


def test_rollback_without_previous_raises(tmp_path):
    path = overlay_path(tmp_path, "btc")
    with pytest.raises(OverlayValidationError):
        rollback_overrides(path)


def test_apply_overrides_mutates_cfg_fields():
    class Cfg:
        order_size = 1.0
    cfg = Cfg()
    apply_overrides(cfg, {"order_size": 9.0})
    assert cfg.order_size == 9.0


def test_make_cfg_applies_overlay_only_in_demo_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LOB_STATE_DIR", str(tmp_path))
    import config
    monkeypatch.setattr(config, "DEMO_STATE_DIR", str(tmp_path))
    import run
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    write_overrides(overlay_path(tmp_path, "btc"), {"order_size": 42})

    demo_cfg = make_cfg("btc", mode="demo")
    assert demo_cfg.order_size == 42.0

    real_cfg = make_cfg("btc", mode="real")
    assert real_cfg.order_size != 42.0
