"""Research tool: does perpetual-futures funding rate carry real signal?

Every prior feature experiment on this project (see LESSONS_LEARNED.md and
the 2026-08-17/09-05 entries in the experiment-log memory) tested variations
within the same momentum/technical-indicator family (EMA/RSI/MACD, its
BTC-relative and cross-sectional cousins) and found no durable out-of-sample
edge. Funding rate is a structurally DIFFERENT mechanism: it measures how
crowded/expensive it is to be long vs short in the perpetual-futures market
right now (positive = longs pay shorts, i.e. the crowd is long), and the
documented effect is mean-reversion — an extreme funding rate tends to
precede the crowded side getting squeezed, which is the opposite bet from
"price went up recently so it'll keep going up".

Coinbase trades spot only, so this pulls funding history from perpetual
futures elsewhere. Two data-access notes worth keeping:
  * Binance's and Bybit's live REST APIs (fapi.binance.com, api.bybit.com)
    both return HTTP 451/403 "restricted location" from this host.
  * Binance's public historical-data bucket (data.binance.vision) is NOT
    geo-restricted — it's static file hosting, not the trading API — and
    serves monthly funding-rate CSV archives per symbol going back to each
    perp's listing date. That's the source used here.

Builds baseline buy-fired rows exactly like feature_experiment.py (same
compute_features()/signals.evaluate() live code path, same label/pnl math),
merges in funding-derived features causally aligned to each bar's timestamp
(merge_asof, backward — no lookahead), then compares BASELINE vs
BASELINE+FUNDING via the same 5-fold blocked time-series CV used throughout
this project, plus a bucketed win-rate table (a linear model can miss a
threshold/mean-reversion effect that a quantile split would show).

Pure research — never writes state/model.pkl or touches the live outcomes
table. Run inside the container (needs Coinbase creds/network):
    docker compose run --rm --entrypoint \\
      "python -m scripts.funding_rate_experiment" app
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from scripts.feature_experiment import (  # noqa: E402
    BASE_FEATS, build_dataset, label_buy_rows, blocked_cv_report,
)

log = logging.getLogger("funding_rate_experiment")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

FUNDING_FEATS = ["funding_rate", "funding_z", "funding_cum_3d"]

_FUNDING_URL = (
    "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
    "{sym}/{sym}-fundingRate-{ym}.zip"
)
# Binance USDⓈ-M perps fund every 8h -> ~3/day. Windows below are in units of
# funding PERIODS, not days.
_ZSCORE_WINDOW_PERIODS = 90   # ~30 days
_ZSCORE_MIN_PERIODS = 20
_CUM_WINDOW_PERIODS = 9       # ~3 days


def product_to_binance_symbol(product: str) -> str:
    return product.split("-")[0] + "USDT"


def fetch_funding_history(symbol: str, max_months: int = 15,
                           pause_s: float = 0.1) -> pd.Series:
    """Monthly CSVs from data.binance.vision, walking backward from the
    current month. Stops at the first missing month (before the perp's
    listing date) — fail-open, returns whatever was collected. Index is
    Unix seconds (to match Coinbase candle ``start``); values are the raw
    funding rate."""
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    frames = []
    for i in range(max_months):
        ym = f"{y}-{m:02d}"
        url = _FUNDING_URL.format(sym=symbol, ym=ym)
        try:
            resp = requests.get(url, timeout=15)
        except requests.RequestException as exc:
            log.warning("%s: funding fetch error for %s (%s), stopping", symbol, ym, exc)
            break
        if resp.status_code != 200:
            # The bucket only publishes a month's archive once it's complete,
            # so the current in-progress month always 404s — that's expected,
            # not "before listing date". Only treat a miss as the stop
            # condition once we're past that first (current) month.
            if i == 0:
                m -= 1
                if m == 0:
                    m, y = 12, y - 1
                continue
            break
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    frames.append(pd.read_csv(f))
        except Exception as exc:  # noqa: BLE001 — corrupt/empty archive, skip
            log.warning("%s: bad archive for %s (%s), skipping month", symbol, ym, exc)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        time.sleep(pause_s)
    if not frames:
        return pd.Series(dtype=float)
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = (df["calc_time"] // 1000).astype(int)
    df = df.sort_values("ts").drop_duplicates("ts")
    return pd.Series(df["last_funding_rate"].astype(float).values, index=df["ts"].values)


def fetch_all_funding(products: list[str], max_months: int) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for idx, product in enumerate(products, 1):
        sym = product_to_binance_symbol(product)
        s = fetch_funding_history(sym, max_months=max_months)
        if s.empty:
            log.info("[%d/%d] %s (%s): no funding history, excluded", idx, len(products), product, sym)
            continue
        log.info("[%d/%d] %s (%s): %d funding periods, %s -> %s",
                  idx, len(products), product, sym, len(s),
                  datetime.fromtimestamp(int(s.index.min()), tz=timezone.utc).date(),
                  datetime.fromtimestamp(int(s.index.max()), tz=timezone.utc).date())
        out[product] = s
    return out


def attach_funding_features(buys: pd.DataFrame,
                             funding_by_product: dict[str, pd.Series]) -> pd.DataFrame:
    """Causal merge: each buy row gets the most recent funding reading at or
    before its bar's timestamp (merge_asof, direction='backward' — no
    lookahead), plus rolling stats computed only from that product's own
    funding history up to and including that reading."""
    parts = []
    for product, group in buys.groupby("product"):
        s = funding_by_product.get(product)
        if s is None or s.empty:
            continue
        s = s.sort_index()
        roll_mean = s.rolling(_ZSCORE_WINDOW_PERIODS, min_periods=_ZSCORE_MIN_PERIODS).mean()
        roll_std = s.rolling(_ZSCORE_WINDOW_PERIODS, min_periods=_ZSCORE_MIN_PERIODS).std()
        funding_z = (s - roll_mean) / roll_std.replace(0.0, np.nan)
        funding_cum = s.rolling(_CUM_WINDOW_PERIODS, min_periods=3).sum()
        fdf = pd.DataFrame({
            "ts": s.index.astype(int),
            "funding_rate": s.values,
            "funding_z": funding_z.values,
            "funding_cum_3d": funding_cum.values,
        }).sort_values("ts")
        g = group.sort_values("start")
        merged = pd.merge_asof(g, fdf, left_on="start", right_on="ts", direction="backward")
        parts.append(merged)
    if not parts:
        return buys.iloc[0:0]
    return pd.concat(parts, ignore_index=True)


def bucketed_winrate(df: pd.DataFrame, feat: str, n_buckets: int = 5) -> None:
    """Quantile win-rate/avg-pnl table — catches a threshold/mean-reversion
    effect a linear model's AUC could wash out."""
    d = df.dropna(subset=[feat]).copy()
    if len(d) < n_buckets * 10:
        print(f"  (too few rows to bucket {feat})")
        return
    d["bucket"] = pd.qcut(d[feat], n_buckets, duplicates="drop")
    g = d.groupby("bucket", observed=True).agg(
        n=("label", "size"), win_rate=("label", "mean"), avg_pnl=("pnl", "mean"))
    print(f"\n  {feat} buckets (low -> high):")
    for bucket, row in g.iterrows():
        print(f"    {str(bucket):>22s}  n={int(row['n']):4d}  "
              f"win_rate={row['win_rate']*100:5.1f}%  avg_pnl={row['avg_pnl']:+.4f}")


