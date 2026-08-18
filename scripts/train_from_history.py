"""Bootstrap the model from deep Coinbase history instead of waiting weeks for
live paper outcomes to close under the (deliberately widened, 2026-08-17) 96h
horizon / 6h candles.

Walks each discovered product's full available candle history through the
exact same `compute_features()` / `signals.evaluate()` code path the live bot
uses (src/bot.py:_decide_product), and at every bar where a buy signal would
have fired (and cleared the volatility screen), computes the forward
MODEL_HORIZON_HOURS return as a synthetic labeled outcome — same label/pnl
math as `Bot._evaluate_due_outcomes`. Fits the model on these synthetic
outcomes (via the same `Model._fit`/`_logloss` the live retrain uses) and, if
the time-ordered holdout looks sane, saves it as `state/model.pkl`,
replacing the coldstart heuristic.

Known limitations (read before trusting the output):
  - Does NOT replicate the live HTF-trend hard gate (src/bot.py:_htf_trend) —
    that would need a second candle series per product. Synthetic samples may
    include some entries the live bot would actually block on HTF-bearish.
  - Consecutive bars in a sustained trend can all fire "buy" — the label
    windows overlap heavily (horizon_bars-1 out of horizon_bars bars shared
    between neighboring samples). This mirrors what the live bot itself does
    (nothing stops repeat buys either), but it means the samples are far from
    i.i.d.; treat the holdout metric as a rough signal, not a rigorous
    estimate.
  - Backtest survivorship: only today's discovered (currently-liquid)
    products are walked, so products that later got delisted/died are
    invisible.

Deliberately does NOT touch the live `outcomes` SQLite table — real paper
fills stay a pure, uncontaminated record. This is a one-time offline
bootstrap; Model.maybe_retrain()'s existing promotion guard (never promote a
worse holdout) is what lets real accumulating data eventually override it.

Usage (must run inside the container — needs Coinbase API creds/network):
    docker compose run --rm --entrypoint \\
      "python -m scripts.train_from_history" app
    # add --dry-run to print the report without writing state/model.pkl
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import marketdata, product_discovery, signals  # noqa: E402
from src.coinbase_client import CoinbaseClient, CoinbaseError, RateLimited  # noqa: E402
from src.config import load_config  # noqa: E402
from src.indicators import compute_features  # noqa: E402
from src.model import Model  # noqa: E402
from src.state import State  # noqa: E402

log = logging.getLogger("train_from_history")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

STATE_DIR = "state"
DB_PATH = f"{STATE_DIR}/grow.db"
MODEL_PATH = f"{STATE_DIR}/model.pkl"


def fetch_deep_history(client: CoinbaseClient, product_id: str, granularity: str,
                       max_days: int, limit: int = 300, pause_s: float = 0.15) -> pd.DataFrame:
    """Page backward from now, oldest->newest DataFrame, until max_days is
    covered, the product's listing date is hit (short/empty page), or a
    request fails (fail-open: return whatever was collected so far)."""
    secs = marketdata.GRANULARITY_SECONDS.get(granularity, 21600)
    end = datetime.now(timezone.utc)
    earliest = end - timedelta(days=max_days)
    chunks = []
    seen_starts: set[int] = set()
    cursor_end = end
    while cursor_end > earliest:
        cursor_start = max(earliest, cursor_end - timedelta(seconds=secs * limit))
        try:
            raw = client.get_candles(
                product_id=product_id, granularity=granularity,
                start=str(int(cursor_start.timestamp())),
                end=str(int(cursor_end.timestamp())), limit=limit,
            )
        except RateLimited:
            log.warning("%s: rate limited, stopping pagination with %d bars so far",
                        product_id, sum(len(c) for c in chunks))
            break
        except CoinbaseError as exc:
            log.warning("%s: candle fetch failed (%s), stopping pagination", product_id, exc)
            break
        df_chunk = marketdata.candles_to_df(raw)
        if df_chunk.empty:
            break
        new = df_chunk[~df_chunk["start"].isin(seen_starts)]
        if new.empty:
            break
        seen_starts.update(df_chunk["start"].tolist())
        chunks.append(new)
        oldest_start = int(df_chunk["start"].min())
        cursor_end = datetime.fromtimestamp(oldest_start, tz=timezone.utc)
        if len(df_chunk) < limit * 0.5:
            break  # short page -> likely hit the product's listing date
        time.sleep(pause_s)
    if not chunks:
        return pd.DataFrame()
    full = pd.concat(chunks).drop_duplicates(subset="start").sort_values("start").reset_index(drop=True)
    return full


def synthesize_outcomes(df: pd.DataFrame, product: str, cfg) -> list[dict]:
    """Walk one product's candle history and emit synthetic labeled outcomes
    everywhere the live buy path would have fired (rule signal + volatility
    screen — see module docstring for what's NOT replicated)."""
    rows: list[dict] = []
    warmup = cfg.ema_slow + 1
    secs = marketdata.GRANULARITY_SECONDS.get(cfg.candle_granularity, 21600)
    horizon_bars = max(1, round(cfg.model_horizon_hours * 3600 / secs))
    roundtrip_fee = 2 * cfg.fee_bps / 10_000.0

    for i in range(warmup, len(df) - horizon_bars):
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
        if sig.direction != "buy":
            continue
        if feats.volatility > cfg.max_buy_volatility:
            continue
        entry_price = feats.price
        exit_price = float(df["close"].iloc[i + horizon_bars])
        pnl_pct = (exit_price - entry_price) / entry_price
        label = 1 if pnl_pct > 0 else 0
        entry_ts = datetime.fromtimestamp(int(df["start"].iloc[i]), tz=timezone.utc)
        rows.append({
            "product": product,
            "entry_ts_utc": entry_ts.isoformat(),
            "features": feats.to_vector(),
            "label": label,
            "pnl": pnl_pct - roundtrip_fee,
        })
    return rows


def run(max_days: int, dry_run: bool, max_products: int | None, dump_csv: str | None = None) -> int:
    cfg = load_config()
    from src import secrets as secrets_mod
    key_path = secrets_mod.coinbase_key_path(cfg.coinbase_key_file or None)
    client = CoinbaseClient.from_key_file(key_path)

    products, _volumes = product_discovery.discover(
        client, cfg.product_min_quote_volume_24h, cfg.product_discovery_max_count)
    if max_products:
        products = products[:max_products]
    log.info("Backtesting %d products at %s candles, %dd lookback, %dh horizon",
             len(products), cfg.candle_granularity, max_days, cfg.model_horizon_hours)

    all_rows: list[dict] = []
    for idx, product in enumerate(products, 1):
        df = fetch_deep_history(client, product, cfg.candle_granularity, max_days)
        if df.empty:
            log.warning("[%d/%d] %s: no candle history, skipping", idx, len(products), product)
            continue
        rows = synthesize_outcomes(df, product, cfg)
        log.info("[%d/%d] %s: %d candles -> %d synthetic buy signals",
                 idx, len(products), product, len(df), len(rows))
        all_rows.extend(rows)

    n = len(all_rows)
    print("=" * 60)
    print(f"Synthetic outcomes generated: {n}")
    if n < cfg.model_min_train_samples:
        print(f"Below MODEL_MIN_TRAIN_SAMPLES ({cfg.model_min_train_samples}) — "
              "not enough signal history to bootstrap a model. Try widening "
              "--max-days or check that product discovery found products.")
        return 1

    wins = sum(r["label"] for r in all_rows)
    win_rate = wins / n
    avg_pnl = sum(r["pnl"] for r in all_rows) / n
    print(f"Win rate (raw direction): {win_rate * 100:.1f}%   avg realized pnl: {avg_pnl * 100:+.2f}%")

    if dump_csv:
        import csv
        with open(dump_csv, "w", newline="") as f:
            fieldnames = ["product", "entry_ts_utc", "label", "pnl"] + \
                list(all_rows[0]["features"].keys())
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in all_rows:
                w.writerow({"product": r["product"], "entry_ts_utc": r["entry_ts_utc"],
                           "label": r["label"], "pnl": r["pnl"], **r["features"]})
        print(f"Dumped {n} synthetic rows to {dump_csv}")

    # Reuse the exact fit/holdout logic the live retrain path uses.
    all_rows.sort(key=lambda r: r["entry_ts_utc"])
    holdout_n = max(1, int(n * 0.2))
    train_rows, holdout_rows = all_rows[:-holdout_n], all_rows[-holdout_n:]
    if len({r["label"] for r in train_rows}) < 2:
        print("Training split lacks both classes — cannot fit. Aborting.")
        return 1

    state = State(DB_PATH)
    model = Model(cfg, state, MODEL_PATH)  # _load() no-ops if model.pkl absent
    candidate = model._fit(train_rows)  # noqa: SLF001 — intentional reuse of internals
    holdout_loss = model._logloss(candidate, holdout_rows)  # noqa: SLF001

    import math
    import numpy as np
    from src.model import features_to_row
    from sklearn.metrics import roc_auc_score
    p_train_rate = sum(r["label"] for r in train_rows) / len(train_rows)
    baseline_loss = -(p_train_rate * math.log(p_train_rate)
                       + (1 - p_train_rate) * math.log(1 - p_train_rate))
    Xh = np.array([features_to_row(r["features"]) for r in holdout_rows], dtype=float)
    yh = np.array([r["label"] for r in holdout_rows], dtype=int)
    ph = candidate.predict_proba(Xh)[:, 1]
    try:
        auc = roc_auc_score(yh, ph)
    except ValueError:
        auc = float("nan")  # single-class holdout

    print(f"Time-ordered holdout logloss: {holdout_loss:.4f}   "
          f"constant-baseline logloss: {baseline_loss:.4f}   "
          f"holdout AUC: {auc:.4f}")
    print(f"(train n={len(train_rows)}, holdout n={len(holdout_rows)}, "
          f"train win rate={p_train_rate * 100:.1f}%)")
    if holdout_loss >= baseline_loss:
        print("*** Candidate is NO BETTER than predicting the constant base rate "
              "(logloss >= baseline). This is not a usable model — do not deploy. ***")
    print("=" * 60)

    if holdout_loss >= baseline_loss:
        print("Refusing to write state/model.pkl: candidate has no demonstrated "
              "skill over the constant baseline.")
        return 1

    if dry_run:
        print("--dry-run: not writing state/model.pkl. Re-run without --dry-run to deploy.")
        return 0

    final = model._fit(all_rows)  # noqa: SLF001 — refit on full synthetic set for deployment
    model._save(final, n)  # noqa: SLF001
    print(f"Wrote {MODEL_PATH} (n_samples={n}). Restart the bot container to load it.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-days", type=int, default=365,
                    help="how far back to page candle history per product (default 365)")
    ap.add_argument("--max-products", type=int, default=None,
                    help="cap the number of discovered products walked (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report but do not write state/model.pkl")
    ap.add_argument("--dump-csv", default=None,
                    help="also write the raw synthetic (features, label, pnl) rows to this CSV path")
    args = ap.parse_args()
    return run(args.max_days, args.dry_run, args.max_products, args.dump_csv)


if __name__ == "__main__":
    raise SystemExit(main())
