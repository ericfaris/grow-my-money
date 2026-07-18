"""Candle fetch -> pandas DataFrame per product.

Turns the Coinbase client's raw candle dicts into an oldest->newest DataFrame
with float OHLCV columns, ready for ``indicators.compute_features``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from .state import utcnow

log = logging.getLogger(__name__)

# Coinbase Advanced granularity -> seconds per candle
GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


def fetch_candles(client, product_id: str, granularity: str = "ONE_HOUR",
                  limit: int = 300) -> pd.DataFrame:
    """Fetch recent candles and return an oldest->newest OHLCV DataFrame."""
    secs = GRANULARITY_SECONDS.get(granularity, 3600)
    end = utcnow()
    start = end - timedelta(seconds=secs * (limit + 2))
    raw = client.get_candles(
        product_id=product_id,
        granularity=granularity,
        start=str(int(start.timestamp())),
        end=str(int(end.timestamp())),
        limit=limit,
    )
    return candles_to_df(raw)


def candles_to_df(raw: list[dict]) -> pd.DataFrame:
    """Normalize a list of candle dicts into a sorted OHLCV DataFrame."""
    rows = []
    for c in raw:
        rows.append(
            {
                "start": int(c.get("start", 0)),
                "low": float(c.get("low", "nan")),
                "high": float(c.get("high", "nan")),
                "open": float(c.get("open", "nan")),
                "close": float(c.get("close", "nan")),
                "volume": float(c.get("volume", 0) or 0),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Coinbase returns newest-first; sort ascending so indicators see history.
    df = df.sort_values("start").reset_index(drop=True)
    return df
