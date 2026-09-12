"""Research tool: does adding BTC-relative / cross-sectional features help?

The baseline feature set (ema_gap_pct, rsi, macd_hist, ret_recent, volatility —
see src/model.py FEATURE_KEYS) computes everything per-product in isolation.
Two structural gaps that could explain its AUC~0.46 decay (see
train_from_history.py's 5-fold CV):

  1. No regime awareness — an altcoin's own RSI/MACD means something totally
     different in a BTC uptrend vs a BTC selloff (most alts are BTC-beta
     plays), but the model never sees BTC's state.
  2. No cross-sectional context — a given ret_recent/volume_ratio value isn't
     comparable across products or across time; nothing normalizes "is this
     strong RIGHT NOW, RELATIVE TO THE REST OF THE UNIVERSE".

This script computes the FULL per-bar feature series for every discovered
product (not just where each product's own buy signal fires), builds:
  - excess_ret_vs_btc:   product's ret_recent minus BTC's ret_recent at the
                          same bar (BTC-relative momentum)
  - btc_regime_bullish:  BTC's own ema_bullish at that bar (regime flag —
                          NOT selection-biased like a product's own
                          ema_bullish/macd_bullish, since alts aren't gated
                          on BTC's state)
  - rank_ret_recent:     percentile rank of ret_recent across all products
                          active at that exact bar (cross-sectional momentum)
  - rank_volume_ratio:   percentile rank of volume_ratio, same idea

...then re-applies the live signals.evaluate() gate to find buy-fired bars,
and fits/evaluates BASELINE-ONLY vs BASELINE+EXTRA logistic regressions on
the *identical* row subset (only rows where BTC data was alignable) using the
same 5-fold blocked time-series CV as train_from_history.py, so the AUC/
logloss numbers are directly comparable.

Pure research — never writes state/model.pkl or touches the live outcomes
table. Run inside the container (needs Coinbase creds/network):
    docker compose run --rm --entrypoint \\
      "python -m scripts.feature_experiment" app
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import marketdata, product_discovery, signals  # noqa: E402
from src.coinbase_client import CoinbaseClient  # noqa: E402
from src.config import load_config  # noqa: E402
from src.indicators import compute_features  # noqa: E402
from src import secrets as secrets_mod  # noqa: E402
from scripts.train_from_history import fetch_deep_history  # noqa: E402

log = logging.getLogger("feature_experiment")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

BASE_FEATS = ["ema_gap_pct", "rsi", "macd_hist", "ret_recent", "volatility"]
EXTRA_FEATS = ["excess_ret_vs_btc", "btc_regime_bullish", "rank_ret_recent", "rank_volume_ratio"]


def compute_feature_series(df: pd.DataFrame, product: str, cfg) -> pd.DataFrame:
    """One row per bar from warmup onward: full Features + the live signal
    direction at that bar (used later to find buy-fired rows)."""
    warmup = cfg.ema_slow + 1
    records = []
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        try:
            feats = compute_features(
                window, ema_fast=cfg.ema_fast, ema_slow=cfg.ema_slow,
                rsi_period=cfg.rsi_period, macd_fast=cfg.macd_fast,
                macd_slow=cfg.macd_slow, macd_signal=cfg.macd_signal,
                volume_avg_window=cfg.volume_avg_window,
            )
        except ValueError:
            continue
        sig = signals.evaluate(product, feats, cfg)
        rec = feats.to_vector()
        rec["start"] = int(df["start"].iloc[i])
        rec["signal"] = sig.direction
        records.append(rec)
    out = pd.DataFrame(records)
    if not out.empty:
        out["product"] = product
    return out


def build_dataset(cfg, max_days: int, max_products: int | None):
    key_path = secrets_mod.coinbase_key_path(cfg.coinbase_key_file or None)
    client = CoinbaseClient.from_key_file(key_path)
    products, _ = product_discovery.discover(
        client, cfg.product_min_quote_volume_24h, cfg.product_discovery_max_count)
    if max_products:
        products = products[:max_products]
    if "BTC-USD" not in products:
        products = ["BTC-USD"] + products
    log.info("Fetching full per-bar series for %d products, %s candles, %dd lookback",
             len(products), cfg.candle_granularity, max_days)

    close_lookup: dict[str, pd.Series] = {}
    series_frames = []
    for idx, product in enumerate(products, 1):
        df = fetch_deep_history(client, product, cfg.candle_granularity, max_days)
        if df.empty:
            log.warning("[%d/%d] %s: no history, skipping", idx, len(products), product)
            continue
        close_lookup[product] = df.set_index("start")["close"]
        series = compute_feature_series(df, product, cfg)
        log.info("[%d/%d] %s: %d bars", idx, len(products), product, len(series))
        if not series.empty:
            series_frames.append(series)

    all_bars = pd.concat(series_frames, ignore_index=True)

    # --- cross-sectional ranks: percentile rank within each timestamp bucket ---
    all_bars["rank_ret_recent"] = all_bars.groupby("start")["ret_recent"].rank(pct=True)
    all_bars["rank_volume_ratio"] = all_bars.groupby("start")["volume_ratio"].rank(pct=True)

    # --- BTC-relative regime features ---
    btc = all_bars[all_bars["product"] == "BTC-USD"][["start", "ret_recent", "ema_bullish"]]
    btc = btc.rename(columns={"ret_recent": "btc_ret_recent", "ema_bullish": "btc_regime_bullish"})
    all_bars = all_bars.merge(btc, on="start", how="left")
    all_bars["excess_ret_vs_btc"] = all_bars["ret_recent"] - all_bars["btc_ret_recent"]

    return all_bars, close_lookup, cfg


def label_buy_rows(all_bars: pd.DataFrame, close_lookup: dict, cfg) -> pd.DataFrame:
    secs = marketdata.GRANULARITY_SECONDS.get(cfg.candle_granularity, 21600)
    horizon_s = cfg.model_horizon_hours * 3600
    roundtrip_fee = 2 * cfg.fee_bps / 10_000.0

    buys = all_bars[
        (all_bars["signal"] == "buy") & (all_bars["volatility"] <= cfg.max_buy_volatility)
    ].dropna(subset=EXTRA_FEATS).copy()

    labels, pnls = [], []
    keep = []
    for row in buys.itertuples(index=False):
        cs = close_lookup.get(row.product)
        if cs is None:
            keep.append(False)
            continue
        target_ts = row.start + horizon_s
        idx = cs.index.searchsorted(target_ts)
        if idx >= len(cs):
            keep.append(False)
            continue
        exit_price = float(cs.iloc[idx])
        pnl_pct = (exit_price - row.price) / row.price
        labels.append(1 if pnl_pct > 0 else 0)
        pnls.append(pnl_pct - roundtrip_fee)
        keep.append(True)

    buys = buys[keep].copy()
    buys["label"] = labels
    buys["pnl"] = pnls
    return buys


def blocked_cv_report(name: str, df: pd.DataFrame, feats: list[str], n_folds: int = 5):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.metrics import roc_auc_score, log_loss

    df = df.sort_values("start").reset_index(drop=True)
    X = df[feats].values.astype(float)
    y = df["label"].values.astype(int)
    n = len(df)
    edges = np.linspace(0, n, n_folds + 1).astype(int)

    print(f"\n--- {name} ({feats}) ---")
    print(f"{'fold':>4} {'n_test':>7} {'test_winrate':>12} {'AUC':>7} {'logloss':>8} {'baseline':>9}")
    aucs = []
    for i in range(1, n_folds):
        tr, te = slice(0, edges[i]), slice(edges[i], edges[i + 1])
        Xtr, ytr = X[tr], y[tr]
        Xte, yte = X[te], y[te]
        if len(set(ytr)) < 2 or len(set(yte)) < 2 or len(yte) < 10:
            continue
        pipe = Pipeline([("sc", StandardScaler()), ("lr", LogisticRegression(max_iter=1000))])
        pipe.fit(Xtr, ytr)
        p = pipe.predict_proba(Xte)[:, 1]
        auc = roc_auc_score(yte, p)
        ll = log_loss(yte, p, labels=[0, 1])
        base_rate = ytr.mean()
        base_ll = -(base_rate * math.log(base_rate) + (1 - base_rate) * math.log(1 - base_rate))
        aucs.append(auc)
        print(f"{i:>4} {len(yte):>7} {yte.mean() * 100:>11.1f}% {auc:>7.3f} {ll:>8.4f} {base_ll:>9.4f}")
    if aucs:
        print(f"mean AUC across folds: {np.mean(aucs):.3f}")
    return aucs


def run(max_days: int, max_products: int | None) -> int:
    cfg = load_config()
    all_bars, close_lookup, cfg = build_dataset(cfg, max_days, max_products)
    buys = label_buy_rows(all_bars, close_lookup, cfg)
    print("=" * 60)
    print(f"Buy-fired rows with BTC/cross-sectional context available: {len(buys)}")
    if len(buys) < 100:
        print("Too few rows for a meaningful comparison. Aborting.")
        return 1
    print(f"win rate: {buys['label'].mean() * 100:.1f}%")

    base_aucs = blocked_cv_report("BASELINE (current live features)", buys, BASE_FEATS)
    ext_aucs = blocked_cv_report("BASELINE + BTC-relative + cross-sectional", buys,
                                 BASE_FEATS + EXTRA_FEATS)

    print("\n" + "=" * 60)
    if base_aucs and ext_aucs:
        print(f"mean AUC: baseline={np.mean(base_aucs):.3f}  extended={np.mean(ext_aucs):.3f}  "
              f"delta={np.mean(ext_aucs) - np.mean(base_aucs):+.3f}")
    print("=" * 60)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-days", type=int, default=365)
    ap.add_argument("--max-products", type=int, default=None)
    args = ap.parse_args()
    return run(args.max_days, args.max_products)


if __name__ == "__main__":
    raise SystemExit(main())
