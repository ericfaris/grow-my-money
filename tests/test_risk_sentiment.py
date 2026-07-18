"""Acceptance #3 & #4: the fail-OPEN sentiment step dampens/vetoes buys (with a
visible reason) but can NEVER become a fail-closed reject, and never touches
sells or the four hard checks."""
from __future__ import annotations

from src.risk import RiskManager, TradeIntent
from src.sentiment import SentimentResult


class _StubSentiment:
    """Canned lean() — returns a result, None, or raises."""

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises

    def lean(self, product):  # noqa: ARG002
        if self._raises:
            raise RuntimeError("provider blew up")
        return self._result


def _mgr(config, state, portfolio, killswitch, price_source, sentiment):
    return RiskManager(config, state, portfolio, killswitch, price_source,
                       sentiment=sentiment)


def test_strongly_bearish_buy_is_rejected(config, state, portfolio, killswitch, price_source):
    stub = _StubSentiment(SentimentResult("BTC-USD", score=-0.8, panic=82, n_posts=5))
    mgr = _mgr(config, state, portfolio, killswitch, price_source, stub)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "reject"
    assert d.adjusted_notional == 0.0
    assert "sentiment" in d.reason.lower()


def test_moderately_bearish_buy_is_resized(config, state, portfolio, killswitch, price_source):
    stub = _StubSentiment(SentimentResult("BTC-USD", score=-0.4, panic=None, n_posts=3))
    mgr = _mgr(config, state, portfolio, killswitch, price_source, stub)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "resize"
    assert d.adjusted_notional == 1000.0 * config.sentiment_dampen_factor
    assert "sentiment" in d.reason.lower()


def test_lean_none_buy_approved_unchanged(config, state, portfolio, killswitch, price_source):
    mgr = _mgr(config, state, portfolio, killswitch, price_source, _StubSentiment(None))
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "approve"
    assert d.adjusted_notional == 1000.0


def test_lean_raises_buy_approved_unchanged(config, state, portfolio, killswitch, price_source):
    # THE critical case: a provider bug must NOT become a fail-closed reject.
    mgr = _mgr(config, state, portfolio, killswitch, price_source,
               _StubSentiment(raises=True))
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "approve"
    assert d.adjusted_notional == 1000.0


def test_sell_unaffected_by_bearish_sentiment(config, state, portfolio, killswitch, price_source):
    stub = _StubSentiment(SentimentResult("BTC-USD", score=-0.9, panic=90, n_posts=5))
    mgr = _mgr(config, state, portfolio, killswitch, price_source, stub)
    d = mgr.check(TradeIntent("BTC-USD", "sell", 1000.0, base_size=0.01))
    assert d.action == "approve"
    assert d.adjusted_notional == 1000.0


def test_inert_when_sentiment_none(config, state, portfolio, killswitch, price_source):
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 1000.0))
    assert d.action == "approve"
    assert d.adjusted_notional == 1000.0
