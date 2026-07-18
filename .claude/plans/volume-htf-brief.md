# grow-my-money — Volume Signal + Multi-Timeframe Confirmation — Concept Brief

## Problem
The bot trades on a single timeframe (`CANDLE_GRANULARITY=ONE_HOUR`) using only
price-derived indicators (EMA/RSI/MACD, `src/indicators.py`), with no use of
**volume** (even though OHLCV candles already include it —
`src/marketdata.py::candles_to_df` parses a `volume` column that nothing
currently reads) and no confirmation from a **higher timeframe**. This means
the bot can enter on thin, low-conviction moves (a small EMA cross with no
real volume behind it) and can be whipsawed by short-term 1h noise that
contradicts the broader trend. Both are well-established, cheap-to-add
technical filters that don't require any new data source (volume is already
fetched; a second timeframe is just another `fetch_candles` call with a
different granularity, both already supported by
`src/marketdata.GRANULARITY_SECONDS`).

## Goal
Two related, additive filters on **buy** decisions only:
1. **Volume confirmation** — weight buy conviction by whether current volume
   supports the move (e.g. above its recent average) vs. a thin/low-volume
   move.
2. **Multi-timeframe (HTF) confirmation** — check that a higher timeframe
   (`SIX_HOUR`, confirmed with the user) agrees with the primary 1h trend
   direction before sizing a buy at full conviction.

Both act as **soft dampeners on position size/conviction**, never hard
blocks — consistent with the bot's existing philosophy that de-risking
actions (sells) are never gated, and mirrors the same soft-dampener shape as
the news-sentiment feature (in progress as of this brief, see Constraints).

## In scope (v1)
- **Volume feature**: extend `src/indicators.py`'s `Features` dataclass with a
  volume-derived field — a **volume ratio** (current candle's volume ÷ a
  trailing simple moving average of volume, e.g. over 20 candles) computed in
  `compute_features` (same module/function that already computes EMA/RSI/MACD
  from the same `df`, so this is additive to an existing function, not a new
  data fetch).
- **HTF feature**: a second, independent candle fetch at `SIX_HOUR`
  granularity (reuse `marketdata.fetch_candles` with a different
  `granularity` arg — already parametrized) per product per cycle, reduced to
  just its trend direction (bullish/bearish via the same EMA-fast-vs-slow
  logic already in `compute_features`/`Features.ema_bullish` — no need to
  compute RSI/MACD for the HTF frame, only what's needed to know trend
  direction, though reusing `compute_features` wholesale on the HTF df is
  also acceptable if simpler and the extra computation is cheap).
