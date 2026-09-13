"""Research tool: does on-chain capital-flow (stablecoin supply / DeFi TVL
growth) carry real signal?

Two prior signal families were tested and both failed at full scale (see
LESSONS_LEARNED.md): momentum/technical-indicators (EMA/RSI/MACD, its
BTC-relative and cross-sectional cousins — 3 separate evaluations) and
perpetual-futures funding rate (looked promising on a 6-product/4-month
subset, vanished on the full 48-product/14-month backtest).

This is a THIRD, structurally different mechanism: capital actually moving
into or out of the crypto ecosystem, rather than anything derived from an
asset's own price history. Two public, free, deep-history series from
DeFiLlama:
  - aggregate USD-pegged stablecoin supply (stablecoins.llama.fi) — new
    stablecoins minted are usually capital about to buy crypto; supply
    shrinking usually means capital is leaving.
  - aggregate DeFi TVL (api.llama.fi) — a broader on-chain risk-appetite
    proxy (capital locked in yield/lending/DEX protocols).

Both are MARKET-WIDE regime series (one number per day, not per-product),
unlike funding rate which was per-product. Daily history back to
2017-11-29 (stablecoins) / 2017-09-27 (TVL), no geo-blocking (unlike
Binance/Bybit's live trading APIs — see the funding-rate experiment).

Builds baseline buy-fired rows exactly like feature_experiment.py (same
compute_features()/signals.evaluate() live code path, same label/pnl math),
merges in the on-chain features causally aligned to each bar's timestamp
(merge_asof, backward — no lookahead, same day's flow data is public
same-day so this isn't lookahead), then compares BASELINE vs
BASELINE+ONCHAIN via the same 5-fold blocked time-series CV used
throughout this project, plus a bucketed win-rate table.

IMPORTANT (see LESSONS_LEARNED.md, "a lesson in trusting small-subset
results"): never report or act on a --max-products-limited dry run as if it
were the answer — it exists only to catch pipeline bugs cheaply. Only the
full-universe, full-history run is evidence.

Pure research — never writes state/model.pkl or touches the live outcomes
table. Run inside the container (needs Coinbase creds/network):
    docker compose run --rm --entrypoint \\
      "python -m scripts.onchain_flow_experiment" app
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from scripts.feature_experiment import (  # noqa: E402
    BASE_FEATS, build_dataset, label_buy_rows, blocked_cv_report,
)
from scripts.funding_rate_experiment import bucketed_winrate  # noqa: E402

log = logging.getLogger("onchain_flow_experiment")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

ONCHAIN_FEATS = ["stable_supply_chg_3d", "stable_supply_chg_7d", "stable_supply_z", "tvl_chg_7d"]

_STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
_TVL_URL = "https://api.llama.fi/charts"
_ZSCORE_WINDOW_DAYS = 30
_ZSCORE_MIN_DAYS = 14


def fetch_daily_series(url: str, value_path: tuple[str, ...]) -> pd.Series:
    """Fetch a DeFiLlama daily chart endpoint -> Series indexed by Unix
    seconds (midnight UTC). ``value_path`` walks nested dict keys, e.g.
    ("totalCirculatingUSD", "peggedUSD")."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    ts, vals = [], []
    for row in data:
        v = row
        try:
            for key in value_path:
                v = v[key]
        except (KeyError, TypeError):
            continue
        ts.append(int(row["date"]))
        vals.append(float(v))
    s = pd.Series(vals, index=ts).sort_index()
    return s[~s.index.duplicated(keep="last")]


