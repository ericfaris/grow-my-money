"""Acceptance #1: paper mode routes to the simulator and NEVER calls a real
order endpoint."""
from __future__ import annotations

import pytest

from src.execution import OrderGateway
from src.risk import RiskManager, TradeIntent
from tests.conftest import FakePriceSource


class SpyClient:
    """Records any live-order calls so tests can assert they never happen."""

    def __init__(self):
        self.buys = []
        self.sells = []

    def place_market_buy(self, client_order_id, product_id, quote_size):
        self.buys.append((client_order_id, product_id, quote_size))
        return {"success": True, "order_id": "x"}

    def place_market_sell(self, client_order_id, product_id, base_size):
        self.sells.append((client_order_id, product_id, base_size))
        return {"success": True, "order_id": "x"}

    def get_spot_price(self, product_id):
        return 60000.0


def _gateway(config, state, killswitch, price_source, client):
    risk = RiskManager(config, state, portfolio=_Portfolio(state, config), killswitch=killswitch,
                       price_source=price_source)
    return OrderGateway(config, state, risk, price_source, coinbase_client=client)


class _Portfolio:
    def __init__(self, state, config):
        from src.portfolio import Portfolio
        self.p = Portfolio(state, config.paper_start_bankroll)

    @property
    def bankroll(self):
        return self.p.bankroll

    def position_value(self, product, ps):
        return self.p.position_value(product, ps)

    def total_value(self, ps):
        return self.p.total_value(ps)


def test_paper_fill_never_calls_live(config, state, killswitch, price_source):
    state.set_mode("paper")
    client = SpyClient()
    gw = _gateway(config, state, killswitch, price_source, client)
    res = gw.execute(TradeIntent("BTC-USD", "buy", 500.0))
    assert res["status"] == "filled"
    assert client.buys == []  # NEVER touched the live buy endpoint
    assert client.sells == []
    # a simulated trade was recorded
    assert len(state.all_trades()) == 1
    assert state.all_trades()[0]["mode"] == "paper"


def test_paper_large_intent_fills_in_full(config, state, killswitch, price_source):
    state.set_mode("paper")
    client = SpyClient()
    gw = _gateway(config, state, killswitch, price_source, client)
    res = gw.execute(TradeIntent("BTC-USD", "buy", 9000.0))
    assert res["status"] == "filled"
    assert res["decision"].action == "approve"
    trade = state.all_trades()[0]
    assert abs(trade["notional"] - 9000.0) < 5.0  # slippage rounding only


def test_paper_slippage_applied(config, state, killswitch):
    state.set_mode("paper")
    ps = FakePriceSource({"BTC-USD": 60000.0})
    client = SpyClient()
    gw = _gateway(config, state, killswitch, ps, client)
    gw.execute(TradeIntent("BTC-USD", "buy", 600.0))
    fill_price = state.all_trades()[0]["price"]
    assert fill_price > 60000.0  # buy pays up by slippage