- **Where the dampening lives**: `src/signals.py::evaluate()` and/or
  `src/bot.py::_decide_product`'s buy-sizing math. Concretely:
  - `signals.evaluate()` already computes a `Signal.confidence` in `[0,1]`
    (blend of EMA-gap and MACD-histogram strength) — **today this
    `confidence` value is computed but never consumed** (verified:
    `bot.py::_decide_product`'s buy-sizing `scale` is derived purely from
    `p_win` vs `cfg.buy_probability_threshold`; `sig.confidence` is unused).
    Extend the confidence computation to also factor in volume ratio and HTF
    agreement, and then **start using `sig.confidence` in the buy-sizing
    `scale` calculation** in `bot.py` (or pass volume/HTF factors through
    directly — planner's call on the cleanest wiring) so the dampening
    actually changes `notional`, not just an unused field.
  - This does **not** need to route through `src/risk.py` the way sentiment
    does. Reasoning (verified in this session while scoping the sentiment
    feature): the dashboard's `intents.reason` column is only ever populated
    from `RiskDecision.reason`, never from the original `TradeIntent.reason`
    (signal-composed reason) — but that's **already true today** for the
    existing `p_win`/model-tag reasoning too (it's logged via `log.info` in
    `src/execution.py` but never persisted to `intents.reason`). So a
    volume/HTF dampener that changes sizing the same way `p_win` scaling
    already does is consistent with existing, accepted behavior — no new gap
    is introduced, and no dashboard change is required for v1. (If in the
    future the signal-level reason should be dashboard-visible, that's a
    separate, pre-existing gap unrelated to this feature — out of scope
    here.)
  - **Sells remain completely untouched** — same as the model gate today,
    volume/HTF dampening applies to **buys only**. This preserves the bot's
    documented invariant that sells are "NEVER gated ... so the bot can
    always de-risk" (`src/signals.py` module docstring).
- **Config knobs** (mirroring the `dashboard_*`/`sentiment_*` pattern already
  established in `src/config.py`):
  - Volume: lookback window for the volume SMA (e.g.
    `volume_avg_window: int = 20`), and thresholds for what counts as
    "confirming" vs "thin" volume (e.g. `volume_confirm_ratio: float = 1.2`,
    `volume_thin_ratio: float = 0.7`).
  - HTF: granularity (`htf_candle_granularity: str = "SIX_HOUR"`, confirmed
    default with the user; keep it configurable per the same reasoning
    pattern used elsewhere in this project — env-tunable, sensible default),
    and a dampen factor when HTF disagrees (e.g.
    `htf_disagree_factor: float = 0.5`, applied to size/confidence the same
    shape as the sentiment feature's `sentiment_dampen_factor`).
- **Rate/perf note**: this roughly doubles per-cycle candle fetches (primary
  1h + HTF 6h per product, 12 products today) — still well within Coinbase's
  public candle endpoint limits at a 30-min decision interval, but the
  planner should confirm there's no obvious rate-limit concern given
  `src/coinbase_client.py`'s existing retry/backoff and 429 handling, and
  should make sure an HTF fetch failure fails **open** (dampener simply
  doesn't apply / defaults to neutral) rather than blocking the whole cycle
  for that product — mirror the try/except-per-product pattern already in
  `bot.py::run_cycle`'s loop (`except Exception: log.warning(...); continue`
  behavior already wraps `_decide_product` per product).
- **Tests**: unit tests for the new volume-ratio computation in
  `indicators.py` (given a synthetic DataFrame, assert the ratio), for HTF
  trend-agreement logic, and for the sizing effect (a low-volume or
  HTF-disagreeing scenario produces a smaller `notional` than an otherwise
  identical high-volume/HTF-agreeing scenario) — follow the existing test
  style in `tests/test_indicators.py` and `tests/test_model.py` (both work
  with synthetic feature/DataFrame fixtures already).

## Out of scope (v1)
- Hard-gating buys on volume/HTF disagreement (explicitly rejected by the
  user in favor of soft dampening) — no suppressed-buy path from this
  feature; a disagreeing volume/HTF signal should shrink size, never zero a
  buy out entirely by itself (that's what the model's `p_win` gate is for).
- Applying volume/HTF dampening to **sells** — sells stay fully rule-based
  and ungated, unchanged.
- Order-book depth / bid-ask spread signals (a related but distinct idea
  raised in the same conversation — deliberately deferred to a future brief,
  not bundled here to keep this change reviewable).
- Any change to `src/model.py`'s trained-model feature set or the ML
  training/promotion pipeline (volume ratio and HTF trend could theoretically
  become model features later, but that's a separate, larger change — v1
  only touches the rule-based `signals.py` layer + `bot.py` sizing).
- Any change to `src/risk.py`'s existing checks (the four fail-closed safety
  checks, and the new fail-open sentiment check currently being built in a
  parallel session — see Constraints) — this feature does not touch
  `risk.py` at all per the "where the dampening lives" decision above.
- A third+ timeframe, or a full multi-timeframe voting system — v1 is
  exactly two timeframes (primary + one HTF confirmation).

## Constraints
- **Coordinate with the concurrent news-sentiment build.** A separate,
  already-dispatched build (plan at
  `.claude/plans/news-sentiment-plan.md`) is modifying `src/risk.py`,
  `src/bot.py` (only `Bot.__init__`, to inject a `SentimentProvider` into
  `RiskManager`), `src/config.py`, `requirements.txt`, `docker-compose.yml`,
  `.env.example`, and `README.md` — **at the time this brief is written,
  that build may still be in flight.** This feature's implementation
  (Phase 4 of this feature's own lifecycle) **must not start until the
  sentiment build has completed and its changes are confirmed merged/present
  in the working tree** — both features touch `src/bot.py` and
  `src/config.py`, and running two build agents on the same files
  concurrently would corrupt both. The orchestrating session (not the build
  executor) is responsible for this sequencing — flag it, don't just assume
  it's handled.
- Match existing conventions: type hints, `Config` dataclass fields with env
  wiring via the existing `_get*` helpers, per-product try/except-and-continue
  fault tolerance in the decision loop (already the pattern in
  `bot.py::run_cycle`).
- `src/indicators.py::compute_features`'s existing signature and the
  `Features` dataclass are used elsewhere (`src/model.py`'s feature
  extraction, existing tests) — adding a new field must be additive
  (new field with a sensible default/computation) and must not change the
  meaning or presence of any existing field. Check `tests/test_indicators.py`
  and `tests/test_model.py` for exact current usage before changing the
  dataclass.
- No new external API/data source — volume comes from data already fetched;
  HTF is the same Coinbase candle endpoint at a different granularity,
  reusing `src/marketdata.fetch_candles`/`GRANULARITY_SECONDS` (`SIX_HOUR`
  already exists there — no new granularity constant needed).

## Acceptance criteria
1. `src/indicators.py`'s `Features` (or `compute_features`) gains a
   volume-ratio field computed from the existing `volume` column already
   parsed by `marketdata.candles_to_df` — provable with a unit test using a
   synthetic DataFrame with a known volume spike/dip.
2. A second candle fetch at the configured HTF granularity (`SIX_HOUR`
   default) happens per product per buy evaluation, and its trend direction
   (bullish/bearish) is computed and available to the dampening logic —
   provable with a unit test injecting a fake/stub candle fetch for the HTF
   frame.
3. A buy where volume is thin (below `volume_thin_ratio`) and/or the HTF
   trend disagrees with the primary-timeframe direction results in a
   **smaller `notional`** than an otherwise-identical scenario with
   confirming volume and HTF agreement — provable with a unit test comparing
   two `_decide_product`-level (or lower, if the planner isolates the sizing
   math into a testable pure function) scenarios that differ only in
   volume/HTF inputs.
4. No scenario in this feature ever fully suppresses a buy that the existing
   signal + model-gate logic would otherwise fire (no new "return None" path
   introduced by volume/HTF alone) — confirms the "soft dampener, not hard
   gate" requirement. A test should assert a thin-volume/HTF-disagreeing buy
   still executes (at reduced size), not that it's blocked.
5. Sells are provably unaffected — the existing sell path/tests continue to
   pass unmodified, and a new test confirms a sell signal ignores
   volume/HTF inputs entirely.
6. An HTF fetch failure (simulate: fake client raises) does not block or
   fail the cycle for that product — the buy proceeds using primary-timeframe
   signal + model gate only, dampening simply not applied (fail-open,
   mirroring the sentiment feature's philosophy but for a different reason:
   this is a data-availability issue, not a safety check).
7. Full existing test suite still passes:
   `.venv/bin/python -m pytest -q` (root:
   `/home/eric/projects/grow-my-money`) — baseline is 61 today, growing as
   the concurrent sentiment build lands its own new tests first.
8. `docker compose up -d --build` still brings the container up healthy
   (bot loop + dashboard both running) — verify via `docker compose logs app`
   and `curl 127.0.0.1:8420/`. Confirm the live paper-trading bot's decision
   cycles still complete without new errors after this change (check logs
   for a few cycles post-deploy).

## Open questions & decisions made
- **HTF granularity**: `SIX_HOUR`, confirmed by the user (a 6x higher
  timeframe than the primary `ONE_HOUR`), configurable via env var with that
  as the default.
- **Dampening mechanism (signals.py confidence vs. direct bot.py sizing
  multiplier)**: left to the Opus planner to choose the cleanest wiring
  after reading `signals.py` and `bot.py::_decide_product` in full — the
  brief author's lean is to feed volume+HTF into `Signal.confidence` (since
  that field already exists for exactly this purpose and is currently dead)
  and then have `bot.py` finally start consuming `sig.confidence` in its
  `scale` calculation alongside the existing `p_win`-derived scale (e.g.
  multiply them, or take a weighted combination) — but the planner should
  verify this doesn't produce surprising interaction effects with the
  existing `p_win` scaling and pick a clear, testable formula.
- **Sequencing with the sentiment build**: see Constraints — this feature's
  Phase 4 (execution) must wait for the sentiment build to finish and land.
  The orchestrating session already knows this; noted here so the plan
  reflects awareness of it even though it's a process constraint, not a code
  constraint.
- Plan approval gate: **skipped** at the user's request (matches earlier
  choices) — plan will be sanity-checked by this session, not shown to the
  user before build starts.

