"""Scriptable backtest over historical candles — no live orders, no network.

Feeds a saved candle fixture (CSV with columns start,low,high,open,close,volume
or a Coinbase-style JSON list of candle dicts) through the same
indicators/signals path and simulates fills, printing a summary P&L. This is a
pre-paper sanity check, deliberately unpolished (plan section 9).

Usage:
    .venv/bin/python scripts/backtest.py <fixture> [--product BTC-USD]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# allow running as a plain script (python scripts/backtest.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.indicators import compute_features  # noqa: E402
from src import signals  # noqa: E402


def load_fixture(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text())
        rows = raw.get("candles", raw) if isinstance(raw, dict) else raw
        df = pd.DataFrame([
            {"start": int(c.get("start", i)), "low": float(c["low"]),
             "high": float(c["high"]), "open": float(c["open"]),
             "close": float(c["close"]), "volume": float(c.get("volume", 0) or 0)}
            for i, c in enumerate(rows)
        ])
    else:
        df = pd.read_csv(p)
    return df.sort_values("start").reset_index(drop=True)


def run(fixture: str, product: str = "BTC-USD") -> int:
    cfg = load_config(use_dotenv=False)
    df = load_fixture(fixture)
    if len(df) < cfg.ema_slow + 5:
        print(f"fixture too short: {len(df)} candles")
        return 1

    cash = cfg.paper_start_bankroll
    position = 0.0
    avg_entry = 0.0
    trades = 0
    fee = cfg.fee_bps / 10_000.0

    warmup = cfg.ema_slow + 1
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        try:
            feats = compute_features(window, ema_fast=cfg.ema_fast, ema_slow=cfg.ema_slow,
                                     rsi_period=cfg.rsi_period, macd_fast=cfg.macd_fast,
                                     macd_slow=cfg.macd_slow, macd_signal=cfg.macd_signal)
        except ValueError:
            continue
        sig = signals.evaluate(product, feats, cfg)
        price = feats.price
        if sig.direction == "buy" and position == 0.0 and cash > cfg.risk.min_order_usd:
            budget = cfg.per_trade_budget_fraction * cfg.paper_start_bankroll
            spend = min(budget, cash)
            qty = spend / price
            position += qty
            avg_entry = price
            cash -= spend * (1 + fee)
            trades += 1
        elif sig.direction == "sell" and position > 0.0:
            proceeds = position * price
            cash += proceeds * (1 - fee)
            position = 0.0
            trades += 1

    final_value = cash + position * float(df["close"].iloc[-1])
    ret = (final_value - cfg.paper_start_bankroll) / cfg.paper_start_bankroll

    hold_qty = cfg.paper_start_bankroll / float(df["close"].iloc[warmup])
    hold_value = hold_qty * float(df["close"].iloc[-1])
    hold_ret = (hold_value - cfg.paper_start_bankroll) / cfg.paper_start_bankroll

    print("=" * 46)
    print(f"Backtest {product}  candles={len(df)} trades={trades}")
    print(f"strategy final value: ${final_value:,.2f}  return {ret * 100:+.2f}%")
    print(f"buy-and-hold value:   ${hold_value:,.2f}  return {hold_ret * 100:+.2f}%")
    print(f"delta vs hold: {(ret - hold_ret) * 100:+.2f}%")
    print("=" * 46)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--product", default="BTC-USD")
    args = ap.parse_args()
    return run(args.fixture, args.product)


if __name__ == "__main__":
    raise SystemExit(main())