def build_onchain_features() -> pd.DataFrame:
    log.info("Fetching stablecoin supply history from %s ...", _STABLECOIN_URL)
    stable = fetch_daily_series(_STABLECOIN_URL, ("totalCirculatingUSD", "peggedUSD"))
    log.info("Fetching DeFi TVL history from %s ...", _TVL_URL)
    tvl = fetch_daily_series(_TVL_URL, ("totalLiquidityUSD",))
    log.info("stablecoin supply: %d days, %s -> %s", len(stable),
              pd.Timestamp(stable.index.min(), unit="s"), pd.Timestamp(stable.index.max(), unit="s"))
    log.info("DeFi TVL: %d days, %s -> %s", len(tvl),
              pd.Timestamp(tvl.index.min(), unit="s"), pd.Timestamp(tvl.index.max(), unit="s"))

    stable_chg_3d = stable.pct_change(3)
    stable_chg_7d = stable.pct_change(7)
    daily_chg = stable.pct_change(1)
    roll_mean = daily_chg.rolling(_ZSCORE_WINDOW_DAYS, min_periods=_ZSCORE_MIN_DAYS).mean()
    roll_std = daily_chg.rolling(_ZSCORE_WINDOW_DAYS, min_periods=_ZSCORE_MIN_DAYS).std()
    stable_z = (daily_chg - roll_mean) / roll_std.replace(0.0, np.nan)
    tvl_chg_7d = tvl.pct_change(7)

    df = pd.DataFrame({
        "ts": stable.index,
        "stable_supply_chg_3d": stable_chg_3d.values,
        "stable_supply_chg_7d": stable_chg_7d.values,
        "stable_supply_z": stable_z.values,
    })
    tvl_df = pd.DataFrame({"ts": tvl.index, "tvl_chg_7d": tvl_chg_7d.values})
    df = df.merge(tvl_df, on="ts", how="outer").sort_values("ts").reset_index(drop=True)
    return df


def attach_onchain_features(buys: pd.DataFrame, onchain: pd.DataFrame) -> pd.DataFrame:
    """Causal merge (merge_asof, backward — no lookahead): each buy row gets
    the most recent daily on-chain reading at or before its bar's
    timestamp. Market-wide, so every product shares the same series
    (unlike funding rate, which was per-product)."""
    g = buys.sort_values("start").reset_index(drop=True)
    return pd.merge_asof(g, onchain, left_on="start", right_on="ts", direction="backward")


def run(max_days: int, max_products: int | None) -> int:
    cfg = load_config()
    all_bars, close_lookup, cfg = build_dataset(cfg, max_days, max_products)
    buys = label_buy_rows(all_bars, close_lookup, cfg)
    print("=" * 70)
    print(f"Buy-fired rows (baseline features available): {len(buys)}")
    if len(buys) < 100:
        print("Too few rows for a meaningful comparison. Aborting.")
        return 1

    onchain = build_onchain_features()
    buys_f = attach_onchain_features(buys, onchain)
    buys_f = buys_f.dropna(subset=ONCHAIN_FEATS)
    print(f"Buy-fired rows with on-chain context available: {len(buys_f)}")
    if len(buys_f) < 100:
        print("Too few rows with on-chain data for a meaningful comparison. Aborting.")
        return 1
    print(f"win rate on this subset: {buys_f['label'].mean() * 100:.1f}%")

    print("\n--- raw correlation with pnl ---")
    for feat in ONCHAIN_FEATS:
        corr = buys_f[[feat, "pnl"]].corr().iloc[0, 1]
        print(f"  corr({feat}, pnl) = {corr:+.4f}")

    print("\n--- bucketed win rate (catches a threshold effect a linear AUC could miss) ---")
    for feat in ONCHAIN_FEATS:
        bucketed_winrate(buys_f, feat)

    base_aucs = blocked_cv_report("BASELINE (current live features)", buys_f, BASE_FEATS)
    ext_aucs = blocked_cv_report("BASELINE + ON-CHAIN FLOW", buys_f, BASE_FEATS + ONCHAIN_FEATS)

    print("\n" + "=" * 70)
    if base_aucs and ext_aucs:
        print(f"mean AUC: baseline={np.mean(base_aucs):.3f}  "
              f"extended={np.mean(ext_aucs):.3f}  "
              f"delta={np.mean(ext_aucs) - np.mean(base_aucs):+.3f}")
    if max_products is not None:
        print("NOTE: this was a --max-products-limited dry run — do NOT treat this "
              "result as evidence either way. Run the full universe before concluding "
              "anything (see LESSONS_LEARNED.md's small-subset-result lesson).")
    print("=" * 70)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-days", type=int, default=365)
    ap.add_argument("--max-products", type=int, default=None)
    args = ap.parse_args()
    return run(args.max_days, args.max_products)


if __name__ == "__main__":
    raise SystemExit(main())
