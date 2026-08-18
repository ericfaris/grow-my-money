"""Regression tests for two anti-whipsaw/anti-noise-trade gates added after
the first paper restart showed heavy fee drag: a minimum hold time before an
ordinary sell can fire (so a position gets a chance to develop before being
whipsawed out), and a hard volatility screen on buys (a liquidity/data-quality
check beyond the 24h-volume floor — see ERA-USD, -7% to -12.6% per trade)."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from src.bot import Bot
from src.indicators import Features
from src.state import iso, utcnow


def _feat(**kw) -> Features:
    base = dict(price=100.0, ema_fast=101.0, ema_slow=100.0, ema_gap_pct=0.01,
                ema_bullish=1, rsi=55.0, macd_line=0.5, macd_signal=0.2,
                macd_hist=0.3, macd_bullish=1, ret_recent=0.02, volatility=0.01)
    base.update(kw)
    return Features(**base)


@pytest.fixture
def bot(config, state, tmp_path):
    b = Bot(config, state, coinbase_client=None, state_dir=tmp_path)
    b._price_cache["BTC-USD"] = 100.0
    return b


def _stub_candles(monkeypatch, feats: Features):
    """_decide_product needs a non-empty candle df and then computes features
    from it — stub both so the gates can be exercised without real market
    data or a live client."""
    monkeypatch.setattr("src.bot.marketdata.fetch_candles",
                        lambda *a, **kw: pd.DataFrame({"close": [1.0] * 30}))
    monkeypatch.setattr("src.bot.compute_features", lambda *a, **kw: feats)


# -- minimum hold time (ordinary sells only) ---------------------------------

def test_sell_suppressed_when_held_below_min_hold(bot, state, config, monkeypatch):
    now = utcnow()
    state.seed_position("BTC-USD", 1.0, 100.0,
                        iso(now - timedelta(hours=config.min_hold_hours - 1)))
    _stub_candles(monkeypatch, _feat(ema_bullish=0))  # cross-down -> sell signal

    action = bot._decide_product("BTC-USD", now=now)

    assert action is None
    assert state.open_positions()["BTC-USD"]["base_size"] == 1.0  # untouched


def test_sell_allowed_once_min_hold_elapsed(bot, state, config, monkeypatch):
    now = utcnow()
    state.seed_position("BTC-USD", 1.0, 100.0,
                        iso(now - timedelta(hours=config.min_hold_hours + 1)))
    _stub_candles(monkeypatch, _feat(ema_bullish=0))  # cross-down -> sell signal

    action = bot._decide_product("BTC-USD", now=now)

    assert action is not None
    assert action["side"] == "sell"
    assert action["status"] == "filled"


def test_sell_with_no_opened_ts_is_not_gated(bot, state, config, monkeypatch):
    """Legacy/edge case: a position with no recorded opened_ts_utc must still
    be sellable (fail open, not fail closed, on missing timestamp data)."""
    now = utcnow()
    state.conn.execute(
        "INSERT INTO positions(product,base_size,avg_entry,opened_ts_utc) "
        "VALUES(?,?,?,?)", ("BTC-USD", 1.0, 100.0, None),
    )
    state.conn.commit()
    _stub_candles(monkeypatch, _feat(ema_bullish=0))

    action = bot._decide_product("BTC-USD", now=now)

    assert action is not None
    assert action["side"] == "sell"


# -- buy volatility screen ----------------------------------------------------

def test_buy_suppressed_above_max_volatility(bot, config, monkeypatch):
    _stub_candles(monkeypatch, _feat(volatility=config.max_buy_volatility + 0.01))

    action = bot._decide_product("BTC-USD", now=utcnow())

    assert action is None


def test_buy_allowed_at_or_below_max_volatility(bot, config, monkeypatch):
    _stub_candles(monkeypatch, _feat(volatility=config.max_buy_volatility))
    monkeypatch.setattr(bot.model, "predict_p_win",
                        lambda feats: (config.buy_probability_threshold + 0.1, "coldstart"))
    monkeypatch.setattr(bot, "_htf_trend", lambda product: True)

    action = bot._decide_product("BTC-USD", now=utcnow())

    assert action is not None
    assert action["side"] == "buy"
