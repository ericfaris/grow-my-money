# grow-my-money — Volume Signal + Multi-Timeframe (HTF) Confirmation — Implementation Plan

Executor: you have this file and the repo, nothing else. Repo root:
`/home/eric/projects/grow-my-money`. Run tests with the venv explicitly:
`.venv/bin/python -m pytest -q`. Read the four modules named in "Key code facts"
before writing code — the design depends on their exact current behavior.

**Sequencing note (already handled by the orchestrator, stated for context):**
the concurrent news-sentiment build has **already landed** in the working tree.
Its changes are present in `src/config.py` (sentiment fields at lines ~124-132
and their `load_config` wiring at ~199-209), `src/risk.py` (a fifth fail-open
sentiment step and a keyword-only `sentiment=None` param on `RiskManager`), and
`src/bot.py` (imports `SentimentProvider`, builds `self.sentiment`, passes it to
`RiskManager`). **This plan is written against that post-sentiment state — do not
revert or touch any sentiment code.** New file `src/sentiment.py` and tests
`tests/test_sentiment.py`, `tests/test_risk_sentiment.py`,
`tests/test_secrets_cryptopanic.py` exist; leave them alone. This feature does
**not** touch `src/risk.py`, `src/sentiment.py`, or any sentiment logic at all.

---

## Summary

Add two cheap, additive **soft dampeners on buy sizing only**: (1) a **volume
ratio** (current candle volume ÷ trailing SMA of volume) computed inside
`indicators.compute_features` from the `volume` column that
`marketdata.candles_to_df` already parses but nothing reads; and (2) a
**higher-timeframe (HTF) trend confirmation** — a second candle fetch at
`SIX_HOUR` granularity per buy candidate, reduced to a single bullish/bearish
bit via the existing EMA-fast-vs-slow logic. Thin volume and/or an HTF trend that
disagrees with the primary 1h buy direction **shrink** the order's `notional`
(never zero it, never block it); confirming volume and an agreeing HTF leave
sizing at full conviction. Sells, the ML model, and the four fail-closed risk
checks are all untouched. HTF fetch failures fail **open** (dampener simply not
applied). No new external data source: volume is already fetched, HTF is the same
Coinbase candle endpoint at a different granularity.

---

## Key code facts (verified — trust these, but re-read to confirm)

1. **`compute_features`** (`src/indicators.py`, lines 61-106) already takes the
   whole `df` (which includes a `volume` column from `candles_to_df`) and returns
   a `Features` dataclass. It has keyword params like `vol_lookback: int = 24`.
   Adding a `volume_avg_window` kwarg and a new `Features` field is purely
   additive.
2. **The model's feature set is an EXPLICIT whitelist** — `FEATURE_KEYS`
   (`src/model.py` line 30): `["ema_gap_pct", "ema_bullish", "rsi", "macd_hist",
   "macd_bullish", "ret_recent", "volatility"]`. `features_to_row` does
   `d.get(k, 0.0) for k in FEATURE_KEYS`. **Therefore adding a `volume_ratio`
   field to `Features` does NOT enter the model** — it is ignored by
   `features_to_row`, `rule_pseudo_prob` (which reads named keys), and training.
   `state.record_outcome` stores `json.dumps(features)` (`src/state.py` line 282)
   so an extra JSON key is harmless. **Do NOT add `volume_ratio` to
   `FEATURE_KEYS`** — that would silently change the model feature set (out of
   scope).
3. **`Features` has no field defaults today** (all 12 fields are required,
   `src/indicators.py` lines 42-55). `tests/test_model.py::_feat` constructs
   `Features(**base)` with exactly those 12 keys. So a new `volume_ratio` field
   **must have a default and be placed LAST** in the dataclass (dataclass rule:
   defaulted fields follow non-defaulted ones), so `Features(**base)` keeps
   working with no test change. Default `1.0` = neutral (ratio of a candle at the
   average).
