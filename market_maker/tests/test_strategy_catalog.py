"""Choosing the algorithm: the catalog (strategy/catalog.py), the
``algorithm`` configuration field, the worker building the chosen strategy,
a Demo worker switching at a round boundary, and a live worker started with
the algorithm of its saved configuration."""

from datetime import datetime, timezone

import pytest

import config
import demo_config
import run
from demo_config import overlay_path, write_overrides
from engine import SimpleMarketMaker
from session import WorkerLease
from strategy.algo1 import Algo1Strategy
from strategy.algo2 import Algo2Strategy
from strategy.algo3 import Algo3Strategy
from strategy.algo4 import Algo4Strategy
from strategy.catalog import (
    ALGORITHMS,
    DEFAULT_ALGORITHM,
    SELECTABLE_ALGORITHMS,
    STRATEGIES,
    algorithm_info,
    algorithm_label,
    build_strategy,
    strategy_key,
)

PROFILES = config.TRADING_PROFILES
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


# ---------- the catalog ----------

def test_catalog_lists_algorithms_1_3_and_4():
    assert list(ALGORITHMS) == ["algo1", "algo3", "algo4"]
    assert ALGORITHMS["algo1"].cls is Algo1Strategy and ALGORITHMS["algo3"].cls is Algo3Strategy
    assert ALGORITHMS["algo4"].cls is Algo4Strategy and ALGORITHMS["algo4"].label == "MM_algo3"
    assert [info.number for info in ALGORITHMS.values()] == [1, 3, 4]
    assert SELECTABLE_ALGORITHMS == ("algo1", "algo3", "algo4")
    assert DEFAULT_ALGORITHM == "algo1"


def test_cli_choices_keep_the_algorithm_2_stub():
    assert set(STRATEGIES) == {"algo1", "algo2", "algo3", "algo4"}
    assert STRATEGIES["algo2"] is Algo2Strategy
    assert run.STRATEGIES is STRATEGIES


def test_the_config_field_offers_exactly_the_selectable_algorithms():
    assert demo_config.ALGORITHM_CHOICES == SELECTABLE_ALGORITHMS
    assert demo_config.FIELD_DEFINITIONS["algorithm"].choices == SELECTABLE_ALGORITHMS


def test_every_strategy_class_carries_its_catalog_key():
    for key, info in ALGORITHMS.items():
        assert info.cls.key == key
        assert strategy_key(info.cls(config.MMConfig(**PROFILES["usdt"]))) == key
    assert strategy_key(object()) is None


def test_labels_and_unknown_keys():
    assert algorithm_label("algo3") == "MM_algo2"          # shown under its branch name
    assert algorithm_label("algo1") == "MM_algo1v2"
    # shown as MM_algo2 only; the design note's number stays in `number`, not in the visible title
    assert ALGORITHMS["algo3"].number == 3 and "Algorithm 3" not in ALGORITHMS["algo3"].title
    assert algorithm_info(None).key == "algo1"            # rows written before the Strategy column
    assert algorithm_info("no-such").key == "algo1"


def test_build_strategy_follows_the_config_unless_a_key_is_given():
    cfg = config.MMConfig(**PROFILES["btc"])
    assert isinstance(build_strategy(cfg), Algo1Strategy)
    cfg.algorithm = "algo3"
    assert isinstance(build_strategy(cfg), Algo3Strategy)
    assert isinstance(build_strategy(cfg, "algo1"), Algo1Strategy)
    with pytest.raises(KeyError):
        build_strategy(cfg, "algo9")


@pytest.mark.parametrize("pair", ["btc", "usdt"])
def test_both_profiles_run_algorithm_1_by_default_and_carry_algorithm_3_settings(pair):
    profile = PROFILES[pair]
    assert profile["algorithm"] == "algo1"
    for field in ("algo3_depth_k", "algo3_decay_rho", "algo3_beta", "algo3_half_spread",
                  "algo3_min_volume_orders", "algo3_imbalance_clip"):
        assert field in profile
    assert profile["algo3_beta"] == 0.0           # no lean until beta is estimated from data


# ---------- the worker builds the chosen strategy ----------

