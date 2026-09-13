# Lessons Learned

Running log of what this project has actually taught us — trading-strategy
findings and the engineering bugs that shaped how much to trust the numbers.
Newest first within each section. See `.claude` memory
(`grow_my_money_experiment_log`) for the full blow-by-blow; this file is the
distilled, durable version meant to live in the repo.

## Strategy verdict: no demonstrated edge in the technical-indicator family

Four independent evaluations, two live regimes and two offline backtests,
all reach the same conclusion:

| # | What | Result |
|---|---|---|
| 1 | Live 1h/24h regime (ended 2026-08-17) | **-20.2%** vs **-2.0%** buy-hold (-18.6pp), 16% win rate, fees ate 8% of bankroll |
| 2 | Offline backtest, full year of history, same regime's features | AUC 0.46 (worse than random); failed its own no-worse-than-baseline guard |
| 3 | Offline: added BTC-relative + cross-sectional features | Still no edge — AUC 0.52 at best, logloss lost to a coin flip in every fold |
| 4 | Live 6h/96h regime (current, deployed 2026-08-18) | Looked promising 2026-09-04 (+20.4%, first promoted model, logloss 0.50) — promotion's own live picks then went **15% win rate over the next 20 trades**; gap vs. buy-hold nearly tripled to -14.7pp by 2026-09-13 |
| 5 | Offline: perpetual-futures funding rate (raw rate, rolling z-score, 3-day cumulative), `scripts/funding_rate_experiment.py`, 48 products, ~12,145 buy-fired rows, up to 14 months | **AUC delta +0.001** (0.509→0.510); correlations with pnl all ~0 (+0.007, -0.026, +0.018); bucketed win rate flat at 39-43% across every quantile, no trend |
| 6 | Offline: on-chain capital flow (stablecoin supply growth 3d/7d + z-score, DeFi TVL growth 7d), `scripts/onchain_flow_experiment.py`, 50 products, 13,557 buy-fired rows, 365 days | **AUC delta -0.031** (0.508→0.477, WORSE than baseline); correlations with pnl all ~0 (-0.007, -0.001, +0.023, +0.016); bucketed win rates noisy with no clean trend |

**Conclusion**: EMA/RSI/MACD/momentum-derived features do not have a durable,
real out-of-sample edge at this scale. Every time a live result looked good,
it was a small-sample fluke that inverted on the next batch — not evidence
tuning would fix. Threshold/horizon/volatility-screen tuning was tried
repeatedly across both regimes; the shape of the result never changed.
**Funding rate — a genuinely different signal mechanism, not another
momentum variant — was tried next and landed in the same place**: a small
subset (6 products, 4 months) looked strong (AUC +0.098, a clean bucketed
win-rate progression 22%→67%), but that did not replicate at the full
universe/history scale. The small-sample result was noise, not signal —
the same shape of false positive seen throughout this project. See the
2026-09-13 entry below for the full account and what NOT to read into a
small-subset result going forward.