4. **`tests/test_indicators.py` line 63** asserts
   `set(f.to_vector().keys()) >= {...}` (superset) — adding a key passes
   unchanged.
5. **Buy sizing** (`src/bot.py::_decide_product`, lines 184-199): after the
   `p_win` model gate, `budget = per_trade_budget_fraction * bankroll`, then
   `scale = min(1.0, (p_win - threshold)/max(1e-6, 1-threshold) + 0.5)` (roughly
   [0.5, 1.0]), then `notional = budget * scale`. **`sig.confidence` is computed
   in `signals.evaluate` but never read here** — verified.
6. **`Signal`** (`src/signals.py` lines 15-20) is a **mutable** `@dataclass`
   (`product, direction, confidence, reason`). `evaluate` computes a base `conf`
   in [0,1] from EMA gap + MACD histogram strength (lines 35-37), builds the buy
   `Signal` at lines 40-41, sells at 42-44, holds at 45-48.
7. **Per-product fault tolerance** already wraps `_decide_product` in
   `run_cycle`'s `for product` loop (`src/bot.py` lines 146-156):
   `except Exception: log.warning(...)` and continue. An HTF fetch that raises
   inside `_decide_product` would abort that product's cycle — so the HTF fetch
   must have its **own** try/except returning `None` (fail-open), NOT rely on the
   outer loop.
8. **`marketdata.fetch_candles(client, product_id, granularity, limit)`**
   (lines 30-43) is already fully parametrized on `granularity`;
   `GRANULARITY_SECONDS` already contains `"SIX_HOUR": 21600` (line 25). Reuse
   as-is; no marketdata change needed.
9. **`RiskManager` is untouched by this feature.** Sizing dampening happens in
   `bot.py` **before** the intent is built, so the reduced `notional` flows
   through `gateway.execute → risk.check` exactly like any other buy notional
   (the per-position cap can still resize it further down; that's fine and
   correct — dampening is strictly softer than the hard cap).

---

## Approach & key decisions

### Decision 1 — RESOLVES the brief's open question: an explicit `size_factor`, computed in `bot.py`'s buy branch, NOT folded into the weak `confidence` blend

The brief asks precisely: *how do volume+HTF factors feed into `Signal.confidence`
and how does `bot.py`'s buy-sizing `scale` consume it?* Resolution:

**Do not multiply `scale` (or `notional`) by `sig.confidence`.** `conf` (lines
35-37 of `signals.py`) is a *trend-strength magnitude* (`0.5*gap_strength +
0.5*hist_strength`), and for ordinary moves it is a small number well below 1.0
(a 5% EMA gap is needed just to reach `gap_strength = 1.0`). Multiplying the
existing `p_win`-derived `scale` by that raw `confidence` would shrink **every**
buy dramatically and conflate trend strength with the volume/HTF dampener — the
"surprising interaction" the brief explicitly warns against. It would also break
the requirement that a **confirming** volume + **agreeing** HTF leave sizing at
full conviction (a fully-confirming buy must multiply by 1.0, not by a small
`conf`).

**Chosen design:** compute a dedicated multiplicative **`size_factor ∈ (0, 1]`**
that is exactly `1.0` in the fully-confirming case and `< 1.0` only when volume is
thin and/or HTF disagrees. It is produced by a **pure function**
`signals.buy_size_factor(volume_ratio, htf_bullish, cfg)` (independently unit
testable — satisfies criteria 3 & 4 at the lowest level), and `bot.py` consumes
it directly:

```
notional = budget * scale * size_factor
```

`size_factor` is orthogonal to the `p_win` `scale`: `scale` answers "how
convinced is the model?", `size_factor` answers "does volume + the higher
timeframe back this up?". Multiplying keeps both effects independent and legible.