def run(max_days: int, max_months: int, max_products: int | None) -> int:
    cfg = load_config()
    all_bars, close_lookup, cfg = build_dataset(cfg, max_days, max_products)
    buys = label_buy_rows(all_bars, close_lookup, cfg)
    print("=" * 70)
    print(f"Buy-fired rows (baseline features available): {len(buys)}")
    if len(buys) < 100:
        print("Too few rows for a meaningful comparison. Aborting.")
        return 1

    products = sorted(buys["product"].unique())
    print(f"Fetching funding history for {len(products)} products from "
          f"data.binance.vision ({max_months} months back)...")
    funding_by_product = fetch_all_funding(products, max_months)
    print(f"Funding history found for {len(funding_by_product)}/{len(products)} products.")

    buys_f = attach_funding_features(buys, funding_by_product)
    buys_f = buys_f.dropna(subset=FUNDING_FEATS)
    print(f"Buy-fired rows with funding context available: {len(buys_f)}")
    if len(buys_f) < 100:
        print("Too few rows with funding data for a meaningful comparison. Aborting.")
        return 1
    print(f"win rate on this subset: {buys_f['label'].mean() * 100:.1f}%")

    print("\n--- raw correlation with pnl (sanity check, matches the "
          "2026-08-17 diagnosis methodology) ---")
    for feat in FUNDING_FEATS:
        corr = buys_f[[feat, "pnl"]].corr().iloc[0, 1]
        print(f"  corr({feat}, pnl) = {corr:+.4f}")

    print("\n--- bucketed win rate (catches a threshold effect a linear AUC could miss) ---")
    for feat in FUNDING_FEATS:
        bucketed_winrate(buys_f, feat)

    base_aucs = blocked_cv_report("BASELINE (current live features)", buys_f, BASE_FEATS)
    ext_aucs = blocked_cv_report("BASELINE + FUNDING", buys_f, BASE_FEATS + FUNDING_FEATS)

    print("\n" + "=" * 70)
    if base_aucs and ext_aucs:
        print(f"mean AUC: baseline={np.mean(base_aucs):.3f}  "
              f"extended={np.mean(ext_aucs):.3f}  "
              f"delta={np.mean(ext_aucs) - np.mean(base_aucs):+.3f}")
    print("=" * 70)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-days", type=int, default=270,
                     help="candle/price lookback (funding history for newer "
                          "perps is often shallower and will further limit "
                          "the usable overlap per product)")
    ap.add_argument("--max-months", type=int, default=15,
                     help="funding-history lookback in months")
    ap.add_argument("--max-products", type=int, default=None)
    args = ap.parse_args()
    return run(args.max_days, args.max_months, args.max_products)


if __name__ == "__main__":
    raise SystemExit(main())