## Relevant files/areas
- `src/indicators.py` — `Features` dataclass, `compute_features()`; add the
  volume-ratio field here.
- `src/marketdata.py` — `fetch_candles()`, `candles_to_df()`,
  `GRANULARITY_SECONDS` (already has `SIX_HOUR`); reuse for the HTF fetch,
  no changes needed here unless the planner finds a reason to add a
  convenience wrapper.
- `src/signals.py` — `evaluate()`, `Signal.confidence` (currently computed,
  unused downstream) — likely where volume/HTF factors get folded in.
- `src/bot.py` — `_decide_product()` (buy-sizing `scale` calculation, where
  `sig.confidence` should start being consumed); `run_cycle()`'s
  per-product try/except pattern (mirror for HTF-fetch fault tolerance).
- `src/config.py` — add new `Config` fields for volume/HTF knobs, following
  the `dashboard_*`/`sentiment_*` pattern (check whichever of these exists
  in the tree by the time this feature's plan is written, given the
  concurrent sentiment build).
- `tests/test_indicators.py`, `tests/test_model.py` — existing `Features`
  usage to preserve; follow their fixture style for new tests.
- `.env.example` — document new env vars, matching existing block style.
- `.claude/plans/news-sentiment-plan.md` — read this to understand exactly
  what the concurrent build already changed in `bot.py`/`config.py`, so this
  feature's plan builds on top of it correctly rather than assuming the
  pre-sentiment state of those files.