**On `Signal.confidence`:** to give the currently-dead field meaning (the brief's
secondary wish) without creating the interaction problem, `evaluate` will, **for
buy signals only**, additionally set `confidence = base_conf * volume_factor`
(volume component only, since `volume_ratio` is on `feats` and free inside
`evaluate`; HTF is not available there). This makes `confidence` reflect volume
conviction for observability/logging. **But `bot.py` sizing uses `size_factor`,
not `confidence`** — `confidence` remains advisory. Rationale for not routing
sizing through `confidence`: as above, it is a small magnitude, and threading HTF
into `evaluate` would force an HTF fetch for every product on every cycle (see
Decision 3). Sells and holds keep their base `conf` unchanged (their sizing never
consults volume/HTF — criterion 5).

**Rejected alternatives:**
- *Multiply `scale`/`notional` by raw `sig.confidence`* — rejected: shrinks all
  buys, conflates trend strength with the dampener, no full-conviction case.
- *Fold everything (incl. HTF) into `Signal.confidence` inside `evaluate`* —
  rejected: `evaluate` has no access to the HTF frame; passing it in forces an
  HTF fetch for every product every cycle (12× extra fetches) rather than only
  for buy candidates that clear the model gate.

### Decision 2 — `size_factor` shape (concrete formula)

Pure function in `src/signals.py`:

```python
def buy_size_factor(volume_ratio, htf_bullish, cfg) -> float:
    """Multiplicative buy-size dampener in (0, 1]. 1.0 = fully confirming.
    Volume and HTF are independent soft factors; either missing => neutral (1.0
    for that factor). NEVER returns 0 (soft dampener, never a hard block)."""
    factor = 1.0
    # --- Volume confirmation ---
    if volume_ratio is not None:
        if volume_ratio <= cfg.volume_thin_ratio:
            factor *= cfg.volume_thin_factor            # thin -> full dampen
        elif volume_ratio < cfg.volume_confirm_ratio:
            span = cfg.volume_confirm_ratio - cfg.volume_thin_ratio
            frac = (volume_ratio - cfg.volume_thin_ratio) / span if span > 0 else 1.0
            factor *= cfg.volume_thin_factor + frac * (1.0 - cfg.volume_thin_factor)
        # volume_ratio >= volume_confirm_ratio -> *1.0 (full conviction)
    # --- HTF confirmation (buy is bullish; disagreement = HTF not bullish) ---
    if htf_bullish is not None and not htf_bullish:
        factor *= cfg.htf_disagree_factor
    return max(0.0, min(1.0, factor))
```

Notes:
- Between `volume_thin_ratio` and `volume_confirm_ratio` the volume factor ramps
  **linearly** from `volume_thin_factor` up to `1.0` (avoids a cliff; smooth and
  easy to reason about in tests).
- `htf_bullish is None` (fetch failed/insufficient data) ⇒ HTF factor is neutral
  (fail-open, criterion 6). `htf_bullish is True` (agrees with a bullish buy) ⇒
  neutral. Only an explicit `False` (disagrees) dampens.
- The result is `> 0` for any sane config (all factors default `> 0`), so this
  path **never** produces a zero/blocked buy on its own (criterion 4).
- `volume_thin_factor` is a **new** config knob (see Decision 4) — the brief
  named `htf_disagree_factor` but did not name a volume dampen factor; I add one
  for symmetry and testability rather than hard-coding a magic number.

### Decision 3 — HTF fetch: buy candidates only, in `bot.py`, fail-open

The HTF candle fetch happens inside `_decide_product`'s **buy branch, after the
`p_win` gate passes** (i.e. only for products that would actually place a buy).
This minimizes extra fetches (not "every product every cycle" — only realized
buy candidates), and keeps `signals.evaluate`'s signature unchanged. A new
private helper `Bot._htf_trend(product) -> Optional[bool]` performs the fetch +
`compute_features` and returns `feats.ema_bullish` as a bool, wrapped in its own
`try/except` that logs a warning and returns `None` on **any** failure (empty df,
insufficient candles, client error). Returning `None` ⇒ HTF factor neutral.
Reusing `compute_features` wholesale on the HTF df is acceptable per the brief
(the extra RSI/MACD compute is cheap); we only read `.ema_bullish`.

