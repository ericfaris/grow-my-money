# grow-my-money — News/Sentiment Signal — Implementation Plan

Executor: you have this file and the repo, nothing else. Read the four modules
named in "Key code facts" below before writing code — the design depends on
their exact behavior. Repo root: `/home/eric/projects/grow-my-money`. Run tests
with the venv explicitly: `.venv/bin/python -m pytest -q` (61 passing today).

---

## Summary

Give the bot a lightweight, fail-open news-sentiment awareness that can **dampen
(resize down) or veto (reject) a buy** the technicals + ML model would otherwise
approve, using CryptoPanic's free API (community `votes` / `panic_score`), with
**zero** changes to the ML model, to the four existing fail-closed safety checks,
or to sell handling. It ships **inert** — until Eric creates a CryptoPanic token
file, every sentiment fetch fails open and the bot trades exactly as it does
today. When a token is present, a strongly-bearish product gets its buys
softened or blocked, and the reason ("...bearish sentiment...") shows up
automatically in the existing dashboard activity feed with no dashboard code
changes.

---

## Key code facts (verified — trust these, but re-read to confirm)

1. **Buy flow** (`src/bot.py::_decide_product`, lines ~180-195): `signals.evaluate`
   → model `p_win` gate → compute `notional = budget * scale` → build
   `TradeIntent(side="buy", notional, reason)` → `self.gateway.execute(intent)`.
   `gateway.execute` (`src/execution.py`) calls `self.risk.check(intent)`.
2. **The `reason` shown on the dashboard is the `RiskDecision.reason`, NOT the
   signal reason.** In `src/execution.py::OrderGateway.execute`, `record_intent(...)`
   is called **without** a reason; then `update_intent_risk(coid, decision.action,
   decision.adjusted_notional, decision.reason)` writes the `intents.reason`
   column (`src/state.py` line ~165). `src/dashboard_data.py::build_activity`
   reads `intent["reason"]`. Therefore a reason string is only surfaced to the
   dashboard if it comes out of `RiskManager.check`.
3. **`RiskManager.check` wraps `_check` in a try/except that FAILS CLOSED**
   (`src/risk.py` lines 60-66): any exception → `reject`. A sentiment call that
   raises inside `_check` would therefore turn into a REJECT — the opposite of
   fail-open. The sentiment step must catch its own exceptions internally.
4. **The four fail-closed checks are buys-only where it matters**: halt and
   per-position cap run only `if is_buy`; sells fall through with
   `working_notional = intent.notional`. The final `RiskDecision` is built at
   `src/risk.py` lines 107-109 from `working_notional` + `resize_reason`.
5. **All existing `RiskManager(...)` call sites pass exactly 5 positional args**
   (`config, state, portfolio, killswitch, price_source`) — verified across
   `tests/test_killswitch.py`, `tests/test_risk_portfolio_halt.py`,
   `tests/test_risk_per_position_cap.py`, `tests/test_risk_daily_trade_cap.py`,
   `tests/test_execution_paper.py`. A new **optional keyword-only** param is
   therefore safe and keeps criterion 4 (those tests unmodified) satisfied.
6. **Secrets convention** (`src/secrets.py`): `_resolve(configured, host,
   container)` prefers an explicit path, else the container path if it exists,
   else the host path; `_check_mode_600` warns (never fails); the public getter
   raises `SecretError` if the file is absent.
7. **TTL-cache template**: `src/dashboard_data.py::PriceProvider` — a
   `dict[str, tuple[value, ..., monotonic_ts]]` cache, refreshed when
   `time.monotonic() - ts >= ttl`, degrades per-product and never raises.

---

## Approach & key decisions

### Decision 1 — Integration point: `risk.py` (RESOLVES the brief's open question)

**Chosen: `src/risk.py`**, as a new **fifth, clearly-delineated, fail-OPEN**
check that runs *after* the four existing fail-closed checks, buys only.

Rationale (decisive point first):

