"""Acceptance #3: rolling 5-trades-per-24h cap (trailing window, not calendar)."""
from __future__ import annotations

from datetime import timedelta

from src.risk import RiskManager, TradeIntent
from src.state import iso, utcnow


def _seed_trade(state, coid, ts):
    state.record_intent(coid, "BTC-USD", "buy", "paper", 100.0, ts_utc=iso(ts))
    state.record_fill(coid, "BTC-USD", "buy", "paper",
                      base_size=0.001, price=60000.0, notional=100.0, ts_utc=iso(ts))


def test_sixth_trade_in_window_rejected(config, state, portfolio, killswitch, price_source):
    now = utcnow()
    # 5 fills all within the trailing 24h
    for i in range(5):
        _seed_trade(state, f"coid-{i}", now - timedelta(hours=i + 1))
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 100.0), now=now)
    assert d.action == "reject"
    assert "trade cap" in d.reason.lower() or "daily" in d.reason.lower()


def test_trailing_window_frees_up_after_oldest_ages_out(config, state, portfolio,
                                                        killswitch, price_source):
    now = utcnow()
    # 5 fills, but the oldest is 23h ago; advance 'now' by 2h -> that one ages out.
    offsets = [23, 10, 5, 3, 1]
    for i, h in enumerate(offsets):
        _seed_trade(state, f"coid-{i}", now - timedelta(hours=h))
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)

    # At `now`, all 5 are inside the window -> reject.
    d_now = mgr.check(TradeIntent("BTC-USD", "buy", 100.0), now=now)
    assert d_now.action == "reject"

    # 2h later, the 23h-old fill is now 25h old -> only 4 in window -> allowed.
    later = now + timedelta(hours=2)
    d_later = mgr.check(TradeIntent("BTC-USD", "buy", 100.0), now=later)
    assert d_later.action in ("approve", "resize")


def test_flatten_sell_is_exempt_from_count_cap(config, state, portfolio, killswitch, price_source):
    now = utcnow()
    for i in range(5):
        _seed_trade(state, f"coid-{i}", now - timedelta(hours=i + 1))
    # give the bot a position to flatten
    state.record_intent("pos", "BTC-USD", "buy", "paper", 100.0, ts_utc=iso(now - timedelta(hours=30)))
    state.record_fill("pos", "BTC-USD", "buy", "paper", base_size=0.01, price=60000.0,
                      notional=600.0, ts_utc=iso(now - timedelta(hours=30)))
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    intent = TradeIntent("BTC-USD", "sell", 600.0, base_size=0.01, is_flatten=True)
    d = mgr.check(intent, now=now)
    assert d.action in ("approve", "resize")  # exempt despite 5 trades in window