### Decision 4 — Config knobs (added to the `Config` dataclass, after the sentiment block)

Mirror the existing `dashboard_*` / `sentiment_*` pattern in `src/config.py`
(frozen dataclass field + `_get*` wiring in `load_config`). Do **not** touch
`RiskConfig` (these are not hard safety caps).

### Decision 5 — Volume ratio definition

`volume_ratio = current_candle_volume / SMA(volume, volume_avg_window)`, computed
in `compute_features` from `df["volume"]`. Guard rails: if the `volume` column is
absent, or the window SMA is `0`/`NaN`, default to `1.0` (neutral). With fewer
than `volume_avg_window` candles, average over whatever is available. Placed as
the **last** `Features` field with default `1.0` (Key fact #3).

---

## Step-by-step tasks (ordered, each independently verifiable)

### Task 1 — Add `volume_ratio` to `Features` + compute it (`src/indicators.py`)
1. Add a new field **at the end** of the `Features` dataclass (after
   `volatility`), with a default:
   ```python
   volume_ratio: float = 1.0     # current volume / trailing SMA(volume); 1.0 = neutral/unknown
   ```
   (It must be last and defaulted — Key fact #3. `to_vector()` via `asdict`
   automatically includes it.)
2. Add a keyword param to `compute_features`: `volume_avg_window: int = 20`
   (alongside the existing `vol_lookback=24` etc.).
3. In `compute_features`, after the existing computations and before building the
   `Features(...)`, compute the ratio defensively:
   ```python
   volume_ratio = 1.0
   if "volume" in df.columns and volume_avg_window > 0:
       vol = df["volume"].astype(float).reset_index(drop=True)
       if len(vol):
           sma = float(vol.tail(volume_avg_window).mean())
           cur = float(vol.iloc[-1])
           if sma > 0 and not np.isnan(sma):
               volume_ratio = cur / sma
   ```
   Pass `volume_ratio=volume_ratio` into the `Features(...)` constructor.
   (`np` is already imported.)

Verify: `.venv/bin/python -m pytest -q tests/test_indicators.py tests/test_model.py`
(existing tests must still pass unchanged — proves the additive-field claim).

### Task 2 — Config knobs (`src/config.py`)
Add to the `Config` frozen dataclass, in a new block **after** the sentiment
fields (after line ~132), with defaults:
```python
# Volume confirmation + higher-timeframe (HTF) trend dampener (buys only).
volume_avg_window: int = 20
volume_confirm_ratio: float = 1.2
volume_thin_ratio: float = 0.7
volume_thin_factor: float = 0.6
htf_candle_granularity: str = "SIX_HOUR"
htf_disagree_factor: float = 0.5
```
Wire each in `load_config`'s `Config(...)` construction (after the sentiment
lines ~199-209), using the existing helpers:
```python
volume_avg_window=_get_int(env, "VOLUME_AVG_WINDOW", 20),
volume_confirm_ratio=_get_float(env, "VOLUME_CONFIRM_RATIO", 1.2),
volume_thin_ratio=_get_float(env, "VOLUME_THIN_RATIO", 0.7),
volume_thin_factor=_get_float(env, "VOLUME_THIN_FACTOR", 0.6),
htf_candle_granularity=_get(env, "HTF_CANDLE_GRANULARITY", "SIX_HOUR"),
htf_disagree_factor=_get_float(env, "HTF_DISAGREE_FACTOR", 0.5),
```
No new validation required (fail-open ethos — out-of-range knobs should not
raise; leave `Config.validate` and `RiskConfig` untouched).

Verify: `.venv/bin/python -c "from src.config import load_config; c=load_config(env={}, use_dotenv=False); print(c.volume_confirm_ratio, c.htf_candle_granularity, c.htf_disagree_factor)"`
→ `1.2 SIX_HOUR 0.5`.