## Repo commands & tree state
- **Test**: `.venv/bin/python -m pytest -q` (run from
  `/home/eric/projects/grow-my-money`; venv explicitly, nothing on PATH).
  61 passing as of this brief, before the concurrent sentiment build's new
  tests land.
- **Build/deploy container**: `docker compose up -d --build` (same
  directory).
- **Logs**: `docker compose logs -f --tail=200`.
- **Git tree state**: repo has never had its initial commit — whole tree
  `git status --short` shows as staged (`A`)/modified (`AM`). A second,
  concurrently-running build (news-sentiment feature) is actively modifying
  `src/risk.py`, `src/bot.py`, `src/config.py`, `requirements.txt`,
  `docker-compose.yml`, `.env.example`, `README.md`, plus new files
  `src/sentiment.py`, `tests/test_sentiment.py`,
  `tests/test_risk_sentiment.py`, `tests/test_secrets_cryptopanic.py`. **Do
  not start this feature's Phase 4 execution until that build is confirmed
  complete** (the orchestrating session tracks this).
- **Running container**: `grow-my-money-app-1` is up, running both the
  trading bot loop and the dashboard (`127.0.0.1:8420`), actively
  paper-trading against live Coinbase data with a real-account-mirrored
  starting state (~$1198 bankroll, 8 real positions). Do not disrupt its
  trading logic or state; expect live data in `state/grow.db` while
  building/testing.
