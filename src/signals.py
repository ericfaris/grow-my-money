"""Rule-based signal layer: indicators -> TradeIntent proposal.

Turns the latest ``Features`` into a direction (buy/sell/hold) plus a base
confidence in [0,1]. The model layer (``model.py``) then gates/scales buys; sells
are driven here (cross-down / RSI exit) and are NEVER gated by the model so the
bot can always de-risk.
"""
from __future__ import annotations

from dataclasses import dataclass

from .indicators import Features


@dataclass
class Signal:
    product: str
    direction: str        # 'buy' | 'sell' | 'hold'
    confidence: float     # base rule confidence in [0,1]
    reason: str


def evaluate(product: str, feats: Features, cfg) -> Signal:
    """Rule logic:
      * BUY when fast EMA is above slow EMA AND MACD bullish AND RSI not overbought.
      * SELL when fast EMA crosses below slow EMA OR RSI overbought (exit/de-risk).
      * else HOLD.
    Confidence blends the EMA gap magnitude and MACD histogram.
    """
    bullish = feats.ema_bullish and feats.macd_bullish
    overbought = feats.rsi >= cfg.rsi_overbought
    oversold = feats.rsi <= cfg.rsi_oversold

    # base confidence from trend strength
    gap_strength = min(abs(feats.ema_gap_pct) * 20.0, 1.0)  # 5% gap -> 1.0
    hist_strength = min(abs(feats.macd_hist) / (abs(feats.price) * 0.01 + 1e-9), 1.0)
    conf = max(0.0, min(1.0, 0.5 * gap_strength + 0.5 * hist_strength))

    if bullish and not overbought:
        return Signal(product, "buy", conf,
                      f"EMA{cfg.ema_fast}>EMA{cfg.ema_slow}, MACD bullish, RSI={feats.rsi:.0f}")
    if (not feats.ema_bullish) or overbought:
        reason = "EMA cross-down" if not feats.ema_bullish else f"RSI overbought {feats.rsi:.0f}"
        return Signal(product, "sell", conf, reason)
    if oversold:
        # oversold with no bullish confirmation yet -> hold, wait for cross
        return Signal(product, "hold", conf, f"RSI oversold {feats.rsi:.0f}, awaiting cross")
    return Signal(product, "hold", conf, "no signal")
