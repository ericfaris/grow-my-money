"""Regression: nothing gated a paper buy against actual available cash — the
per-trade budget is a fixed fraction of the ORIGINAL bankroll (see bot.py),
not of remaining cash, so repeated buys could (and did) drive paper cash
negative with no floor. This tests the cash-sufficiency check that closes
that gap."""
from __future__ import annotations

from src.risk import RiskManager, TradeIntent
from src.state import iso, utcnow


def _pad_with_eth_position(state, base_size=3.0):
    """Hold enough ETH (priced at $3,000 in the `prices` fixture) that
    total portfolio value stays clear of the 70% drawdown halt regardless of
    how low BTC-side cash is set — isolating the cash-sufficiency check from
    the (separately tested) portfolio-halt check."""
    state.seed_position("ETH-USD", base_size, 3_000.0, iso(utcnow()))


def test_buy_within_cash_is_approved(config, state, portfolio, killswitch, price_source):
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 500.0))
    assert d.action in ("approve", "resize")


def test_buy_exceeding_cash_is_resized_to_available(config, state, portfolio, killswitch,
                                                      price_source):
    _pad_with_eth_position(state)
    state.set_runtime("paper_cash", 100.0)
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 500.0))
    assert d.action == "resize"
    fee_rate = config.fee_bps / 10_000.0
    assert d.adjusted_notional * (1.0 + fee_rate) <= 100.0 + 1e-6


def test_buy_rejected_when_cash_below_min_order(config, state, portfolio, killswitch,
                                                  price_source):
    _pad_with_eth_position(state)
    state.set_runtime("paper_cash", config.risk.min_order_usd - 1.0)
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 500.0))
    assert d.action == "reject"
    assert "cash" in d.reason.lower()


def test_buy_rejected_when_cash_already_negative(config, state, portfolio, killswitch,
                                                   price_source):
    _pad_with_eth_position(state)
    state.set_runtime("paper_cash", -50.0)
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 500.0))
    assert d.action == "reject"


def test_sell_not_gated_by_cash_check(config, state, portfolio, killswitch, price_source):
    _pad_with_eth_position(state)
    state.set_runtime("paper_cash", -50.0)
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "sell", 500.0, base_size=0.01))
    assert d.action in ("approve", "resize")
