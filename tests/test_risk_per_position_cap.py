"""Acceptance #2: per-position 25% cap resizes (or rejects), never over-approves."""
from __future__ import annotations

from src.risk import RiskManager, TradeIntent


def _mgr(config, state, portfolio, killswitch, price_source):
    return RiskManager(config, state, portfolio, killswitch, price_source)


def test_buy_within_cap_is_approved(config, state, portfolio, killswitch, price_source):
    mgr = _mgr(config, state, portfolio, killswitch, price_source)
    # bankroll 10k, per-position cap 25% = 2500. Buy 1000 with no position.
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "approve"
    assert d.adjusted_notional == 1000.0


def test_oversized_buy_is_resized_to_headroom(config, state, portfolio, killswitch, price_source):
    # existing BTC position worth 2000 (cap is 2500 -> 500 headroom)
    state.record_intent("coid-seed", "BTC-USD", "buy", "paper", 2000.0)
    state.record_fill("coid-seed", "BTC-USD", "buy", "paper",
                      base_size=2000.0 / 60000.0, price=60000.0, notional=2000.0)
    mgr = _mgr(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 4000.0))
    assert d.action == "resize"
    assert abs(d.adjusted_notional - 500.0) < 1e-6
    assert d.adjusted_notional < 4000.0  # never approve full size


def test_no_headroom_rejects(config, state, portfolio, killswitch, price_source):
    # position already at/over cap -> headroom < min order -> reject
    state.record_intent("coid-full", "BTC-USD", "buy", "paper", 2500.0)
    state.record_fill("coid-full", "BTC-USD", "buy", "paper",
                      base_size=2500.0 / 60000.0, price=60000.0, notional=2500.0)
    mgr = _mgr(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "reject"
    assert d.adjusted_notional == 0.0
