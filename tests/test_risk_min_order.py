"""Regression: RiskConfig.min_order_usd was validated and documented as a hard
safety floor, but its only consumer (the per-position-cap resize path) was
removed in 80a3f19, leaving it completely unenforced. This tests the
standalone floor check that replaces it."""
from __future__ import annotations

from src.risk import RiskManager, TradeIntent
from src.state import utcnow


def test_dust_buy_rejected_below_min_order(config, state, portfolio, killswitch, price_source):
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", config.risk.min_order_usd - 1.0))
    assert d.action == "reject"
    assert "minimum order" in d.reason.lower()


def test_buy_at_or_above_min_order_allowed(config, state, portfolio, killswitch, price_source):
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", config.risk.min_order_usd + 1.0))
    assert d.action in ("approve", "resize")


def test_ordinary_dust_sell_rejected(config, state, portfolio, killswitch, price_source):
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "sell", config.risk.min_order_usd - 1.0,
                              base_size=0.0001))
    assert d.action == "reject"


def test_flatten_sell_exempt_from_min_order_floor(config, state, portfolio, killswitch,
                                                   price_source):
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "sell", config.risk.min_order_usd - 1.0,
                              base_size=0.0001, is_flatten=True))
    assert d.action in ("approve", "resize")


def test_sentiment_dampening_below_floor_is_rejected(config, state, portfolio, killswitch,
                                                       price_source):
    """A buy that clears the floor pre-dampening but gets shrunk below it by the
    sentiment dampener must still be rejected by the floor check."""
    class BearishStub:
        def lean(self, product):
            class Res:
                score = -0.9
                panic = 80.0
            return Res()

    config = config.__class__(**{
        **config.__dict__,
        "sentiment_dampen_score": 0.0,
        "sentiment_veto_score": -1.0,
        "sentiment_dampen_factor": 0.05,
    })
    notional = config.risk.min_order_usd + 1.0  # clears floor pre-dampening
    mgr = RiskManager(config, state, portfolio, killswitch, price_source,
                      sentiment=BearishStub())
    d = mgr.check(TradeIntent("BTC-USD", "buy", notional))
    assert d.action == "reject"
    assert "minimum order" in d.reason.lower()