### Task 3 — `buy_size_factor` pure function + optional confidence enrichment (`src/signals.py`)
1. Add the `buy_size_factor(volume_ratio, htf_bullish, cfg) -> float` function
   exactly as in Decision 2. Put it at module level (import-free; uses only
   `cfg` attributes).
2. In `evaluate`, in the **buy branch only** (lines 39-41), enrich confidence
   with the volume component so the field is no longer dead:
   ```python
   if bullish and not overbought:
       vol_factor = buy_size_factor(feats.volume_ratio, None, cfg)  # volume-only, HTF applied later in bot.py
       conf = max(0.0, min(1.0, conf * vol_factor))
       return Signal(product, "buy", conf, "<same reason string as today>")
   ```
   Passing `htf_bullish=None` here means only the volume component affects
   `confidence` (HTF is applied at sizing time in `bot.py`). **Do not** change the
   sell or hold branches — their `conf` stays exactly as today (criterion 5).
   Keep the buy `reason` string byte-for-byte as it is now.

Verify: covered by Task 6 tests.

### Task 4 — HTF trend helper + consume `size_factor` in sizing (`src/bot.py`)
1. Add a private helper on `Bot` (fail-open, Key facts #7):
   ```python
   def _htf_trend(self, product: str) -> Optional[bool]:
       """Higher-timeframe trend bit for the buy-size dampener. Fail-OPEN:
       any failure returns None (dampener simply not applied)."""
       try:
           df = marketdata.fetch_candles(
               self.client, product, self.cfg.htf_candle_granularity,
               self.cfg.candle_limit)
           if df.empty:
               return None
           f = compute_features(
               df, ema_fast=self.cfg.ema_fast, ema_slow=self.cfg.ema_slow,
               rsi_period=self.cfg.rsi_period, macd_fast=self.cfg.macd_fast,
               macd_slow=self.cfg.macd_slow, macd_signal=self.cfg.macd_signal,
               volume_avg_window=self.cfg.volume_avg_window)
           return bool(f.ema_bullish)
       except Exception as exc:  # noqa: BLE001 — fail-open
           log.warning("HTF trend fetch failed for %s (%s); proceeding without "
                       "HTF dampening", product, exc)
           return None
   ```
2. In `_decide_product`, pass `volume_avg_window` into the primary
   `compute_features` call (lines 163-167) so `feats.volume_ratio` is populated
   with the configured window:
   ```python
   feats = compute_features(
       df, ema_fast=self.cfg.ema_fast, ema_slow=self.cfg.ema_slow,
       rsi_period=self.cfg.rsi_period, macd_fast=self.cfg.macd_fast,
       macd_slow=self.cfg.macd_slow, macd_signal=self.cfg.macd_signal,
       volume_avg_window=self.cfg.volume_avg_window,
   )
   ```
3. In the **buy branch, after the `p_win` gate passes** (after line 189, before
   `budget = ...`), compute the dampener and apply it:
   ```python
   htf_bullish = self._htf_trend(product)
   size_factor = signals.buy_size_factor(feats.volume_ratio, htf_bullish, self.cfg)
   budget = self.cfg.per_trade_budget_fraction * self.portfolio.bankroll
   scale = min(1.0, (p_win - self.cfg.buy_probability_threshold)
               / max(1e-6, 1.0 - self.cfg.buy_probability_threshold) + 0.5)
   notional = budget * scale * size_factor
   htf_txt = "n/a" if htf_bullish is None else ("bull" if htf_bullish else "bear")
   reason = (f"{sig.reason}; p_win={p_win:.3f} model={tag}; "
             f"vol_ratio={feats.volume_ratio:.2f} htf={htf_txt} size_x={size_factor:.2f}")
   intent = TradeIntent(product=product, side="buy", notional=notional, reason=reason)
   ```
   (This replaces the existing `budget`/`scale`/`notional`/`reason` lines 190-195
   — keep the surrounding `predict_p_win`, suppression `log.info`, and
   `gateway.execute`/return unchanged.)

Verify: `.venv/bin/python -m pytest -q` (whole suite) after Task 6.

### Task 5 — `.env.example` docs
Add a `# --- Volume + HTF confirmation (buys only) ---` block documenting
`VOLUME_AVG_WINDOW`, `VOLUME_CONFIRM_RATIO`, `VOLUME_THIN_RATIO`,
`VOLUME_THIN_FACTOR`, `HTF_CANDLE_GRANULARITY`, `HTF_DISAGREE_FACTOR` with their
defaults, matching the existing block style (mirror the sentiment block the
concurrent build added). No secrets. No `docker-compose.yml` change is needed
(no new secret/volume — HTF reuses the existing Coinbase client).

### Task 6 — Tests (new file; do not modify existing tests)
Create **`tests/test_volume_htf.py`**. Use existing `conftest` fixtures
(`config`, `state`, `killswitch`, `price_source`, `prices`). Cover:

**A. Volume ratio in `compute_features` (criterion 1):**
- Build a synthetic df of ~60 rows with a flat `close` uptrend and a `volume`
  column that is constant except a **spike on the last candle** → assert
  `compute_features(df, volume_avg_window=20).volume_ratio > 1.0`.
- Same with a **dip on the last candle** → `volume_ratio < 1.0`.
- A df with **no `volume` column** → `volume_ratio == 1.0` (neutral default).
- (Optional) constant volume → `volume_ratio ≈ 1.0`.

**B. `buy_size_factor` pure logic (criteria 3 & 4):**
- Confirming volume (`ratio >= volume_confirm_ratio`) + `htf_bullish=True` →
  `== 1.0`.
- Thin volume (`ratio <= volume_thin_ratio`) + `htf_bullish=True` →
  `== config.volume_thin_factor` (and `> 0`).
- Confirming volume + `htf_bullish=False` → `== config.htf_disagree_factor`.
- Thin volume + `htf_bullish=False` → `== volume_thin_factor * htf_disagree_factor`
  and strictly `> 0` (criterion 4: never zero).
- `volume_ratio=None, htf_bullish=None` → `== 1.0` (fail-open).
- A mid-range ratio (between thin and confirm) with `htf_bullish=True` → strictly
  between `volume_thin_factor` and `1.0` (linear ramp).

**C. Sizing effect end-to-end via a Bot (criteria 3, 4, 6) — smaller notional,
still fires:** Build a `Bot` with a fake client. Pattern for the fake client
(mirror `SpyClient` in `tests/test_execution_paper.py` + the candle shape from
`src/coinbase_client.get_candles`):
```python
class FakeClient:
    def __init__(self, primary_candles, htf_candles, spot):
        self._primary = primary_candles      # list[dict] with keys start,low,high,open,close,volume
        self._htf = htf_candles
        self._spot = spot
    def get_candles(self, product_id, granularity, start, end, limit=None):
        return self._htf if granularity == "SIX_HOUR" else self._primary
    def get_spot_price(self, product_id):
        return self._spot
```
Construct candle dicts so the **primary** frame yields a BUY (EMA bullish + MACD
bullish + RSI not overbought) and a passing `p_win` (cold-start rule prob for a
bullish feature is > 0.5; ensure it clears `buy_probability_threshold=0.55` — if
cold-start prob is borderline, lower the threshold in a per-test `config`
override, e.g. `config.__class__(**{**config.__dict__, "buy_probability_threshold": 0.5})`,
matching the `_seed`-style overrides in `tests/test_model.py`). Then:
- Scenario **confirming**: primary volume spike on last candle (ratio high) + HTF
  candles that are bullish → run `bot._decide_product("BTC-USD")` (or
  `run_cycle`) and read the recorded trade notional
  (`state.all_trades()[-1]["notional"]`).
- Scenario **thin/disagree**: identical primary *close* series but a volume dip on
  the last candle (thin) and/or HTF candles that are bearish → notional strictly
  **smaller** than the confirming scenario, and the trade **still fills**
  (`status == "filled"`, a trade row exists) — proves soft-dampen, not block
  (criterion 4). Use separate `state`/db per scenario (fresh `Bot`) to avoid the
  per-position cap / trade-count cap interfering; or assert the ratio of
  notionals rather than absolute values.
- Scenario **HTF fetch fails** (criterion 6): a fake client whose `get_candles`
  **raises** when `granularity == "SIX_HOUR"` but returns the primary frame
  otherwise → the buy still fills at the **un-dampened-by-HTF** size (equal to a
  volume-confirming, HTF-neutral run); no exception escapes `_decide_product`.

**D. Sells unaffected (criterion 5):** feed a primary frame that yields a SELL
(EMA cross-down) for a product with an open position; assert the sell fills and
that `_htf_trend`/`buy_size_factor` are **not** consulted for it (e.g. use a fake
client whose `get_candles` raises on `SIX_HOUR` — the sell must still succeed,
proving the sell path never fetches HTF). Simplest: assert a sell fills normally
with a client that has no HTF data at all.

Verify each block runs green:
`.venv/bin/python -m pytest -q tests/test_volume_htf.py`.

---

## Data / model / API changes

- **`Features` dataclass**: +1 field `volume_ratio: float = 1.0` (last, defaulted).
  Appears in `to_vector()` but **excluded from the model** (not in `FEATURE_KEYS`).
- **No DB schema change.** `outcomes.features_json` gains an extra harmless key.
- **No `src/model.py` change** (out of scope; do not add `volume_ratio` to
  `FEATURE_KEYS`).
- **No `src/risk.py` change.** Dampening changes `notional` before the intent
  reaches `risk.check`.
- **New `Config` fields** (Task 2): `volume_avg_window`, `volume_confirm_ratio`,
  `volume_thin_ratio`, `volume_thin_factor`, `htf_candle_granularity`,
  `htf_disagree_factor` — all optional with safe defaults.
- **New env vars** (Task 5): `VOLUME_AVG_WINDOW`, `VOLUME_CONFIRM_RATIO`,
  `VOLUME_THIN_RATIO`, `VOLUME_THIN_FACTOR`, `HTF_CANDLE_GRANULARITY`,
  `HTF_DISAGREE_FACTOR`.
- **External API**: none new. HTF is `marketdata.fetch_candles(..., "SIX_HOUR")`,
  the same Coinbase endpoint with existing retry/backoff/429 handling in
  `coinbase_client`. Roughly doubles candle fetches **for buy candidates only**
  (not all 12 products/cycle) — well within limits at a 30-min interval.

---

## Testing & verification (maps to acceptance criteria)

- **#1 (volume-ratio field from a synthetic df)**: `tests/test_volume_htf.py`
  block A.
- **#2 (HTF fetch per buy eval → trend bit available)**: block C uses a fake
  client whose `SIX_HOUR` branch is exercised; assert dampening reflects HTF
  direction.
- **#3 (thin/disagree → smaller notional)**: blocks B + C (confirming vs
  thin/disagree notionals).
