import json

import run
from helpers import make_bot, make_orderbook


def test_zero_inventory_usdt_diagnostic_classifies_depth_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.exp_ret_min = cfg.commission_perc
    bot = make_bot(cfg)
    book = {
        "bids": [["100", "1"] for _ in range(11)],
        "asks": [["100.1", "1"] for _ in range(11)],
        "lastTradePrice": "100",
    }

    diagnostic = bot.strategy.diagnostics(book, 0.0, round_no=23, session_id=cfg.session)

    assert diagnostic["inv_d"] == -5
    assert diagnostic["candidate_pairs"] == [[5, 10], [4, 9], [3, 8], [2, 7], [1, 6]]
    assert diagnostic["commission_perc"] == 0.001
    assert diagnostic["exp_ret_min"] == 0.001
    assert diagnostic["required_threshold"] == 0.002
    assert diagnostic["selected_bid_level"] is None
    assert diagnostic["selected_ask_level"] is None
    assert diagnostic["fallback_bid_level"] == 10
    assert diagnostic["fallback_ask_level"] == 10
    assert diagnostic["fallback_detected"] is True
    assert diagnostic["selection_reason"] == "no_candidate_passed_fallback_rejected"
    assert diagnostic["candidate_results"][0]["bid_price"] == 100.0
    assert diagnostic["candidate_results"][0]["ask_price"] == 100.1
    assert diagnostic["candidate_results"][0]["last_price"] == 100.0
    assert diagnostic["candidate_results"][0]["required_threshold"] == 0.002


def test_shallow_book_diagnostic_documents_unexpressible_inventory_skew(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    bot = make_bot(cfg)

    diagnostic = bot.strategy.diagnostics(
        make_orderbook(n=4, last=100), 0.0, round_no=1, session_id=cfg.session
    )

    assert diagnostic["depth_max_available"] == 3
    assert diagnostic["candidate_pairs"] == []
    assert diagnostic["fallback_detected"] is True
    assert diagnostic["selection_reason"] == "inventory_skew_unexpressible_in_shallow_book"


def test_actual_candidate_selection_is_not_classified_as_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.commission_perc = 0.0001
    cfg.exp_ret_min = 0.0001
    bot = make_bot(cfg)
    book = make_orderbook(n=11, last=100)

    quote = bot.strategy.decide_order(book, 0.0)
    diagnostic = bot.strategy.diagnostics(book, 0.0, round_no=2, session_id=cfg.session)

    assert diagnostic["fallback_detected"] is False
    assert diagnostic["selection_reason"] == "candidate_selected"
    assert diagnostic["selected_bid_level"] == quote["bid level"]
    assert diagnostic["selected_ask_level"] == quote["ask level"]


def test_optional_jsonl_logging_is_deterministic_and_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    cfg.algo1_diagnostics_path = str(tmp_path / "algo1.jsonl")
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


def test_no_quote_reason_distinguishes_shallow_skew_from_profitability(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "DEMO_STATE_DIR", str(tmp_path))
    cfg = run.make_cfg("usdt", mode="demo", session_id="shared-session")
    bot = make_bot(cfg)

    assert bot.strategy.no_quote_reason(make_orderbook(n=4), 0.0) == (
        "inventory_skew_unexpressible_in_shallow_book"
    )
    # Book is flat at every level, so even the loosest inventory-skewed
    # candidate never clears the profitability bar -- the root-cause fix
    # (reports/batch-fill-definitive-root-cause.md) surfaces this under its
    # own name rather than the generic profitability_threshold, since it
    # used to be the silent (depth_max, depth_max) fallback path.
    tight = {
        "bids": [["100", "1"] for _ in range(11)],
        "asks": [["100.1", "1"] for _ in range(11)],
        "lastTradePrice": "100",
    }
    assert bot.strategy.no_quote_reason(tight, 0.0) == (
        "no_profitable_inventory_skewed_candidate"
    )
