"""``update_filled_amounts`` on the live worker's order table.

The live worker loads its table with ``pandas.read_csv``. Since pandas 3 a
column of client order ids is typed ``str``, and the matched amount (a float)
that replaces the id used to raise ``TypeError: Invalid value ... for dtype
'str'`` and end the worker (BTC live worker, 17 October 2026 11:34 UTC)."""
import io

import pandas as pd
import pytest

from engine import SimpleMarketMaker


class _Stub:
    """Only what update_filled_amounts touches on the bot."""

    def __init__(self, closed_orders, tracked):
        self._closed = closed_orders
        self.tracked_orders = tracked

    def get_status_orders(self, status):
        assert status == "close"
        return self._closed


def _table_from_csv():
    text = (
        "Buy Order ID,Buy Status,Sell Order ID,Sell Status,Buy Size\n"
        "o-1,usdtirt-buy-7-1,o-2,usdtirt-sell-7-1,10\n"
        "o-3,no_quote,,,0\n"
    )
    return pd.read_csv(io.StringIO(text))


def _closed():
    return [
        {"clientOrderId": "usdtirt-buy-7-1", "type": "buy", "matchedAmount": 2.1e-05,
         "unmatchedAmount": 0.0, "status": "done"},
        {"clientOrderId": "usdtirt-sell-7-1", "type": "sell", "matchedAmount": 4.0,
         "unmatchedAmount": 6.0, "status": "open"},
        {"clientOrderId": "foreign-1", "type": "buy", "matchedAmount": 1.0,
         "unmatchedAmount": 0.0, "status": "done"},
    ]


def test_matched_amount_replaces_the_client_id_in_a_table_loaded_from_csv():
    table = _table_from_csv()
    stub = _Stub(_closed(), {"buy": {"usdtirt-buy-7-1"}, "sell": {"usdtirt-sell-7-1"}})

    history = SimpleMarketMaker.update_filled_amounts(
        stub, table, {"usdtirt-buy-7-1", "usdtirt-sell-7-1"})

    assert table.loc[0, "Buy Status"] == pytest.approx(2.1e-05)
    assert table.loc[0, "Sell Status"] == pytest.approx(4.0)
    assert table.loc[1, "Buy Status"] == "no_quote"
    assert table["Buy Status"].dtype == object and table["Sell Status"].dtype == object
    assert history == {"usdtirt-buy-7-1": [2.1e-05], "usdtirt-sell-7-1": [4.0]}
    # fully filled ids leave the tracked set, a partial fill stays tracked
    assert stub.tracked_orders == {"buy": set(), "sell": {"usdtirt-sell-7-1"}}


def test_an_untracked_or_unknown_id_changes_nothing():
    table = _table_from_csv()
    stub = _Stub(_closed(), {"buy": set(), "sell": set()})

    history = SimpleMarketMaker.update_filled_amounts(stub, table, set())

    assert history == {}
    assert table.loc[0, "Buy Status"] == "usdtirt-buy-7-1"
    assert table.loc[0, "Sell Status"] == "usdtirt-sell-7-1"


def test_amount_written_after_a_csv_round_trip_too(tmp_path):
    """The next start reloads a table that already mixes ids and amounts."""
    table = _table_from_csv()
    stub = _Stub(_closed(), {"buy": {"usdtirt-buy-7-1"}, "sell": set()})
    SimpleMarketMaker.update_filled_amounts(stub, table, {"usdtirt-buy-7-1"})
    path = tmp_path / "track.csv"
    table.to_csv(path, index=False)

    reloaded = pd.read_csv(path)
    stub = _Stub(_closed(), {"buy": set(), "sell": {"usdtirt-sell-7-1"}})
    SimpleMarketMaker.update_filled_amounts(stub, reloaded, {"usdtirt-sell-7-1"})

    assert reloaded.loc[0, "Sell Status"] == pytest.approx(4.0)
    assert float(reloaded.loc[0, "Buy Status"]) == pytest.approx(2.1e-05)
