# grow-my-money — News/Sentiment Signal — Concept Brief

## Problem
The bot (`src/signals.py`, `src/indicators.py`, `src/model.py`) trades purely
on technical price action: EMA/RSI/MACD computed from OHLC candles
(`src/marketdata.py`), plus an ML model (`src/model.py`) trained on those same
technical features and past trade outcomes. It has **zero** awareness of news,
headlines, macro events, exchange listings/delistings, hacks, regulatory
announcements, or social sentiment. This is a real blind spot: a purely
technical signal can happily buy into a crash caused by breaking bad news, or
miss that a rally is unsustainable pump-driven noise.

## Goal
Give the bot a lightweight sentiment awareness that can **dampen or veto**
buy decisions technicals would otherwise approve, without touching the
existing ML model's training pipeline or any safety-critical risk logic.

## In scope (v1)
- **Data source: CryptoPanic API** (free tier), fetched per-product each
  decision cycle. Endpoint: `https://cryptopanic.com/api/API_PLAN/v2/` (or
  the v1 free-plan base the account's token is provisioned for — confirm at
  build time), authenticated via an `auth_token` query param. Filter by
  `currencies=<SYMBOL>` (e.g. `BTC`, `ETH`, `XRP` — strip the `-USD` suffix
  from `cfg.products`) and optionally `filter=bullish|bearish|important` or
  read the `votes`/`panic_score` fields on returned posts to derive a
  bullish/bearish lean per product. (Source: CryptoPanic developer docs,
  https://cryptopanic.com/developers/api/ — auth via `auth_token` query
  param, filters include `bullish`/`bearish`/`hot`/`rising`/`important`, post
  objects carry `votes` and a 0-100 `panic_score`.)
- **New out-of-repo credential**: a CryptoPanic API token, following the
  exact convention in `src/secrets.py` (see `coinbase_key_path` /
  `smtp_credentials`) — a new function e.g. `cryptopanic_token(configured:
  str | None = None) -> str`, host default path
  `~/.config/coinbase/grow-my-money.cryptopanic` (mode 600, warn-not-fail on
  permissive mode, same pattern as existing secrets), container path
  `/run/secrets/cryptopanic.token`, new compose bind mount + env var
  `CRYPTOPANIC_TOKEN_FILE` mirroring `COINBASE_KEY_FILE`/`SMTP_CREDS_FILE`.
  **The user (Eric) needs to sign up for a free CryptoPanic account and
  generate a token before this feature can do anything live** — this is a
  manual, out-of-band step for the user, not something the build agent can
  do. The brief author flags this explicitly so the plan's rollout steps
  include "user must obtain and place a token" as a documented prerequisite,
  analogous to the Coinbase key setup this project already went through.
- **Integration point: dampener/veto, not a new ML feature.** Sentiment must
  NOT be added as a trained-model input in `src/model.py` in v1 (that's
  explicitly deferred — see Out of scope). Instead:
  - A new small module (e.g. `src/sentiment.py`) computes a per-product
    sentiment score/lean each cycle (cached, see below).
  - The score is consulted either in `src/signals.py::evaluate()` (to
    suppress/soften a buy signal before it ever becomes a `TradeIntent`) or
    as an additional check in `src/risk.py::RiskManager._check()` (to
    veto/reject a buy intent that already passed technicals) — **the planner
    decides which integration point is cleaner given the actual code
    structure**, but whichever is chosen:
    - It must be **read-only w.r.t. safety-critical invariants** — it must
      NOT touch the kill switch, the portfolio halt flag, the per-position
      cap, or the trade-count cap. It is a new, independent, additive check.
    - It must **fail open** (a missing token, API error, timeout, or absent
      sentiment data must NOT block a trade) — sentiment is an enhancement
      signal, not a safety mechanism. Contrast this deliberately with
      `src/risk.py`'s existing fail-closed philosophy for safety checks
      (kill switch, halt, caps) — the brief author is making an explicit,
      different choice here because an external, rate-limited, sometimes-down
      news API becoming a hard blocker on all trading would be worse than
      just ignoring sentiment when it's unavailable. Log clearly when this
      happens (e.g. "sentiment unavailable for BTC-USD: <reason>, proceeding
      without dampening") so it's visible in `docker compose logs` and (see
      below) reflected in the dashboard, but never raise into the trade loop.
  - Sell decisions are **not** affected by sentiment in v1 — only buys can be
    dampened/vetoed (mirrors the existing halt/per-position-cap pattern,
    which also only gates buys).
