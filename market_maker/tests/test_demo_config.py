import pytest

import config
import demo_config

from demo_config import (
    FIELD_DEFINITIONS,
    OverlayLoadError,
    OverlayValidationError,
    apply_overrides,
    desired_digest,
    editable_schema,
    has_previous,
    load_desired_config,
    load_overrides,
    overlay_path,
    rollback_overrides,
    validate_overrides,
    write_overrides,
)
from run import make_cfg


PROFILES = config.TRADING_PROFILES


EXPECTED = {
    "order_size": "hot", "inv_max": "hot", "inv_min": "hot",
    "cancel_after_minutes": "hot", "depth_max": "hot", "time_step": "hot",
    "demo_inv_initial": "new_session", "cash_initial": "new_session",
    "commission_perc": "new_session",
    "buy_locked_cash_limit_enabled": "hot", "buy_locked_cash_limit_ratio": "hot",
    "sell_locked_inventory_limit_enabled": "hot", "sell_locked_inventory_limit_ratio": "hot",
}


@pytest.mark.parametrize("pair", ["btc", "usdt"])
def test_editable_schema_is_pair_profile_intersection(pair):
    from demo_config import editable_schema

    schema = editable_schema(pair, config.TRADING_PROFILES)
    assert {key: field.mode.value for key, field in schema.items()} == EXPECTED
    assert set(schema) <= set(config.TRADING_PROFILES[pair])


def test_obsolete_and_continuous_only_fields_are_not_editable():
    from demo_config import editable_schema

    assert "no_quote_recovery_alert" not in editable_schema("btc", config.TRADING_PROFILES)
    assert "max_rounds" not in editable_schema("btc", config.TRADING_PROFILES)


def test_field_definitions_are_read_only():
    assert not hasattr(FIELD_DEFINITIONS, "update")


def test_deprecated_field_specs_compatibility_shim_is_removed():
    assert not hasattr(demo_config, "FIELD_SPECS")


def test_validate_accepts_known_fields():
    validated = validate_overrides({"order_size": 5, "depth_max": 2}, pair="btc", profiles=PROFILES)
    assert validated == {"order_size": 5.0, "depth_max": 2}


@pytest.mark.parametrize("key", ["pair", "token", "url_send_order", "algorithm_id"])
def test_validate_rejects_identity_and_secret_fields(key):
    with pytest.raises(OverlayValidationError):
        validate_overrides({key: "x"}, pair="btc", profiles=PROFILES)


def test_validate_rejects_unknown_field():
    with pytest.raises(OverlayValidationError):
        validate_overrides({"not_a_real_field": 1}, pair="btc", profiles=PROFILES)


@pytest.mark.parametrize("payload", [
    {"commission_perc": -0.1},
    {"commission_perc": 1.5},
    {"depth_max": 0},
    {"time_step": 0},
    {"order_size": -1},
])
def test_validate_rejects_out_of_range(payload):
    with pytest.raises(OverlayValidationError):
        validate_overrides(payload, pair="btc", profiles=PROFILES)


def test_validate_rejects_wrong_type():
    with pytest.raises(OverlayValidationError):
        validate_overrides({"order_size": "lots"}, pair="btc", profiles=PROFILES)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_rejects_non_finite_numeric_values(value):
    with pytest.raises(OverlayValidationError, match="finite"):
        validate_overrides({"order_size": value}, pair="btc", profiles=PROFILES)


def test_write_read_round_trip(tmp_path):
    path = overlay_path(tmp_path, "btc")
    write_overrides(path, {"order_size": 3, "depth_max": 5}, pair="btc", profiles=PROFILES)
    assert load_overrides(path) == {"order_size": 3.0, "depth_max": 5}


def test_previous_created_and_restorable(tmp_path):
    path = overlay_path(tmp_path, "btc")
    assert not has_previous(path)
    write_overrides(path, {"order_size": 1}, pair="btc", profiles=PROFILES)
    assert not has_previous(path)  # nothing to preserve on first write
    write_overrides(path, {"order_size": 2}, pair="btc", profiles=PROFILES)
    assert has_previous(path)
    assert load_overrides(path) == {"order_size": 2.0}
    restored = rollback_overrides(path)
    assert restored == {"order_size": 1.0}
    assert load_overrides(path) == {"order_size": 1.0}


def test_rollback_without_previous_raises(tmp_path):
    path = overlay_path(tmp_path, "btc")
    with pytest.raises(OverlayValidationError):
        rollback_overrides(path)


def test_load_desired_config_layers_only_pair_editable_values(tmp_path):
    write_overrides(overlay_path(tmp_path, "btc"), {"order_size": 0.25}, pair="btc", profiles=PROFILES)
    desired = load_desired_config("btc", tmp_path, PROFILES)
    assert desired["order_size"] == 0.25
    assert desired["time_step"] == PROFILES["btc"]["time_step"]
    assert load_desired_config("usdt", tmp_path, PROFILES)["order_size"] == PROFILES["usdt"]["order_size"]


