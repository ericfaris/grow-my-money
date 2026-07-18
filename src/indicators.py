"""Pure technical-indicator functions over a candle DataFrame.

Input DataFrame is expected to have a ``close`` column ordered oldest->newest
(and optionally ``high``/``low``/``volume``). All functions are side-effect free
so they are trivially unit-testable against hand-checked fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing == EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # when avg_loss == 0 (all gains) RSI = 100
    out = out.where(avg_loss != 0.0, 100.0)
    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram)."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


@dataclass
class Features:
    price: float
    ema_fast: float
    ema_slow: float
    ema_gap_pct: float          # (ema_fast - ema_slow)/ema_slow
    ema_bullish: int            # 1 if fast > slow
    rsi: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_bullish: int           # 1 if macd_line > signal
    ret_recent: float           # last-N-bar return
    volatility: float           # rolling std of returns

    def to_vector(self) -> dict:
        return asdict(self)


def compute_features(df: pd.DataFrame, *, ema_fast: int = 12, ema_slow: int = 26,
                     rsi_period: int = 14, macd_fast: int = 12, macd_slow: int = 26,
                     macd_signal: int = 9, ret_lookback: int = 6,
                     vol_lookback: int = 24) -> Features:
    """Compute the latest feature vector from a candle DataFrame.

    Raises ValueError if there is not enough data to compute the slow EMA.
    """
    close = df["close"].astype(float).reset_index(drop=True)
    if len(close) < ema_slow + 1:
        raise ValueError(
            f"need >= {ema_slow + 1} candles, got {len(close)}"
        )
    ef = ema(close, ema_fast)
    es = ema(close, ema_slow)
    r = rsi(close, rsi_period)
    ml, sl, hist = macd(close, macd_fast, macd_slow, macd_signal)

    price = float(close.iloc[-1])
    ema_f = float(ef.iloc[-1])
    ema_s = float(es.iloc[-1])
    gap = (ema_f - ema_s) / ema_s if ema_s else 0.0

    rsi_val = float(r.iloc[-1]) if not pd.isna(r.iloc[-1]) else 50.0

    rets = close.pct_change()
    ret_recent = float(close.iloc[-1] / close.iloc[-1 - ret_lookback] - 1.0) \
        if len(close) > ret_lookback else 0.0
    vol = float(rets.tail(vol_lookback).std()) if len(rets.dropna()) else 0.0
    if np.isnan(vol):
        vol = 0.0

    return Features(
        price=price,
        ema_fast=ema_f,
        ema_slow=ema_s,
        ema_gap_pct=gap,
        ema_bullish=1 if ema_f > ema_s else 0,
        rsi=rsi_val,
        macd_line=float(ml.iloc[-1]),
        macd_signal=float(sl.iloc[-1]),
        macd_hist=float(hist.iloc[-1]),
        macd_bullish=1 if float(ml.iloc[-1]) > float(sl.iloc[-1]) else 0,
        ret_recent=ret_recent,
        volatility=vol,
    )