- **#4 (never fully suppresses; still fires)**: block B (factor `> 0` always) +
  block C (thin/disagree scenario still `status == "filled"`).
- **#5 (sells unaffected)**: block D + existing sell tests unchanged.
- **#6 (HTF fetch failure → fail-open)**: block C "HTF fetch fails" scenario.
- **#7 (full suite)**: `.venv/bin/python -m pytest -q` from repo root — expect the
  post-sentiment baseline **plus** the new tests, no regressions (pre-existing
  sklearn warnings are fine).
- **#8 (container healthy)**:
  ```
  docker compose up -d --build && sleep 5
  docker compose logs app --tail=120        # bot loop + dashboard both up; no new errors
  curl -sf -o /dev/null -w '%{http_code}\n' 127.0.0.1:8420/   # 200
  ```
  Then watch a couple of decision cycles: `docker compose logs -f --tail=200` —
  buys should log the new `vol_ratio=.. htf=.. size_x=..` reason suffix; no HTF
  fetch exceptions. **Do not disrupt the running `grow-my-money-app-1` trading
  state.**

Suggested final sequence:
```
.venv/bin/python -m pytest -q
docker compose up -d --build && sleep 5
docker compose logs app --tail=120
curl -sf -o /dev/null -w '%{http_code}\n' 127.0.0.1:8420/
```