- **Only a `RiskDecision.reason` reaches the dashboard** (Key fact #2). The brief
  requires the dampen/veto to appear in the activity feed's `reason`
  ("rejected: bearish sentiment (panic_score=82)"). If we integrated in
  `signals.py`, a suppressed buy returns `None` in `bot.py` → **no intent row is
  ever recorded** → nothing on the dashboard; and a resized buy would still show
  the `RiskDecision.reason` ("approved"), so the sentiment reason would be
  **invisible**. The brief explicitly calls out "if the chosen integration point
  doesn't naturally produce a visible `reason`, that's a plan defect to catch."
  `risk.py` produces a visible reason for free.
- `risk.check` is already **the single chokepoint every order passes through**
  (its docstring), so "does this buy proceed, and at what size" logic lives in
  one place. Resize semantics (`action="resize"`, `adjusted_notional`) and reject
  semantics already exist and thread through `execution.py` → `state` → dashboard
  unchanged.
- Buys-only and sells-exempt fall out naturally: the sentiment step sits inside
  the same `if is_buy` structure, so sells are untouched (criterion 4) with no
  extra guarding.

Rejected alternative — **`signals.py`**: structurally it looks clean (adjust
`Signal.confidence`/`reason` before the intent exists), but (a) it can't surface
a visible reason for a *suppressed* buy (returns None, no row), (b) it would
require also passing a sentiment provider down into `bot.py`/`signals.evaluate`
and re-plumbing size scaling there, and (c) it splits "reasons a buy is
shrunk/blocked" across two modules. Rejected.

### Decision 2 — Keep it fail-OPEN without weakening fail-closed

The sentiment check is **additive and last**. It must never turn into a reject
due to an infrastructure problem. Two layers of protection:

1. The `SentimentProvider` itself returns `None` (never raises) on any failure:
   no token, `SecretError`, HTTP error, timeout, non-200, malformed/empty JSON,
   or no vote data.
2. **Inside `_check`, the sentiment sub-step is wrapped in its own
   `try/except`** that logs and returns "proceed unchanged" on *any* exception —
   so even a bug in the provider cannot bubble up to `check`'s fail-closed outer
   handler (Key fact #3). This is the single most important correctness point in
   the whole feature.

`None`/neutral sentiment ⇒ the buy proceeds with exactly the notional the four
prior checks produced.

### Decision 3 — Inert by default (ships with no token)

- New optional keyword-only param `sentiment=None` on `RiskManager.__init__`.
  When `None`, the sentiment step is a no-op. Existing 5-arg constructions →
  inert, tests unchanged.
- `bot.py` constructs a `SentimentProvider` and injects it **only if**
  `cfg.sentiment_enabled` is true. With no token file present, the provider is
  still injected but every lookup fails open (returns `None`).
- The docker-compose bind mount for the token is added **commented out** (see
  Decision 5) so `docker compose up` does not auto-create a root-owned directory
  at a non-existent host path. Eric uncomments it after placing the token.

### Decision 4 — Scoring: derive a net lean in [-1, +1] from votes (+ panic_score)

Per product, fetch recent CryptoPanic posts for the bare symbol and aggregate a
single **score ∈ [-1, +1]** (negative = bearish) plus an optional worst
`panic_score` for the reason string:

- `pos = Σ post.votes.positive`, `neg = Σ post.votes.negative` over results.
- If `pos + neg == 0` → **no signal → return `None`** (fail-open path; this is
  exactly what sparse meme-coin coverage like SHIB/DOGE will hit — fine).
- `score = (pos - neg) / (pos + neg)`.
- `panic = max(post.panic_score for post in results if present)` (0-100; may be
  absent/None on the free plan — treat missing as unavailable, don't fabricate).

Thresholds (config, see Data changes) applied in `risk.py`, buys only:

- `score <= sentiment_veto_score` (default **-0.6**) → **reject**, reason
  `"rejected: bearish sentiment (score=-0.71, panic=82)"`.
- else `score <= sentiment_dampen_score` (default **-0.3**) → **resize**
  `working_notional *= sentiment_dampen_factor` (default **0.5**), reason
  `"resized: bearish sentiment (score=-0.42) x0.50"` (combine with any prior
  per-position resize reason). If the dampened notional falls below
  `rc.min_order_usd`, still emit a resize down to the dampened value — do **not**
  promote it to a reject (keep sentiment strictly softer than a hard cap; a
  sub-min order simply won't fill, which is acceptable and fail-open in spirit).
- else (neutral/bullish) → proceed unchanged.

Rejected alternative — feeding `panic_score` as the primary signal: it's
nullable on the free plan and less well-defined than the vote tally; use it only
to enrich the reason string. Rejected as the primary axis.

Keep the parsing tolerant: unknown/missing keys default to 0; a post with no
`votes` dict contributes nothing. **The exact CryptoPanic endpoint/plan/response
shape must be confirmed against live docs once a token exists** — so make the
base URL a config value and build parsing against the documented shape below,
covered entirely by mocked-response unit tests. Do not block the build on a live
token.

### Decision 5 — Credential + container wiring mirrors Coinbase/SMTP exactly

New secret function, host path `~/.config/coinbase/grow-my-money.cryptopanic`,
container path `/run/secrets/cryptopanic.token`, env var `CRYPTOPANIC_TOKEN_FILE`
mirroring `COINBASE_KEY_FILE`/`SMTP_CREDS_FILE`. The compose **volume line is
committed commented-out** with an instruction; the env var is committed live
(harmless with no file). See Task 7.

---

## Step-by-step tasks (ordered, each independently verifiable)

### Task 1 — Pin the HTTP client
**File: `requirements.txt`** — add `requests==2.32.3` (currently only a
transitive dep via `coinbase-advanced-py`; pin it explicitly per the brief's
Constraints). Verify: `.venv/bin/python -c "import requests; print(requests.__version__)"`.

### Task 2 — Add the credential getter
**File: `src/secrets.py`** — mirror `coinbase_key_path`:
- Module constants: `_HOST_CRYPTOPANIC = Path.home()/".config"/"coinbase"/"grow-my-money.cryptopanic"`
  and `_CONTAINER_CRYPTOPANIC = Path("/run/secrets/cryptopanic.token")`.
- `def cryptopanic_token(configured: str | None = None) -> str:` → `_resolve` the
  path, `raise SecretError(f"CryptoPanic token file not found at {path}. Sign up
  at cryptopanic.com, generate an API token, and place it there (mode 600).")`
  if absent, `_check_mode_600(path)`, then return the **stripped file contents**
  (the token string), not the path (the token is used as a query param, unlike
  the Coinbase key which is a path handed to the SDK). Never log the token.

### Task 3 — Config fields
**File: `src/config.py`** — add to `Config` (frozen dataclass) with defaults, and
wire each in `load_config` via the existing `_get`/`_get_bool`/`_get_float`/
`_get_int` helpers (follow the `dashboard_*` block pattern):
- `cryptopanic_token_file: str = ""` ← `_get(env, "CRYPTOPANIC_TOKEN_FILE", "")`
- `sentiment_enabled: bool = True` ← `_get_bool(env, "SENTIMENT_ENABLED", True)`
- `sentiment_ttl_sec: int = 900` ← `_get_int(env, "SENTIMENT_TTL_SEC", 900)`
- `sentiment_api_base: str = "https://cryptopanic.com/api/developer/v2/posts/"`
  ← `_get(env, "SENTIMENT_API_BASE", <default>)` (parametrized so the executor
  can correct the exact endpoint after a live token check without a code change)
- `sentiment_dampen_score: float = -0.3` ← `_get_float(...)`
- `sentiment_veto_score: float = -0.6` ← `_get_float(...)`
- `sentiment_dampen_factor: float = 0.5` ← `_get_float(...)`
- `sentiment_timeout_sec: float = 4.0` ← `_get_float(...)`

Do **not** add these to `RiskConfig` (they are not hard safety caps; keep
`RiskConfig.validate` and its four fields untouched). No new validation required,
but if you add any, keep it non-raising for out-of-range sentiment knobs
(fail-open ethos) — e.g. clamp, don't raise.

### Task 4 — New module `src/sentiment.py`
Create `SentimentProvider` modeled on `PriceProvider` (TTL cache, never raises):

```
@dataclass
class SentimentResult:
    product: str
    score: float          # [-1, +1], negative = bearish
    panic: float | None   # worst panic_score seen, or None
    n_posts: int

class SentimentProvider:
    def __init__(self, cfg, token_loader=None, http_get=None):
        # token_loader defaults to (lambda: secrets.cryptopanic_token(cfg.cryptopanic_token_file or None))
        # http_get defaults to requests.get   (both injectable for tests)
        # self._cache: dict[str, tuple[SentimentResult | None, float]]  # (result, monotonic_ts)
    def lean(self, product: str) -> SentimentResult | None:
        # 1) TTL cache check (store None results too, so failures don't hammer the API)
        # 2) resolve token via token_loader(); on SecretError/any Exception -> log.info once, cache None, return None
        # 3) symbol = product.split("-")[0]
        # 4) http_get(cfg.sentiment_api_base, params={"auth_token": token, "currencies": symbol},
        #             timeout=cfg.sentiment_timeout_sec)  ; non-200 -> None
        # 5) parse results -> score per Decision 4; any exception -> log.info, return None
        # cache and return
```

Rules:
- **Never raise** to the caller — wrap the whole body so `lean` returns `None` on
  anything unexpected. Log at `info`/`warning` with the product and a short
  reason (e.g. `"sentiment unavailable for BTC-USD: no token configured,
  proceeding without dampening"`), never the token value.
- Cache `None` as well as real results (keyed by product) with the same TTL, so a
  down API or missing token doesn't fetch every cycle.
- Symbol mapping is `product.split("-")[0]` (BTC-USD → BTC). All 12 current
  products are plain symbols; no collision handling needed.
- `token_loader` and `http_get` are constructor-injected so tests pass fakes (the
  `_FakeClient` pattern from `tests/test_dashboard.py`) with **no network and no
  real token**.

Do **not** import `sentiment` into `src/model.py` (out of scope; keep the model
untouched).

### Task 5 — Wire the sentiment check into `src/risk.py`
- Add optional keyword-only param: `def __init__(self, config, state, portfolio,
  killswitch, price_source, *, sentiment=None):` and store `self.sentiment =
  sentiment`. (Keyword-only via `*` so no positional call site can be affected.)
- In `_check`, **after** the trade-count-cap block and **before** the final
  `RiskDecision` construction (current lines 98-109), insert a new, clearly
  commented step **5) News-sentiment dampener/veto (buys only; FAIL-OPEN)**:
  - Guard: `if is_buy and self.sentiment is not None:` — sells and inert mode
    skip entirely.
  - Wrap the sentiment consultation in its own `try/except Exception` that logs
    and does nothing (proceed) — so it can never reach `check`'s fail-closed
    handler (Key fact #3).
  - `res = self.sentiment.lean(intent.product)`; if `res is None` → proceed
    unchanged.
  - If `res.score <= self.cfg.sentiment_veto_score` → `return RiskDecision(
    "reject", 0.0, f"rejected: bearish sentiment (score={res.score:.2f}"
    + (f", panic={res.panic:.0f}" if res.panic is not None else "") + ")")`.
  - Elif `res.score <= self.cfg.sentiment_dampen_score` →
    `working_notional *= self.cfg.sentiment_dampen_factor`; set `action =
    "resize"`; build `resize_reason` combining any prior per-position resize
    reason with `f"resized: bearish sentiment (score={res.score:.2f}) x{factor:.2f}"`.
  - Log clearly on veto/dampen and on "unavailable ... proceeding without
    dampening" so it's visible in `docker compose logs`.
- Keep the existing four checks and their order **byte-for-byte unchanged**; the
  new block is strictly additional and last. Read `src/risk.py`'s module
  docstring — do not alter its "every one FAILS CLOSED" section; add a short note
  that step 5 is the deliberate fail-**open** exception.

### Task 6 — Inject the provider in `src/bot.py`
- Import: `from .sentiment import SentimentProvider`.
- In `Bot.__init__`, build `self.sentiment = SentimentProvider(config) if
  config.sentiment_enabled else None` and pass it: `self.risk = RiskManager(...,
  price_source, sentiment=self.sentiment)`.
- Nothing else in `bot.py` changes (the buy path already routes through
  `gateway.execute` → `risk.check`).

### Task 7 — Container + docs wiring
- **`docker-compose.yml`**: under `environment:` add
  `CRYPTOPANIC_TOKEN_FILE: /run/secrets/cryptopanic.token` (live, harmless).
  Under `volumes:` add a **commented** line mirroring the existing `:ro` mounts:
  ```
  # Uncomment AFTER creating the CryptoPanic token file (see README), then
  # `docker compose up -d`. Leaving it commented keeps the feature inert.
  # - ${HOME}/.config/coinbase/grow-my-money.cryptopanic:/run/secrets/cryptopanic.token:ro
  ```
  (Committing it uncommented would make Compose auto-create a root-owned
  directory at the missing host path — avoid.)
- **`.env.example`**: add a `# --- News sentiment ---` block documenting
  `SENTIMENT_ENABLED`, `SENTIMENT_TTL_SEC`, `SENTIMENT_API_BASE`,
  `SENTIMENT_DAMPEN_SCORE`, `SENTIMENT_VETO_SCORE`, `SENTIMENT_DAMPEN_FACTOR`,
  `SENTIMENT_TIMEOUT_SEC`, and `CRYPTOPANIC_TOKEN_FILE=` (empty; container
  overrides to `/run/secrets/cryptopanic.token`), matching the existing block
  style. **No secret goes here.**
- **`Dockerfile`**: no change needed (no secret is baked in; the token arrives
  via the runtime mount, exactly like the Coinbase key). `entrypoint.sh`: no
  change (dual-process supervision is unaffected).
- **`README.md`**: add a short "CryptoPanic sentiment (optional)" subsection to
  the go-live/credentials area: how Eric creates the mode-600 token file
  (`umask 077; printf '%s' '<TOKEN>' > ~/.config/coinbase/grow-my-money.cryptopanic`),
  then uncomments the compose volume and `docker compose up -d --build`. State
  clearly the feature is inert until then.

### Task 8 — Tests (new files only; do not modify existing tests)
- **`tests/test_sentiment.py`**:
  - Parsing/lean: fake `http_get` returning a bullish payload (positive votes ≫
    negative) → `score > 0`; a strongly-bearish payload (negative ≫ positive,
    `panic_score` high) → `score <= -0.6`, `panic` populated. Assert the
    votes/panic → score mapping (criterion 1).
  - Fail-open matrix (criterion 2), each returns `None` and does not raise:
    (a) `token_loader` raises `SecretError`; (b) `http_get` raises
    `requests.Timeout`/generic exception; (c) `http_get` returns a non-200
    (fake response object with `.status_code=500`); (d) malformed JSON / missing
    `results`; (e) empty `results` / zero total votes.
  - TTL cache: two `lean` calls within TTL → `http_get` called once (mirror
    `test_price_cache_within_ttl`).
- **`tests/test_risk_sentiment.py`** (criteria 3 & 4). Reuse `conftest`
  fixtures (`config`, `state`, `portfolio`, `killswitch`, `price_source`). Use a
  tiny stub sentiment object with a `lean(product)` method returning a canned
  `SentimentResult`/`None`/raising:
  - Strongly bearish (score -0.8) → `RiskManager(..., sentiment=stub).check(buy)`
    returns `action == "reject"` and `"sentiment" in reason`.
  - Moderately bearish (score -0.4) → `action == "resize"`,
    `adjusted_notional == requested * 0.5`, `"sentiment" in reason`.
  - `lean` returns `None` → buy **approved unchanged** (fail-open).
  - `lean` **raises** → buy **approved unchanged** (proves it does NOT become a
    fail-closed reject — the critical case).
  - **Sell** intent with a bearish stub → unaffected (approve; sentiment skipped).
  - Sanity: `sentiment=None` (inert) → identical to today's behavior.
- **`tests/test_secrets_cryptopanic.py`** (criterion 5): using `tmp_path` +
  `monkeypatch` on the module path constants (or the `configured` arg), assert
  `cryptopanic_token(configured=<file>)` returns the stripped token; missing file
  → `SecretError`; a permissive-mode file → returns token but logs a warning
  (mirror how you'd test `coinbase_key_path`; if no `secrets.py` test exists
  today, this is the first — keep it analogous to `_resolve`'s contract).

---

## Data / model / API changes

- **No DB schema change.** The `intents.reason` column already carries the
  sentiment reason via the existing `update_intent_risk` write path.
- **No dashboard code change.** `build_activity`/`build_payload` already surface
  `intent["reason"]`; a sentiment reject shows as a blocked intent, a dampen as a
  resized intent — confirm by eyeballing `curl 127.0.0.1:8420/api/data` after a
  (mocked or real) bearish cycle.
- **No `src/model.py` change** (out of scope).
- **New config fields** (Task 3) — all optional with safe defaults.
- **New secret** (Task 2) — file-based, mode-600, outside the repo/image.
- **External API**: CryptoPanic. Expected (confirm live): `GET
  {SENTIMENT_API_BASE}?auth_token=<TOKEN>&currencies=<SYMBOL>` → JSON
  `{"results": [{"votes": {"positive": int, "negative": int, "important": int,
  ...}, "panic_score": 0-100|null, "currencies": [{"code": "BTC"}], ...}, ...]}`.
  Parse defensively; treat any deviation as "no signal" → `None`.
- **New env vars**: `CRYPTOPANIC_TOKEN_FILE`, `SENTIMENT_ENABLED`,
  `SENTIMENT_TTL_SEC`, `SENTIMENT_API_BASE`, `SENTIMENT_DAMPEN_SCORE`,
  `SENTIMENT_VETO_SCORE`, `SENTIMENT_DAMPEN_FACTOR`, `SENTIMENT_TIMEOUT_SEC`.

---

## Testing & verification (maps to acceptance criteria)

- **#1 (parse real-shape data → lean)**: `tests/test_sentiment.py` parsing cases.
- **#2 (fail-open on every failure mode)**: `tests/test_sentiment.py` fail-open
  matrix + `tests/test_risk_sentiment.py` `lean` returns None / raises cases.
- **#3 (bearish → resize or reject, reason mentions sentiment)**:
  `tests/test_risk_sentiment.py` reject + resize cases.
- **#4 (safety checks & sells unaffected)**: run the four existing tests
  unmodified:
  `.venv/bin/python -m pytest -q tests/test_killswitch.py
  tests/test_risk_portfolio_halt.py tests/test_risk_per_position_cap.py
  tests/test_risk_daily_trade_cap.py` → all pass; plus the sell case in
  `tests/test_risk_sentiment.py`.
- **#5 (secret via convention)**: `tests/test_secrets_cryptopanic.py`.
- **#6 (full suite)**: `.venv/bin/python -m pytest -q` → expect **61 + new tests
  passing**, no regressions (9 pre-existing sklearn warnings are fine).
- **#7 (container healthy)**: `docker compose up -d --build`, then
  `docker compose logs app --tail=100` (both bot + dashboard start) and
  `curl -sf 127.0.0.1:8420/` (HTTP 200). Do not disrupt the running
  `grow-my-money-app-1` trading state.
- **#8 (inert without a token, live)**: with no token file and the compose mount
  commented out, `docker compose logs app` should show sentiment-unavailable log
  lines (or none if disabled) and trading continuing exactly as before — no
  exceptions, no blocked buys attributable to sentiment.

Suggested final sequence:
```
.venv/bin/python -m pytest -q                       # criterion 6
docker compose up -d --build && sleep 5
docker compose logs app --tail=100                  # criteria 7, 8
curl -sf -o /dev/null -w '%{http_code}\n' 127.0.0.1:8420/
```

---

## Risks & watch-outs

1. **Fail-open vs the fail-closed wrapper (highest risk).** `check()` catches all
   `_check` exceptions and REJECTS. The sentiment sub-step MUST catch its own
   exceptions and proceed; otherwise an API/provider bug silently blocks *all*
   buys — the exact opposite of the requirement. Test the "`lean` raises → buy
   approved" case explicitly.
2. **Ordering.** The sentiment step goes **after** the trade-count cap and
   **before** the final `RiskDecision`. Do not reorder or gate any of the four
   existing checks behind it. It only ever reads `is_buy` and mutates the local
   `working_notional`/`action`/`resize_reason`.
3. **Don't touch `RiskConfig` or its `validate`.** Sentiment knobs live on
   `Config`, not `RiskConfig`; adding them to the frozen `RiskConfig` would ripple
   into the safety-cap validation and the `conftest`/dashboard fixtures.
4. **Keyword-only param.** Use `*, sentiment=None` so no positional call site
   (five existing) is disturbed — criterion 4.
5. **Compose bind mount for a missing file.** Commit it **commented out**. An
   uncommented mount of a non-existent host path makes Compose create a
   root-owned directory under `~/.config/coinbase`, which is ugly and can wedge
   the `:ro` semantics. Env var alone is safe.
6. **Never log or exception-leak the token.** Follow `secrets.py`: messages carry
   the path and reason only. `cryptopanic_token` returns the token *value* (not a
   path) — be careful it never lands in a log line or an f-string that gets
   logged.
7. **Endpoint/plan uncertainty.** No live token exists at build time. Parse
   against the documented shape, keep `SENTIMENT_API_BASE` configurable, and lean
   on mocked tests. Do not hard-block the feature on a live call.
8. **Meme-coin / sparse coverage.** SHIB/DOGE etc. may return zero votes → score
   undefined → `None` → no dampening. That's correct (fail-open), not a bug.
9. **Cache `None` too.** Otherwise a missing token or down API triggers a fetch
   every product every cycle — wasteful and log-spammy.
10. **Don't import `sentiment` into `model.py`.** Keep the model's feature set and
    training path pristine (out of scope, criterion boundary).

---

## Out of scope (do not build)

- Sentiment as a **trained ML feature** in `src/model.py` / outcomes labeling.
- Any **LLM-based** headline scoring or extra LLM calls in the trade loop.
- Sentiment-driven **sells** or forced exits (buys only; sells untouched).
- Any **dashboard UI** redesign or new sentiment cards/charts — only the existing
  `reason` string surfacing.
- **Historical backfill** / sentiment-vs-outcome analysis tooling.
- Any change to the existing **fail-closed** checks (kill switch, portfolio halt,
  per-position cap, trade-count cap) — they stay byte-for-byte as they are.
```
