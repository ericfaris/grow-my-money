"""Acceptance: indicator math against hand-checked values."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.indicators import compute_features, ema, macd, rsi


def test_ema_hand_checked():
    # EMA(span=3, adjust=False) of [1,2,3,4,5], alpha=2/(3+1)=0.5:
    # 1, 1.5, 2.25, 3.125, 4.0625
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ema(s, 3)
    assert out.iloc[-1] == pytest.approx(4.0625)
    assert out.iloc[1] == pytest.approx(1.5)


def test_ema_constant_series_is_constant():
    s = pd.Series([7.0] * 30)
    assert ema(s, 12).iloc[-1] == pytest.approx(7.0)


def test_rsi_all_gains_is_100():
    s = pd.Series(np.arange(1, 40, dtype=float))  # strictly increasing
    assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    s = pd.Series(np.arange(40, 1, -1, dtype=float))  # strictly decreasing
    assert rsi(s, 14).iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_bounds():
    rng = np.random.default_rng(1)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    r = rsi(s, 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_macd_constant_series_is_zero():
    s = pd.Series([50.0] * 60)
    ml, sl, hist = macd(s)
    assert ml.iloc[-1] == pytest.approx(0.0)
    assert hist.iloc[-1] == pytest.approx(0.0)


def test_macd_uptrend_positive():
    s = pd.Series(np.arange(1, 80, dtype=float))
    ml, sl, hist = macd(s)
    assert ml.iloc[-1] > 0  # fast EMA above slow in a steady uptrend


def test_compute_features_uptrend():
    close = np.arange(1, 80, dtype=float)
    df = pd.DataFrame({"close": close})
    f = compute_features(df)
    assert f.ema_bullish == 1
    assert f.macd_bullish == 1
    assert f.rsi == pytest.approx(100.0)
    assert f.price == pytest.approx(79.0)
    assert set(f.to_vector().keys()) >= {"ema_gap_pct", "rsi", "macd_hist", "volatility"}


def test_compute_features_insufficient_data_raises():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        compute_features(df)