---

## Risks & watch-outs

1. **`volume_ratio` must NOT enter the model.** `FEATURE_KEYS` is an explicit
   whitelist — leave it as-is. Adding the field to the dataclass is safe *only*
   because of that whitelist; do not "helpfully" add it to `FEATURE_KEYS` or to
   `rule_pseudo_prob`.
2. **New `Features` field must be last and defaulted.** Otherwise
   `tests/test_model.py::_feat` (`Features(**base)` with the 12 current keys)
   breaks, and the dataclass itself won't compile (non-default after default).
3. **HTF fetch must fail open on its own.** Wrap `_htf_trend` in try/except
   returning `None`; do **not** lean on `run_cycle`'s outer per-product handler
   (that would abort the buy for that product instead of proceeding un-dampened).
4. **Do not multiply sizing by raw `sig.confidence`.** Use the explicit
   `size_factor`. Raw confidence is a small trend-strength magnitude and would
   shrink all buys / break the full-conviction case (Decision 1).
5. **`size_factor` must never be 0.** All factors default `> 0` and
   `buy_size_factor` clamps to `(0,1]` in practice; this is what keeps criterion 4
   (no new suppression path). Do not add a "if factor too low, reject" branch.
6. **HTF fetch only for buy candidates that clear the `p_win` gate.** Put the
   `_htf_trend` call **after** the `p_win < threshold` early-return, not before —
   otherwise you fetch HTF for buys that get suppressed anyway (wasteful) and for
   holds (which return earlier).
