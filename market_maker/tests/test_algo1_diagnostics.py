import json

import pytest

import run
from helpers import make_bot, make_orderbook


def _usdt_book():
    bids = [[2_659_990, "20000"], [2_659_900, "300"], [2_658_000, "1500"], [2_655_000, "8000"],
            [2_650_000, "40000"], [2_640_000, "10000"], [2_630_000, "5000"], [2_620_000, "5000"],
            [2_610_000, "5000"], [2_600_000, "5000"], [2_590_000, "5000"]]
    asks = [[2_660_010, "5000"], [2_660_100, "400"], [2_662_000, "2000"], [2_665_000, "6000"],
            [2_670_000, "30000"], [2_680_000, "12000"], [2_690_000, "5000"], [2_700_000, "5000"],
            [2_710_000, "5000"], [2_720_000, "5000"], [2_730_000, "5000"]]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2660000"}


def test_diagnostic_describes_candidates_pairs_and_the_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.commission_perc = 0.0025
    cfg.exp_ret_min = 0.0025
    cfg.skew_perc = 0.0
    bot = make_bot(cfg)

    quote = bot.strategy.decide_order(_usdt_book(), 25.0)
    diagnostic = bot.strategy.diagnostics(_usdt_book(), 25.0, round_no=23, session_id=cfg.session)

    assert diagnostic["algorithm_version"] == 2
    assert diagnostic["inventory_position"] == 0.5
    assert diagnostic["inv_d"] == 0                       # legacy staircase value
    assert diagnostic["centre"] == pytest.approx(2_660_000.0)
    assert diagnostic["mid_price"] == pytest.approx(2_660_000.0)
    assert diagnostic["commission_perc"] == 0.0025
    assert diagnostic["exp_ret_min"] == diagnostic["required_threshold"] == diagnostic["min_required_return"] == 0.0025
    assert diagnostic["gross_bar"] == pytest.approx(0.00753, abs=2e-5)
    assert diagnostic["distance_grid"][0] == pytest.approx(cfg.grid_min_perc)
    assert [c["price"] for c in diagnostic["candidate_bids"]][:2] == [2_658_010.0, 2_655_010.0]
    assert all({"side", "price", "distance", "level", "anchored", "fill_probability"} <= set(c) for c in diagnostic["candidate_bids"])
    assert len(diagnostic["candidate_results"]) == len(diagnostic["candidate_bids"]) * len(diagnostic["candidate_asks"])
    first = diagnostic["candidate_results"][0]
    for key in ("bid_level", "ask_level", "bid_price", "ask_price", "submitted_bid_price",
                "submitted_ask_price", "last_price", "mid_price", "spread_ratio",
                "relative_quoted_spread", "gross_return", "net_return", "p_bid", "p_ask",
                "expected_value", "required_threshold", "min_required_return", "passed"):
        assert key in first
    assert diagnostic["quote_mode"] == "pair"
    assert diagnostic["selection_reason"] == "pair_selected"
    assert diagnostic["selected_bid_price"] == quote["bid"]
    assert diagnostic["selected_ask_price"] == quote["ask"]
    assert diagnostic["selected_bid_level"] == quote["bid level"]
    assert diagnostic["fallback_detected"] is False
    assert diagnostic["fill_model"]["formula_kappa"] == cfg.fill_kappa
    winner = max((r for r in diagnostic["candidate_results"] if r["passed"]), key=lambda r: r["expected_value"])
    assert winner["submitted_bid_price"] == quote["bid"]