- **Caching**: CryptoPanic's free tier has rate limits (exact quota TBD by
  the planner/executor — check current docs/response headers at build time).
  Reuse the TTL-cache pattern already established in
  `src/dashboard_data.py::PriceProvider` (build a similar cache keyed by
  product, refreshed at most once per some interval — likely once per
  decision cycle is already conservative given `DECISION_INTERVAL_MIN=30`,
  but consider a longer TTL, e.g. 15-30 min, so a manual `cli once` run
  doesn't burn quota, and so the same sentiment fetch can be shared if
  cadence ever tightens).
- **Dashboard surfacing**: since the dashboard (`src/dashboard_data.py`,
  `src/dashboard_web/index.html`, already shipped) exists and shows per-trade
  `reason` strings in the activity feed, any sentiment-driven dampen/veto
  should show up naturally in the existing `reason` field (e.g. "resized:
  bearish sentiment" / "rejected: bearish sentiment (panic_score=82)") with
  **no dashboard code changes required** if the reason string is threaded
  through the same intent/trade `reason` column already displayed — confirm
  this is true when implementing; if the chosen integration point doesn't
  naturally produce a visible `reason`, that's a plan defect to catch, not
  something to skip.
- **Tests**: unit tests for the new sentiment module (mocking the HTTP call,
  no real network/API key needed in CI/test runs — follow the existing
  `tests/conftest.py` fixture style and the fake-client pattern used in
  `tests/test_dashboard.py`'s `_FakeClient`), and tests for the
  veto/dampener's effect on a signal or risk decision, including the
  fail-open path when sentiment is unavailable.

## Out of scope (v1)
- Adding sentiment as a trained ML model feature (`src/model.py`'s feature
  set / `src/outcomes` labeling) — that's a real follow-on but changes the
  model's training/retraining/promotion logic and deserves its own brief.
- Any LLM-based headline scoring — v1 uses CryptoPanic's built-in
  community vote / panic_score fields directly, no additional LLM API calls
  in the trade loop.
- Sentiment-driven **sells** or forced position exits.
- A dashboard UI redesign — only the existing `reason` string surfacing
  matters; no new charts/cards for sentiment history in v1.
- Historical backfill / sentiment-vs-outcome analysis tooling.
- Any change to `src/risk.py`'s existing fail-**closed** checks (kill switch,
  portfolio halt, per-position cap, trade-count cap) — those stay exactly as
  they are; sentiment is a new, separate, fail-**open** check.

## Constraints
- Match existing conventions: type hints, small focused modules, `Config`
  dataclass fields with env wiring via `_get`/`_get_float`/`_get_bool` helpers
  in `src/config.py` (see how `dashboard_*` fields were added), secrets via
  `src/secrets.py`'s established pattern (mode-600 file outside the repo,
  container `:ro` bind mount, warn-don't-fail on permissive mode).
- Must not modify `src/risk.py`'s existing ordered checks or their
  fail-closed semantics for kill switch / halt / per-position cap /
  trade-count cap (see the module's own docstring: "every one FAILS CLOSED").
  The new sentiment check is additive and fail-**open**, and should be
  clearly delineated as a distinct, later/separate step from those four.
- No new external LLM calls in the hot trade-decision path (out of scope,
  see above) — this keeps latency and cost bounded and avoids a second kind
  of external dependency beyond the news API itself.
- Respect the container's existing dual-process/secrets-mount pattern
  (`Dockerfile`, `docker-compose.yml`, `entrypoint.sh` — already updated for
  the dashboard; the new CryptoPanic token mount is additive to the same
  `volumes:`/`environment:` blocks, not a restructuring).
- `requirements.txt` may need an HTTP client — check what's already available
  (`requests` isn't currently pinned; the Coinbase SDK pulls in `requests` as
  a transitive dep per `coinbase-advanced-py`'s own `Requires: backoff,
  cryptography, PyJWT, requests, websockets` — the planner should decide
  whether to depend on the already-present transitive `requests` explicitly
  by pinning it directly in `requirements.txt`, which is the safer/more
  correct choice rather than relying on an undeclared transitive dependency).

## Acceptance criteria
1. A new `src/sentiment.py` (or planner-chosen name) module exists with a
   function/class that, given a product (e.g. `"BTC-USD"`) and a valid
   CryptoPanic token, returns a sentiment lean/score derived from real
   CryptoPanic API data — verifiable via a unit test with a mocked HTTP
   response asserting the parsing logic (votes/panic_score → lean/score).
2. When sentiment is unavailable (no token configured, HTTP error, timeout,
   malformed response), the bot's trade loop proceeds **exactly as it does
   today** — no exception propagates, no trade that would otherwise be
   approved gets blocked solely because sentiment couldn't be fetched. Prove
   this with a unit test simulating each failure mode.
3. When sentiment for a product is strongly bearish (test with a mocked
   strongly-bearish response), a buy that technicals would otherwise approve
   is either **resized down** or **rejected**, with a `reason` string that
   mentions sentiment (e.g. contains "sentiment"), provable via a unit test
   on the chosen integration point (`signals.evaluate` or
   `risk.RiskManager.check`).
4. Sells, the kill switch, the portfolio halt, the per-position cap, and the
   trade-count cap are all **unaffected** by this change — the existing
   relevant tests (`tests/test_killswitch.py`,
   `tests/test_risk_portfolio_halt.py`, `tests/test_risk_per_position_cap.py`,
   `tests/test_risk_daily_trade_cap.py`) still pass unmodified, proving no
   regression to those safety-critical paths.
5. The credential is read via the `src/secrets.py` convention (new function,
   mode-600 warning, host/container path resolution mirroring
   `coinbase_key_path`), with a unit test analogous to any existing
   `secrets.py` tests (check if any exist; if not, at minimum verify the
   resolve-path logic behaves like the existing functions').
6. Full existing test suite still passes:
   `.venv/bin/python -m pytest -q` (root:
   `/home/eric/projects/grow-my-money`) — currently 61 passed.
7. `docker compose up -d --build` still brings the container up healthy with
   both the bot loop and dashboard running (no regression to the
   dual-process entrypoint) — verify via `docker compose logs app` and
   `curl 127.0.0.1:8420/`.
8. **Without a real CryptoPanic token configured** (i.e. as the feature ships
   today, before Eric signs up), the running bot continues trading exactly
   as before — this is the fail-open path (#2) exercised for real, not just
   in a unit test. After deploy, confirm via `docker compose logs app` that
   sentiment fetch failures are logged clearly but do not disrupt trading.

## Open questions & decisions made
- **Exact integration point (signals.py vs risk.py)**: left to the Opus
  planner to decide after reading both modules — brief author's lean is
  towards `risk.py` (it's already the single chokepoint every order passes
  through, per its own docstring, and adding an additional *fail-open*,
  clearly-separate check there keeps all "does this order proceed"
  logic in one place) but `signals.py` is also plausible if it's structurally
  cleaner. Justify whichever is chosen.
- **CryptoPanic API version/plan** (v1 vs v2, exact free-tier endpoint and
  rate limit): the brief author found `https://cryptopanic.com/developers/api/`
  returns 403 to a generic fetch (likely bot-blocking, not indicative of the
  real API's availability) and confirmed via search that authenticated
  requests go to `https://cryptopanic.com/api/API_PLAN/v2/...` with an
  `auth_token` query param, filterable by `currencies=` and `filter=`, with
  `votes`/`panic_score` fields on results. **The executor should verify
  current exact endpoint/response shape against CryptoPanic's live docs or a
  test call once a real token exists** — since no token exists yet (see
  next point), build the parsing logic against the documented/expected shape
  and cover it with mocked-response unit tests; do not block the whole
  feature on having a live token during build.
- **No CryptoPanic token exists yet.** Eric has not signed up. The plan
  should NOT assume a working live integration at deploy time — it should
  ship in the fail-open "no token configured" state, fully inert but ready,
  until Eric provides a token later (a five-minute follow-up, not part of
  this build). Flag this clearly to Eric in the final report.
- **Symbol mapping**: `cfg.products` are Coinbase product IDs like
  `"BTC-USD"`; CryptoPanic's `currencies=` filter wants bare symbols like
  `"BTC"`. The planner should specify the straightforward `.split("-")[0]`
  mapping (all current products — BTC, ETH, SOL, XRP, DOGE, SHIB, ALGO, ADA,
  DOT, LINK, AVAX, LTC — are standard symbols with no expected collision
  issues, but note SHIB/DOGE are meme coins that may have sparse/noisy
  CryptoPanic coverage — that's fine, just means their sentiment score may
  often be neutral/absent, which is exactly the fail-open path).
- Plan approval gate: **skipped** at the user's request (matches their
  earlier choice for the dashboard feature) — plan will be sanity-checked by
  this session, not shown to the user before build starts.

## Relevant files/areas
- `src/signals.py` — current buy/sell signal evaluation (`evaluate()`).
- `src/risk.py` — the order chokepoint (`RiskManager.check`/`_check`),
  existing ordered fail-closed checks; docstring explicitly documents the
  fixed order — read carefully before deciding where a new fail-open check
  can be added without disturbing that order's semantics or tests.
- `src/model.py` — technical-feature ML model; explicitly NOT to be touched
  for a new feature input in v1 (out of scope), but worth a skim to confirm
  the sentiment module doesn't accidentally get imported into it.
- `src/config.py` — `Config`/`RiskConfig` dataclasses and `load_config`;
  follow the pattern used for `dashboard_*` fields to add new
  `sentiment_*`/`cryptopanic_*` config (e.g. a TTL, an enable/disable flag,
  a dampen-vs-veto threshold — planner's call on exact knobs).
- `src/secrets.py` — credential file convention to extend
  (`coinbase_key_path`, `smtp_credentials` as the two existing examples).
- `src/dashboard_data.py` — `PriceProvider`'s TTL-cache pattern is a good
  template for the new sentiment cache; also confirm sentiment-driven
  `reason` strings flow through `build_activity`/`build_payload` without
  changes needed.
- `Dockerfile`, `docker-compose.yml`, `entrypoint.sh` — additive credential
  mount, matching the existing Coinbase/SMTP secret mounts.
- `.env.example` — document new env vars (mirroring the `--- Dashboard ---`
  block style already there).
- `requirements.txt` — add an HTTP client dependency if needed (see
  Constraints).
- `tests/test_risk_daily_trade_cap.py`, `tests/test_risk_per_position_cap.py`,
  `tests/test_risk_portfolio_halt.py`, `tests/test_killswitch.py` — must
  keep passing unmodified (acceptance criterion 4).
- `tests/conftest.py`, `tests/test_dashboard.py` — fixture/fake-client style
  to follow for new tests.

## Repo commands & tree state
- **Test**: `.venv/bin/python -m pytest -q` (run from
  `/home/eric/projects/grow-my-money`; do not assume `pytest`/`python` are on
  PATH — use the venv explicitly, matching the `Makefile`'s `test:` target).
  Currently: 61 passed, 9 warnings (pre-existing sklearn warnings, harmless).
- **Build/deploy container**: `docker compose up -d --build` (same
  directory).
- **Logs**: `docker compose logs -f --tail=200`.
- **Host CLI**: `.venv/bin/python -m src.cli status` / `once` /
  `report-now --dry-run` / `stop` / `resume`. Note: the **host** venv's
  Coinbase key cannot currently authenticate (host `cryptography`/SDK
  mismatch noted in a prior session) — `cli once` on the host will fail to
  fetch live prices; this is pre-existing and unrelated to this feature. The
  **container** has a working key. Prefer verifying against the running
  container (`docker compose logs`, `curl 127.0.0.1:8420/api/data`) over the
  host CLI for anything needing live Coinbase prices.
- **Git tree state**: repo has never had its initial commit — `git status
  --short` shows the whole tree as staged (`A`), plus `.env.example` is
  `AM` (staged + modified, from the dashboard feature's env-var
  documentation additions — pre-existing, not part of this task). This
  build will add further files/edits on top of that same uncommitted tree.
  Do not assume a clean tree; do not attribute existing staged/modified
  files to this task's diff when reporting back.
- **Running container**: `grow-my-money-app-1` (compose project
  `grow-my-money`) is currently up, running both the trading bot loop and
  the dashboard (`127.0.0.1:8420`), actively paper-trading against live
  Coinbase market data with a real-account-mirrored starting state. Do not
  disrupt its trading logic or state; expect live data in `state/grow.db`
  while building/testing.