**Decision (2026-09-13)**: six independent evaluations (four momentum-family,
funding-rate, on-chain flow) now agree: none of these signal types has a
demonstrated edge on this product universe at this trade frequency. Simple
offline feature engineering on public price/funding/on-chain data does not
appear to be where the next win is, if there is one. Candidates not yet
tried: cross-asset macro regime (FRED yields/dollar index — confirmed
reachable, not yet backtested), options-implied vol/skew (Deribit, BTC/ETH
only, so market-wide regime feature not per-product), or accepting the bot
won't beat buy-and-hold on this design and changing the goal instead of the
feature set. Paid on-chain providers (Glassnode/CryptoQuant, deeper metrics
than DeFiLlama's free aggregate series) also untried.

## Engineering bugs that shaped what to trust in the above numbers

These aren't strategy findings — they're bugs in how the numbers themselves
were computed. Listed because they explain why earlier readings (e.g. the
2026-09-04 "+20.4%, -5.9pp gap" checkpoint) were themselves slightly
overstated, and because the pattern (silent, one-sided understatement,
invisible until something else briefly reveals it) is worth recognizing again
if it recurs.

- **Seeded-position accounting gap** (found 2026-09-12, fixed 2026-09-13):
  `reconstruct_equity` replayed trades *forward* from a fixed starting cash
  number, with zero starting holdings. But the paper epoch was actually
  seeded from real Coinbase balances (`seed_position` — deliberately writes
  no `trades` row, since seeding isn't a decision the bot made). Forward
  replay had no way to see those seeded holdings, so several products
  (BTC, DOGE, SHIB, XRP, ALGO, ETH) accumulated permanent phantom-short
  positions in the replay, silently understating the whole equity curve for
  the entire epoch — invisible until the final point (built from real
  ground truth) snapped back, which is what looked like a "spike" on the
  dashboard. **Fix**: rewrote `reconstruct_equity` to walk *backward* from
  today's known-true cash + positions, undoing one trade at a time — exact
  by construction, needs no knowledge of what was seeded. Only applies in
  paper mode (live mode's cash is the real total Coinbase balance, untouched
  by trades — backward-undo doesn't apply there).
  **Lesson**: any time state can be set by something other than the
  documented state-transition path (here: `seed_position` bypassing
  `trades`), a reconstruction that only knows about the documented path will
  drift, and it'll drift in one direction, quietly, until something
  independently ties back to ground truth.

- **Anchor timestamp raced its own opening trades** (found/fixed
  2026-09-12): the paper benchmark anchor was stamped with its own
  `utcnow()` call, taken *after* that cycle's trades had already executed
  with an earlier `now`. `reconstruct_equity`'s `ts_utc >= anchor_ts` filter
  then dropped the epoch's first real trades. **Fix**: pass
  `anchor_ts_utc=iso(now)` explicitly (mirrors what the live-mode path in
  `cli.py` already did correctly) so the anchor can never land after its own
  trades. **Lesson**: when two code paths need the same "instant," pass one
  timestamp value through both rather than calling `now()` twice — even a
  human eye can't see a sub-second race in a log.

- **Benchmark anchor's dollar basis, checked but not actually wrong**
  (2026-09-13): after fixing the equity curve, suspected the buy-hold
  benchmark's own starting dollar amount ($833.53 nominal) was also
  understated relative to the true seeded capital. Checked properly by
  pricing each seeded asset at its *actual spot price at the anchor moment*
  (via Coinbase historical candles) rather than at today's price — true
  value came out to $833.81, just 28 cents off nominal. Applied the
  precise correction anyway. **Lesson (on myself)**: don't reuse a number
  that's marked at "today's price" (the equity curve's own display
  convention) as if it were a point-in-time dollar value — those are two
  different meanings of "value" and conflating them produced a materially
  wrong first estimate ($853.82) before catching it.

## 2026-09-13: funding-rate backtest — a lesson in trusting small-subset results

Built `scripts/funding_rate_experiment.py`, mirroring `feature_experiment.py`'s
harness (same `compute_features()`/`signals.evaluate()` live code path, same
label/pnl math, same 5-fold blocked time-series CV). Coinbase is spot-only,
so funding-rate history came from Binance's perpetual futures — with two
data-access findings worth keeping:

- Binance's and Bybit's **live REST APIs** (`fapi.binance.com`,
  `api.bybit.com`) both return HTTP 451/403 "restricted location" from this
  host's network.
- Binance's **public historical-data bucket** (`data.binance.vision`) is
  NOT geo-restricted — it's static file hosting, not the trading API — and
  serves monthly funding-rate CSV archives per symbol back to each perp's
  listing date. That's what the script uses. One gotcha: the bucket only
  publishes a month's archive once the month is complete, so a naive
  "walk backward from the current month, stop at the first miss" loop
  breaks immediately on the in-progress month and never reaches any real
  history — had to special-case the first (current) month as an expected
  miss.

**First (dry) run — 6 products, 4 months, 537 buy-fired rows**: looked like a
real find. `corr(funding_cum_3d, pnl) = +0.32`, a clean win-rate progression
across quantile buckets (22% → 31% → 53% → 46% → 67%), AUC improved
**0.549 → 0.648 (+0.098)** with funding features added.

**Full run — 48 products, ~14 months, 12,145 buy-fired rows**: the effect
completely disappeared. AUC delta **+0.001** (0.509 → 0.510), all three
funding-derived features correlated with pnl at ~0 (+0.007, -0.026, +0.018),
and every quantile bucket sat flat at 39-43% win rate with no trend at all.

**Lesson**: this is the same false-positive shape seen throughout the
project (the Sep 4 model promotion on n=82 being the most expensive prior
instance) — a small subset can show an AUC bump or a clean-looking bucket
table purely from a handful of products' idiosyncratic behavior over a short
window, and it is not evidence of anything until it's checked at the full
product universe and the deepest available history. **Never report or act
on a small-subset backtest result before running the full-scale version** —
if there isn't time/data to run full-scale immediately, say the small result
is unconfirmed rather than leading with it.

## 2026-09-13 (later): on-chain capital flow — third mechanism, same verdict

Built `scripts/onchain_flow_experiment.py`, same harness pattern as the
funding-rate script. Data: DeFiLlama's free, unblocked, deep-history daily
series — aggregate USD-pegged stablecoin supply (`stablecoins.llama.fi`,
back to 2017-11-29) and aggregate DeFi TVL (`api.llama.fi`, back to
2017-09-27). Both are market-wide regime series (one number per day, shared
across every product), unlike funding rate which was per-product.

Ran the dry-run-then-full-scale discipline from the funding-rate lesson
correctly this time: small dry run (6 products, 4 months) showed a
*negative* delta (-0.045) with noisy, non-monotonic buckets — already a
different shape from the funding-rate false positive, and per the standing
rule, not treated as evidence either way.

**Full run (50 products, 13,557 buy-fired rows, 365 days): AUC delta -0.031
(0.508 → 0.477) — WORSE than baseline**, not just flat. Correlations with
pnl all ~0. Bucketed win rates noisy, no clean trend on any of the four
features. No demonstrated edge, and adding these features actively hurts
the classifier (more noise for the model to fit around, no signal to
compensate for it) — a slightly different failure shape from funding rate's
"strictly neutral," worth keeping in mind if this is scored on AUC-improve
rather than AUC-doesn't-regress.

## Open items

- Funding-rate/basis backtest — not started.
- A capital reset (re-seed paper trading from current real Coinbase
  balances) is planned for whenever the next research direction is ready to
  deploy, not yet executed. Current real balance (pulled 2026-09-13):
  SHIB 7,068,121.90, DOGE 625.22, XRP 462.45, ALEO 3.169258, ALGO 1.201816,
  ETH 0.03434226, BTC 0.00248627, USD $0.72 — essentially frozen at the
  2026-07-22 seed (paper mode never places real orders), except ALEO, which
  was present in the real account then too but got missed by the original
  seed (worth ~$0.057 at anchor-time price — negligible, but worth including
  correctly on the next seed).