7. **Order of operations in the buy branch:** `predict_p_win` → gate →
   `_htf_trend` → `buy_size_factor` → `budget*scale*size_factor` → build intent.
   The per-position cap in `risk.py` may still resize the (already dampened)
   notional down further — that is correct and expected; don't try to prevent it.
8. **Sells/holds must not fetch HTF or apply the factor.** The sell and hold
   branches return before the buy sizing block today — keep it that way; only the
   buy branch calls `_htf_trend`/`buy_size_factor`.
9. **Do not touch sentiment code or `src/risk.py`.** They landed from the
   concurrent build; this feature is orthogonal.
10. **`compute_features` window on the HTF frame:** pass `volume_avg_window` too
    (harmless — we only read `ema_bullish`) so the call signature stays uniform;
    ensure `candle_limit` (300) ≥ `ema_slow + 1` at SIX_HOUR (it is).

---

## Out of scope (do not build)

- **Hard-gating** buys on volume/HTF disagreement — soft dampener only; no new
  `return None`/reject path from this feature.
- Applying volume/HTF dampening to **sells** — sells stay fully rule-based and
  ungated.
- **Order-book depth / bid-ask spread** signals — deferred to a future brief.
- Any change to **`src/model.py`** (`FEATURE_KEYS`, training, promotion) — volume
  ratio is deliberately excluded from the model.
- Any change to **`src/risk.py`** or the sentiment feature.
- A **third+ timeframe** or a full multi-timeframe voting system — exactly one HTF
  (`SIX_HOUR`) confirmation.
- Dashboard UI changes — the new sizing reason rides the existing `reason`
  surfacing (and, as the brief notes, signal-level reasons aren't persisted to
  `intents.reason` today; that pre-existing gap is not addressed here).
