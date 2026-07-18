"""Acceptance #4: -30% portfolio halt (below 0.70x bankroll) rejects buys,
persists the trip flag, and stays tripped until a human resume."""
from __future__ import annotations

import logging

from src.risk import HALT_FLAG, RiskManager, TradeIntent
from tests.conftest import FakePriceSource


def test_halt_trips_when_value_below_threshold(config, state, portfolio, killswitch, caplog):
    # bankroll 10k, halt at 70% = 7000. Give the bot a position worth < that.
    # position: 0.1 BTC, but current price crashed to 50k -> value 5000 (+ 0 cash seeded).
    # We seed cash below threshold by simulating a drawdown: paper_cash=2000, pos worth 4000.
    state.set_runtime("paper_cash", 2000.0)
    state.record_intent("p", "BTC-USD", "buy", "paper", 4000.0)
    state.record_fill("p", "BTC-USD", "buy", "paper", base_size=0.08, price=50000.0,
                      notional=4000.0)
    ps = FakePriceSource({"BTC-USD": 50000.0})  # value = 2000 + 0.08*50000 = 6000 < 7000
    mgr = RiskManager(config, state, portfolio, killswitch, ps)
    with caplog.at_level(logging.WARNING):
        d = mgr.check(TradeIntent("BTC-USD", "buy", 100.0))
    assert d.action == "reject"
    assert state.is_cap_tripped(HALT_FLAG) is True
    assert any("HALT" in r.message.upper() for r in caplog.records)


def test_halt_stays_tripped_until_resume(config, state, portfolio, killswitch, price_source):
    # Manually trip, then even a healthy portfolio still rejects buys until cleared.
    state.set_cap_trip(HALT_FLAG, True)
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 100.0))
    assert d.action == "reject"
    assert "halt" in d.reason.lower()

    # human resume clears it
    state.set_cap_trip(HALT_FLAG, False)
    d2 = mgr.check(TradeIntent("BTC-USD", "buy", 100.0))
    assert d2.action in ("approve", "resize")


def test_fail_closed_when_price_unavailable(config, state, portfolio, killswitch):
    # price source raises -> total_value can't be computed -> reject (fail closed)
    ps = FakePriceSource({})  # no BTC price
    state.record_intent("p", "BTC-USD", "buy", "paper", 100.0)
    state.record_fill("p", "BTC-USD", "buy", "paper", base_size=0.01, price=60000.0,
                      notional=600.0)
    mgr = RiskManager(config, state, portfolio, killswitch, ps)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 100.0))
    assert d.action == "reject"
    assert "fail-closed" in d.reason.lower()
