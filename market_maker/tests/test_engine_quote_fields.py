"""The nine Algorithm 1 v2 decision fields the engine writes into every round
row (demo store and live CSV) for the dashboard's Quote decision and Fill
model tabs."""

from engine import SimpleMarketMaker


def test_quote_extra_fields_map_the_strategy_output_in_column_order():
    quote = {
        "quote mode": "bid_only", "selection reason": "bid_only_inventory_edge", "centre": 100.0,
        "bid distance": 0.01, "ask distance": None, "bid exec prob": 0.4, "ask exec prob": None,
        "expected value": 0.001, "reference price": 99.0, "bid": 99.0, "ask": None,
    }
    fields = SimpleMarketMaker._quote_extra_fields(quote)
    assert list(fields) == SimpleMarketMaker.QUOTE_EXTRA_COLUMNS
    assert fields["Quote Mode"] == "bid_only"
    assert fields["Selection Reason"] == "bid_only_inventory_edge"
    assert fields["Centre"] == 100.0
    assert fields["Bid Distance"] == 0.01 and fields["Ask Distance"] is None
    assert fields["Bid Fill Prob"] == 0.4 and fields["Ask Fill Prob"] is None
    assert fields["Expected Value"] == 0.001
    assert fields["Reference Price"] == 99.0


def test_quote_extra_fields_are_none_without_a_quote():
    fields = SimpleMarketMaker._quote_extra_fields(None)
    assert list(fields) == SimpleMarketMaker.QUOTE_EXTRA_COLUMNS
    assert all(value is None for value in fields.values())
    assert all(value is None for value in SimpleMarketMaker._quote_extra_fields({}).values())


def test_demo_columns_end_with_the_decision_fields():
    assert SimpleMarketMaker.DEMO_COLUMNS[-9:] == SimpleMarketMaker.QUOTE_EXTRA_COLUMNS
    assert len(set(SimpleMarketMaker.DEMO_COLUMNS)) == len(SimpleMarketMaker.DEMO_COLUMNS)