def _demo_bot(state_dir, monkeypatch, *, overrides=None, session="catalog-session"):
    state_dir = state_dir.resolve()
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(state_dir))
    if overrides:
        write_overrides(overlay_path(state_dir, "usdt"), overrides, pair="usdt", profiles=PROFILES)
    cfg = run.make_cfg("usdt", mode="demo", session_id=session)
    cfg.output_csv = str(state_dir / "usdt-demo.csv")
    cfg.demo_state_path = str(state_dir / "usdt-ledger.json")
    cfg.demo_fee_state_path = str(state_dir / "fees.json")
    cfg.demo_archive_path = str(state_dir / "usdt-archive.jsonl")
    cfg.demo_result_db_path = str(state_dir / "usdt-results.sqlite3")
    cfg.algo1_diagnostics_path = str(state_dir / "usdtirt_algo1_diagnostics.jsonl")
    bot = run.make_bot(cfg, mode="demo")
    with WorkerLease(state_dir / ".worker_usdtirt.lock", identity="USDTIRT-demo"):
        bot.exchange.ledger.persist_initial_state()
        assert bot.config_sync.reload_at_round_boundary().ok
    return bot, state_dir


def test_a_demo_worker_builds_the_algorithm_its_configuration_names(tmp_path, monkeypatch):
    bot, _ = _demo_bot(tmp_path, monkeypatch, overrides={"algorithm": "algo3"})
    assert isinstance(bot.strategy, Algo3Strategy)
    assert bot._strategy_name() == "algo3"


def test_the_cli_algorithm_overrides_the_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="cli")
    cfg.demo_state_path = str(tmp_path / "ledger.json")
    cfg.demo_fee_state_path = str(tmp_path / "fees.json")
    cfg.demo_archive_path = str(tmp_path / "archive.jsonl")
    bot = run.make_bot(cfg, "algo3", mode="demo")
    assert isinstance(bot.strategy, Algo3Strategy) and cfg.algorithm == "algo3"


def test_a_demo_worker_switches_algorithm_at_the_next_round_boundary(tmp_path, monkeypatch):
    bot, state_dir = _demo_bot(tmp_path, monkeypatch)
    assert isinstance(bot.strategy, Algo1Strategy)

    write_overrides(overlay_path(state_dir, "usdt"), {"algorithm": "algo3", "algo3_beta": 0.001},
                    pair="usdt", profiles=PROFILES)
    assert bot.config_sync.reload_at_round_boundary().ok
    bot._sync_strategy_with_config()

    assert isinstance(bot.strategy, Algo3Strategy)
    assert bot.strategy.cfg is bot.cfg and bot.cfg.algo3_beta == 0.001
    snapshot = demo_config.read_runtime_snapshot(state_dir, "usdt")
    assert snapshot["applied"]["algorithm"] == "algo3"

    write_overrides(overlay_path(state_dir, "usdt"), {"algorithm": "algo1"}, pair="usdt", profiles=PROFILES)
    assert bot.config_sync.reload_at_round_boundary().ok
    bot._sync_strategy_with_config()
    assert isinstance(bot.strategy, Algo1Strategy)


def test_a_stand_in_strategy_is_never_replaced(tmp_path, monkeypatch):
    bot, _ = _demo_bot(tmp_path, monkeypatch)
    stand_in = object()
    bot.strategy = stand_in
    bot.cfg.algorithm = "algo3"
    bot._sync_strategy_with_config()
    assert bot.strategy is stand_in


class _BookFeed:
    """A real-looking USDT/IRT book with more resting bids than asks."""

    def get_orderbook(self, symbol):
        bids = [[str(2_659_990 - 10 * i), str(400.0 - 20 * i)] for i in range(12)]
        asks = [[str(2_660_010 + 10 * i), str(100.0 + 5 * i)] for i in range(12)]
        return {"bids": bids, "asks": asks, "lastTradePrice": "2660000"}


def test_a_demo_round_with_algorithm_3_records_its_signals_and_its_name(tmp_path, monkeypatch):
    bot, state_dir = _demo_bot(tmp_path, monkeypatch, overrides={"algorithm": "algo3", "algo3_beta": 0.001})
    bot.exchange._market_data = _BookFeed()
    bot.cfg.max_rounds = 1
    bot.now_provider = lambda: NOW
    bot.monotonic_provider = lambda: 0.0
    bot.sleep_fn = lambda seconds: None

    bot.run()

    [row] = bot.result_store.load_view().to_dict("records")
    assert row["Strategy"] == "algo3"
    assert row["Quote Status"] == "quote_generated" and row["Quote Mode"] == "pair"
    assert row["Imbalance"] > 0 and row["Microprice"] > 2_660_000          # buyers dominate the top of book
    assert row["Bid Fill Prob"] is None and row["Expected Value"] is None
    assert row["Net Return"] > row["Min Required Return"]
    import json
    with open(bot.cfg.algo1_diagnostics_path, encoding="utf-8") as stream:
        document = json.loads(stream.readline())
    assert document["algorithm"] == "algo3" and document["levels"]


def test_live_rows_name_the_strategy_too():
    assert SimpleMarketMaker.DEMO_COLUMNS.index("Strategy") == SimpleMarketMaker.DEMO_COLUMNS.index("Algorithm") + 1