def test_load_desired_config_supports_a_profile_field_intersection(tmp_path):
    profiles = {"btc": {"order_size": 0.25}}

    assert load_desired_config("btc", tmp_path, profiles) == {"order_size": 0.25}


@pytest.mark.parametrize("values", [{"depth_max": 0}, {"commission_perc": 2}])
def test_load_desired_config_validates_profile_defaults(tmp_path, values):
    with pytest.raises(OverlayValidationError):
        load_desired_config("btc", tmp_path, {"btc": values})


def test_corrupt_overlay_is_an_explicit_error(tmp_path):
    overlay_path(tmp_path, "btc").write_text("{")
    with pytest.raises(OverlayLoadError, match="invalid JSON"):
        load_desired_config("btc", tmp_path, PROFILES)


@pytest.mark.parametrize("values", [
    {"inv_min": 2, "inv_max": 1},
    {"order_size": 0},
])
def test_cross_field_validation_rejects_invalid_desired_config(tmp_path, values):
    with pytest.raises(OverlayValidationError):
        write_overrides(overlay_path(tmp_path, "btc"), values, pair="btc", profiles=PROFILES)


def test_load_desired_config_rejects_equal_inventory_bounds(tmp_path):
    profiles = {"btc": {"inv_min": 2.0, "inv_max": 2.0}}

    with pytest.raises(OverlayValidationError, match="inv_min must be < inv_max"):
        load_desired_config("btc", tmp_path, profiles)


def test_write_overrides_rejects_equal_inventory_bounds_without_persisting(tmp_path):
    path = overlay_path(tmp_path, "btc")

    with pytest.raises(OverlayValidationError, match="inv_min must be < inv_max"):
        write_overrides(
            path,
            {"inv_min": 2.0, "inv_max": 2.0},
            pair="btc",
            profiles=PROFILES,
        )

    assert not path.exists()


def test_known_legacy_keys_are_removed_while_unknown_keys_fail_closed(tmp_path):
    path = overlay_path(tmp_path, "btc")
    path.write_text('{"order_size": 0.25, "max_rounds": 4}')

    assert load_desired_config("btc", tmp_path, PROFILES)["order_size"] == 0.25
    assert "max_rounds" not in load_overrides(path, pair="btc", profiles=PROFILES)

    path.write_text('{"not_a_real_field": 4}')
    with pytest.raises(OverlayValidationError, match="unknown field"):
        load_desired_config("btc", tmp_path, PROFILES)


def test_write_preserves_existing_overlay_only_after_strict_pair_validation(tmp_path):
    path = overlay_path(tmp_path, "btc")
    previous_path = path.with_name(path.name + ".previous")
    path.write_text('{"not_a_real_field": 4}')
    previous_path.write_text('{"order_size": 0.1}')

    with pytest.raises(OverlayValidationError, match="unknown field"):
        write_overrides(path, {"order_size": 0.25}, pair="btc", profiles=PROFILES)

    assert path.read_text() == '{"not_a_real_field": 4}'
    assert previous_path.read_text() == '{"order_size": 0.1}'


def test_write_failure_preserves_live_and_previous_files_exactly(tmp_path, monkeypatch):
    path = overlay_path(tmp_path, "btc")
    previous_path = path.with_name(path.name + ".previous")
    path.write_text('{ "order_size" : 1 }\n')
    previous_path.write_text('{ "order_size" : 0.5 }\n')
    before_live = path.read_bytes()
    before_previous = previous_path.read_bytes()
    real_atomic_write = demo_config._atomic_write
    calls = 0

    def fail_second_write(target, document):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected live-overlay failure")
        return real_atomic_write(target, document)

    monkeypatch.setattr(demo_config, "_atomic_write", fail_second_write)

    with pytest.raises(OSError, match="injected live-overlay failure"):
        write_overrides(
            path, {"order_size": 2}, pair="btc", profiles=PROFILES,
        )

    assert path.read_bytes() == before_live
    assert previous_path.read_bytes() == before_previous


def test_pair_aware_rollback_rejects_invalid_previous_before_mutation(tmp_path):
    path = overlay_path(tmp_path, "btc")
    previous_path = path.with_name(path.name + ".previous")
    path.write_text('{"order_size": 2}')
    previous_path.write_text('{"not_a_real_field": 1}')
    before = path.read_bytes()

    with pytest.raises(OverlayValidationError, match="unknown field"):
        rollback_overrides(path, pair="btc", profiles=PROFILES)

    assert path.read_bytes() == before


def test_desired_digest_is_stable_across_mapping_order():
    assert desired_digest({"order_size": 1.0, "depth_max": 2}) == desired_digest(
        {"depth_max": 2, "order_size": 1.0}
    )


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
    write_overrides(
        overlay_path(tmp_path, "btc"), {"order_size": 42}, pair="btc", profiles=config.TRADING_PROFILES
    )

    demo_cfg = make_cfg("btc", mode="demo")
    assert demo_cfg.order_size == 42.0

    real_cfg = make_cfg("btc", mode="real")
    assert real_cfg.order_size != 42.0