def test_diagnostic_is_pure_and_json_serialisable(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    bot = make_bot(cfg)
    book = make_orderbook(n=11, last=100)
    before = bot.strategy.cost_basis(), dict(bot.strategy._resting)
    document = bot.strategy.diagnostics(book, 0.0, round_no=1, session_id=cfg.session)
    json.dumps(document, sort_keys=True)
    assert (bot.strategy.cost_basis(), dict(bot.strategy._resting)) == before


def test_shallow_book_diagnostic_reports_the_available_depth(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    bot = make_bot(cfg)

    diagnostic = bot.strategy.diagnostics(
        make_orderbook(n=4, last=100), 0.5, round_no=1, session_id=cfg.session
    )
    assert diagnostic["depth_max_available"] == 3

    two_levels = {
        "bids": [["2000000", "1"], ["1999000", "1"]],
        "asks": [["2000020", "1"], ["2001020", "1"]],
        "lastTradePrice": "2000010",
    }
    diagnostic = bot.strategy.diagnostics(two_levels, 0.5, round_no=1, session_id=cfg.session)
    assert diagnostic["depth_max_available"] == 1
    assert diagnostic["candidate_bids"]                 # synthetic candidates beyond the book
    assert any(not c["anchored"] for c in diagnostic["candidate_bids"])


def test_single_level_book_diagnostic_names_the_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    bot = make_bot(cfg)
    book = {"bids": [["100", "1"]], "asks": [["101", "1"]], "lastTradePrice": "100"}

    diagnostic = bot.strategy.diagnostics(book, 0.5, round_no=1, session_id=cfg.session)

    assert diagnostic["selection_reason"] == "invalid_or_single_level_order_book"
    assert diagnostic["candidate_results"] == []
    assert bot.strategy.no_quote_reason(book, 0.5) == "invalid_or_single_level_order_book"


def test_jsonl_logging_is_deterministic_and_silent_when_disabled(monkeypatch, tmp_path):
    """The trading profiles enable the JSONL (the dashboard's Quote decision
    tab reads it); switching it off must stop the writes, and the document
    must be byte-for-byte reproducible for the same inputs."""
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.algo1_diagnostics_path = str(tmp_path / "algo1.jsonl")
    cfg.algo1_diagnostics_enabled = False
    bot = make_bot(cfg)
    bot.pair = cfg.pair
    bot.session = cfg.session
    book = make_orderbook(n=11, last=100)

    bot._emit_algo1_diagnostic(book, 0.0, round_no=7)
    assert not (tmp_path / "algo1.jsonl").exists()

    cfg.algo1_diagnostics_enabled = True
    bot._emit_algo1_diagnostic(book, 0.0, round_no=7)
    first = (tmp_path / "algo1.jsonl").read_text()
    (tmp_path / "algo1.jsonl").unlink()
    bot._emit_algo1_diagnostic(book, 0.0, round_no=7)
    second = (tmp_path / "algo1.jsonl").read_text()

    assert first == second
    document = json.loads(first)
    assert document["symbol"] == "USDTIRT"
    assert document["session_id"] == "shared-session"
    assert "token" not in first.lower()


def test_jsonl_is_rotated_to_the_newest_keep_rounds(monkeypatch, tmp_path):
    """Each document holds every scored pair (tens of KB), so the file is
    rewritten with the newest ``algo1_diagnostics_keep_rounds`` documents
    once it holds twice that many; the dashboard reads the tail."""
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    path = tmp_path / "algo1.jsonl"
    cfg.algo1_diagnostics_path = str(path)
    cfg.algo1_diagnostics_enabled = True
    cfg.algo1_diagnostics_keep_rounds = 4
    bot = make_bot(cfg)
    bot.pair = cfg.pair
    bot.session = cfg.session
    book = make_orderbook(n=11, last=100)

    for round_no in range(1, 9):
        bot._emit_algo1_diagnostic(book, 0.0, round_no=round_no)
    assert len(path.read_text().splitlines()) == 8          # 2 x keep: still untouched

    bot._emit_algo1_diagnostic(book, 0.0, round_no=9)
    lines = path.read_text().splitlines()
    assert [json.loads(line)["round"] for line in lines] == [6, 7, 8, 9]

    bot._emit_algo1_diagnostic(book, 0.0, round_no=10)
    assert [json.loads(line)["round"] for line in path.read_text().splitlines()] == [6, 7, 8, 9, 10]
    assert not list(tmp_path.glob("*.tmp"))


def test_no_quote_reason_distinguishes_the_bar_from_the_floor(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.commission_perc = 0.2
    cfg.exp_ret_min = 0.2
    bot = make_bot(cfg)
    assert bot.strategy.no_quote_reason(_usdt_book(), 25.0) == "no_side_clears_bar"

    cfg.commission_perc = 0.0025
    cfg.exp_ret_min = 0.0025
    cfg.ev_min = 0.9
    assert bot.strategy.no_quote_reason(_usdt_book(), 25.0) == "expected_value_below_floor"
    cfg.ev_min = 0.0
    assert bot.strategy.no_quote_reason(_usdt_book(), 25.0) == "quote_available"
