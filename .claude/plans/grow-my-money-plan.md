# Implementation Plan: grow-my-money — autonomous Coinbase crypto trading bot

> Executor note: You have only this file and an empty git repo at
> `~/projects/grow-my-money`. Build exactly what is described here. This bot
> autonomously trades **real money** on a real Coinbase account with **no
> per-trade human approval**. The safety caps, paper/live separation, and kill
> switch are load-bearing correctness requirements, not features — a bug in any
> of them can lose real funds. When in doubt, **fail closed** (reject the order,
> stay in paper). Do not "improve" the strategy at the expense of the caps.

---

## 1. Summary

Build a standalone Python bot at `~/projects/grow-my-money` that continuously
watches BTC-USD, ETH-USD, and SOL-USD prices via the **Coinbase Advanced Trade
API**, computes technical indicators (EMA crossover, RSI, MACD), feeds those
features into a small, explainable adaptive model (scikit-learn logistic
regression that retrains as trade outcomes accumulate) to decide buy/sell/hold
and position size, and executes trades **only through a single risk chokepoint**
that enforces hard safety caps (30% portfolio-drawdown halt, 25%-of-bankroll
per-position cap, 5-trades-per-rolling-24h cap) and honors a kill switch. It runs
in **paper mode by default** (real live prices, simulated fills) and only touches
real funds after the operator makes an explicit, documented switch to live mode.
It measures its own performance against a **buy-and-hold benchmark** (equal-parts
BTC/ETH/SOL bought at the start of each trading epoch and never traded) so
success means *beating the market*, not merely being positive. It emails a daily
summary to ericfaris@gmail.com and persists all state (trade history, mode,
model, cap-trip flags, benchmark anchor) to a bind-mounted SQLite database so an
unattended container restart never resets the bot into an optimistic state. It
deploys via the sibling-project Docker lab pattern, with all secrets read at
runtime from mode-600 files outside the repo and outside any image layer.

---

## 2. Approach & key decisions

### 2.1 Language, project layout
Python 3.12 in a venv (`.venv`), per the brief's explicit deviation from the
Node house style (ML tooling). Package layout:

```
grow-my-money/
  src/
    __init__.py
    config.py          # loads + validates all config; defines Config dataclass; MODE default=paper
    secrets.py         # reads out-of-repo credential files (mode-600), never logs values
    coinbase_client.py # thin wrapper over coinbase-advanced-py RESTClient
    marketdata.py      # candle fetch + caching; builds pandas DataFrames per product
    indicators.py      # EMA/RSI/MACD feature computation (pure functions on DataFrame)
    signals.py         # rule-based signal layer -> TradeIntent proposals
    model.py           # adaptive logistic-regression layer: featurize, predict, retrain, persist
    risk.py            # RiskManager — THE single chokepoint all orders pass through
    execution.py       # OrderGateway: paper simulator vs live Coinbase order; both call risk first
    portfolio.py       # cash + positions, mark-to-market valuation, bankroll tracking
    benchmark.py       # buy-and-hold baseline: record anchor once/epoch, mark-to-market vs actual
    killswitch.py      # sentinel-file kill switch (set/clear/is_engaged)
    state.py           # SQLite persistence layer (trades, positions, runtime kv, model meta, benchmark)
    email_report.py    # Gmail SMTP daily summary (smtplib), mirrors bookhunt/src/smtp.js shape
    scheduler.py       # decision loop cadence + daily-report timer
    bot.py             # main run loop wiring everything together (entry point for the daemon)
    cli.py             # subcommands: run, stop, resume, status, set-mode, report-now, backtest
    logging_setup.py   # structured logging config; redaction filter for secrets
  tests/
    conftest.py
    test_risk_per_position_cap.py
    test_risk_daily_trade_cap.py
    test_risk_portfolio_halt.py
    test_killswitch.py
    test_execution_paper.py
    test_mode_default_paper.py
    test_indicators.py
    test_model.py
    test_state_persistence.py
    test_email_report.py
    test_benchmark.py
  scripts/
    backtest.py        # scriptable CLI backtest over historical candles (encouraged, not polished)
  state/               # bind-mounted, gitignored — SQLite db, model pickle, kill sentinel live here
    .gitkeep
  requirements.txt
  pyproject.toml       # optional; requirements.txt is the source of truth for deps
  Dockerfile
  docker-compose.yml
  .env.example         # NON-secret tunables only (caps, interval, mode); real .env gitignored
  .dockerignore
  .gitignore
  Makefile             # up/down/logs/test/venv targets (Python analogue of bookhunt's npm scripts)
  README.md            # operator runbook: setup, go-live checklist, kill switch, cap config
```

**Rationale for a `src/` package with tight module boundaries:** the acceptance
criteria demand that each safety cap be independently testable. Isolating the
`RiskManager` as the sole order chokepoint makes caps provable in unit tests with
mocked state, without touching Coinbase.

### 2.2 Coinbase API client approach — **use the official SDK**
Use the **official `coinbase-advanced-py`** package
(`pip install coinbase-advanced-py`, `from coinbase.rest import RESTClient`).
Rationale:
- It is Coinbase's own, actively maintained SDK for the current Advanced Trade
  API (not the deprecated Coinbase Pro / `coinbasepro` libraries).
- It handles **CDP API-key JWT authentication automatically** (Ed25519/ECDSA key
  auto-detection, correct JWT signing per request) — reimplementing JWT signing
  by hand is exactly the kind of security-sensitive code we should not write from
  scratch for a real-money bot.
- It exposes everything needed: `get_accounts()`, `get_product(product_id)` /
  `get_best_bid_ask(product_ids=[...])`, `get_candles(product_id, start, end,
  granularity)`, `market_order_buy(client_order_id, product_id, quote_size=...)`,
  `market_order_sell(client_order_id, product_id, base_size=...)`.

Rejected alternative: hand-rolled REST + JWT. More code, more risk, no upside at
hobby scale. Rejected alternative: `coinbase-advancedtrade-python` (community) —
less authoritative than Coinbase's own SDK for a money-handling path.

**Client construction:** `RESTClient(key_file=<path to CDP key JSON>)` where the
path comes from `secrets.py` reading `~/.config/coinbase/grow-my-money.key`
(mode 600). Do **not** use env-var construction (`COINBASE_API_KEY` /
`COINBASE_API_SECRET`) — that would push the secret into the container
`environment:` block, which the brief forbids for these keys.

**`coinbase_client.py` responsibilities:** wrap the SDK behind a small interface
(`get_balances()`, `get_candles(product_id, granularity, limit)`,
`get_spot_price(product_id)`, `place_market_buy(...)`, `place_market_sell(...)`)
so tests can inject a fake, and so the live order calls exist in exactly one
place. Include simple retry/backoff on transient errors and a request-rate guard
(the bot makes only a handful of calls per cycle every 15–60 min, far under the
30 req/s private-endpoint limit, but treat any HTTP 429 as fail-closed for that
cycle — skip trading, do not spin).

**Order idempotency:** every live order gets a deterministic `client_order_id`
(UUID persisted with the trade-intent row *before* the call) so a crash between
"send" and "record fill" cannot double-submit on restart (see §6 crash safety).

### 2.3 Technical-indicator layer (`indicators.py`, `signals.py`)
Compute from **HOUR_1 candles** (config `CANDLE_GRANULARITY`, default `HOUR_1`)
per product:
- **EMA crossover:** EMA(12) vs EMA(26) — fast-over-slow = bullish, cross-down =
  bearish. (Chosen over SMA for responsiveness on hourly bars.)
- **RSI(14):** overbought (>70) / oversold (<30) filter.
- **MACD (12/26/9):** MACD line vs signal line + histogram sign, confirms momentum.

These three are standard, explainable, and cheap. `indicators.py` holds pure
functions taking a pandas DataFrame of candles and returning the latest feature
values plus a small feature vector; `signals.py` turns them into a rule-based
`TradeIntent` proposal (direction + a base confidence 0–1) that the model layer
then gates/scales. Rationale for keeping a rule layer *and* a model layer: the
rule layer gives a sane cold-start (model has no data on day one) and remains a
readable sanity check on what the model does.

### 2.4 Adaptive/ML layer (`model.py`) — logistic regression, retrain on outcomes
- **Model:** scikit-learn `LogisticRegression` (explainable, inspectable
  coefficients, fast). Rejected: gradient boosting (harder to introspect at this
  scale) and any deep learning (overkill, brief explicitly discourages).
- **What it predicts:** given the current feature vector (EMA gap %, RSI, MACD
  histogram, recent return, volatility, per-product one-hot), the probability
  that a **buy now would be profitable over the next `MODEL_HORIZON_HOURS`**
  (default 6h) net of an assumed round-trip fee/slippage. Output `p_win` in [0,1].
- **How it's used:** final decision = rule-layer direction, but a **buy fires
  only if `p_win >= BUY_PROBABILITY_THRESHOLD`** (config, default 0.55) and
  **position size scales with `p_win`** (larger conviction → larger fraction of
  the per-trade budget), always then clamped by the RiskManager. Sells are driven
  by rule layer (cross-down / RSI exit / stop) — we do not gate exits on the
  model, so the bot can always de-risk.
- **Cold start:** until at least `MODEL_MIN_TRAIN_SAMPLES` (default 40) closed
  trades exist, `model.py` returns a **rule-derived pseudo-probability** and logs
  `model=coldstart`. No live trade is ever blocked *from de-risking* by cold
  start; only new buys use the threshold.
- **Training data:** each closed position produces one labeled row (features at
  entry → label = did it clear the fee-adjusted profit bar within the horizon).
  Both paper and live outcomes feed training (paper generates data fast during the
  proving period — this is the whole point of paper mode).
- **Retrain trigger/cadence (documented, config-driven):** retrain when **either**
  (a) `RETRAIN_EVERY_N_CLOSED_TRADES` (default 10) new closed trades have
  accumulated since the last fit, **or** (b) the **nightly** maintenance tick runs
  and there is at least one new sample — whichever comes first. Retraining refits
  on the full labeled history, evaluates on a time-ordered holdout, and **only
  promotes the new model if holdout log-loss does not regress beyond a tolerance**
  (`MODEL_PROMOTE_MAX_REGRESSION`, default keep-if-not-worse-by-0.02); otherwise
  it keeps the previous model and logs the rejection. The active model is
  persisted to `state/model.pkl` with metadata (trained_at, n_samples, holdout
  score) mirrored into SQLite. Rationale: bounded, explainable, prevents a bad
  fit on a noisy week from degrading behavior.

### 2.5 Decision-loop cadence (`scheduler.py`)
Default **every 30 minutes** (`DECISION_INTERVAL_MIN`, config, tunable). Rationale:
strategy runs on HOUR_1 candles, so re-deciding twice per candle catches
crossovers promptly without overtrading; combined with the 5-trades/24h cap it is
impossible to churn. The loop is a simple sleep-based scheduler (no external cron)
so the kill switch is honored within one cycle. A separate daily timer fires the
email report at `DAILY_REPORT_HOUR` (config, local tz, default 08:00) and the
nightly model-maintenance tick.

### 2.6 Paper vs live mode — implementation & switching
- Single config value **`MODE`** with domain `{paper, live}`. **`config.py`
  defaults `MODE=paper` and coerces any missing/invalid value to `paper`**
  (fail-closed). The *persisted* mode lives in SQLite `runtime` kv and is the
  source of truth across restarts; env/`.env` can set it but the persisted value,
  once set by an explicit `set-mode` action, wins — so a restart never silently
  reverts to whatever a stale env said. (Precisely: on startup, if persisted mode
  exists, use it; else seed from config default `paper`.)
- Switching to live is an **explicit, deliberate CLI action**:
  `.venv/bin/python -m src.cli set-mode live` which (a) requires an interactive
  typed confirmation phrase, (b) verifies the Coinbase key file is present and the
  account balance is readable, (c) records `mode=live` + `go_live_bankroll` +
  timestamp in SQLite **and records the live benchmark anchor** (see §2.13), and
  (d) logs loudly. There is **no code path that flips to live automatically.**
  Going back: `set-mode paper` is always allowed without confirmation (de-risking
  is easy, arming is hard).
- **Where the split lives:** `execution.OrderGateway.execute(intent)` first calls
  `RiskManager.check(intent)` (same for both modes), then branches: `paper` →
  `PaperFillSimulator` (fills at current best bid/ask ± `SLIPPAGE_BPS`, records a
  simulated trade), `live` → `coinbase_client.place_market_*`. **The live branch
  is the only place in the codebase that calls a real order endpoint**, guarded by
  an assertion `mode == live`.

### 2.7 Safety caps — single chokepoint, fail-closed (`risk.py`)
All order intents — paper and live — pass through `RiskManager.check(intent) ->
RiskDecision(action=approve|resize|reject, adjusted_size, reason)`. Nothing in
`execution.py` may place a fill without an `approve`/`resize` decision; enforce
this with an assertion inside the gateway. Caps are read from config (no magic
numbers) and defined in `config.py`:

- **Kill switch (checked first):** if `killswitch.is_engaged()` → reject.
- **Portfolio-value halt:** compute mark-to-market portfolio value
  (`portfolio.total_value()`); if `< PORTFOLIO_HALT_FRACTION (0.70) *
  go_live_bankroll` → set a **persistent trip flag** in SQLite and reject **all
  buys** (see §2.8 for the sell question). Once tripped, it stays tripped until a
  human clears it via `cli resume` (fail-closed: a transient price dip that
  recovers still requires human acknowledgement before re-arming).
- **Per-position cap:** for a buy, if `position_value(product) + intended_notional
  > PER_POSITION_FRACTION (0.25) * bankroll` → **resize** the order down to the
  remaining headroom; if headroom `<= MIN_ORDER_USD` → reject. Never silently
  place the oversized order.
- **Daily trade-count cap:** count executed trades in the **rolling trailing 24h**
  (`now - 24h`, computed from stored UTC fill timestamps, not calendar-day
  buckets) — if `>= MAX_TRADES_PER_24H (5)` → reject (do **not** queue for
  replay). Both buys and sells count as trades.
- **Fail-closed rule:** if *any* input needed for a check cannot be evaluated
  (can't read balance, can't mark-to-market, DB error) → reject and log loudly.
  Every rejection/resize logs at WARNING with the cap name and the numbers.

Caps live in a `RiskConfig` dataclass; `RiskManager` takes it + a `state` handle.
The order of checks is fixed and unit-tested.

### 2.8 Portfolio-halt auto-flatten — **DECISION POINT (needs human sign-off)**
The brief leaves open whether the -30% halt should also **sell existing
positions** or only **stop new orders**.

**Recommendation: halt new buys immediately AND auto-flatten to cash on the
halt trip — but ship it behind a config flag `HALT_AUTO_FLATTEN` that DEFAULTS
TO `false` (halt-only) for v1, and require the operator to explicitly opt in.**

Rationale for recommending flatten-capable-but-off-by-default:
- *For flattening:* a 30% drawdown on a trend-following crypto strategy often
  signals a regime the bot is not handling; converting to cash caps further loss
  and is the conservative capital-preservation move for real money. Selling is a
  de-risking action, which the whole design treats as "always allowed."
- *Against flattening as the silent default:* auto-selling into a sharp dip can
  crystallize a loss right before a bounce, and it is an irreversible real-money
  action taken with no human in the loop. Getting it wrong is costly, so it must
  be a deliberate opt-in, not a surprise.
- Halt-only (default) already satisfies the brief's hard requirement ("stop
  placing new orders"); flatten is strictly additive and reversible-in-config.

**Flag this to the human before go-live: decide `HALT_AUTO_FLATTEN` true/false.**
Document both behaviors in the README go-live checklist. Do not silently pick
flattening. If enabled, the flatten sells still route through the gateway/logging
(they bypass the *buy* halt but are subject to the kill switch and are recorded as
trades — note they may exceed the 5/24h cap deliberately; make that explicit:
flatten is an emergency de-risk and is **exempt from the trade-count cap** but
**not** from the kill switch, and this exemption is documented and tested).

### 2.9 Kill switch (`killswitch.py`)
Mechanism: a **sentinel file** `state/KILL`. `is_engaged()` returns
`os.path.exists(KILL_PATH)`. It is checked (a) at the top of every decision cycle
and (b) again inside `RiskManager.check()` immediately before any order — so even
a decision that began before the kill is set cannot place a *new* order. It does
**not** interrupt an in-flight Coinbase HTTP call (per brief). Operator invokes:
- `.venv/bin/python -m src.cli stop` (creates the sentinel; works from host or
  `docker compose exec`), or `touch state/KILL` directly (documented fallback —
  the bind-mounted `state/` dir makes this reachable from the host).
- `.venv/bin/python -m src.cli resume` removes the sentinel **and** clears the
  portfolio-halt trip flag (a single "I've looked, re-arm" action). Because
  `state/` is bind-mounted, the sentinel survives container restarts — a killed
  bot stays killed across a crash/restart until a human resumes. This is
  intentional fail-closed behavior.

### 2.10 Credentials (`secrets.py`)
Out-of-repo, mode-600, read at runtime, never logged — mirroring the Cloudflare
token convention:
- **Coinbase CDP key:** `~/.config/coinbase/grow-my-money.key` — the JSON key file
  Coinbase's UI generates (contains `name`/`privateKey`). Created by the operator
  with **trade permission only, no withdraw/transfer** (manual prerequisite;
  Claude never handles the value). `secrets.py` reads the path, checks the file
  exists and is mode 600 (warn if looser), and passes the path to
  `RESTClient(key_file=...)`. In the container it is **bind-mounted read-only** at
  a fixed path (see §2.11); it is never `COPY`ed into the image.
- **SMTP App Password:** `~/.config/coinbase/grow-my-money.smtp` (mode 600), a tiny
  file with `SMTP_USER=...` / `SMTP_PASS=...` (Gmail App Password), read at
  runtime. Non-secret SMTP settings (`SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
  `SMTP_FROM`, recipient) may live in `.env`/config; the **password never does**.
- `logging_setup.py` installs a redaction filter that scrubs any value that looks
  like the key/secret/password from log records as defense-in-depth.

### 2.11 Docker / deploy (adapted from bookhunt, minus browser stack)
- **Dockerfile:** `FROM python:3.12-slim`, `WORKDIR /app`, non-root uid 1000,
  `pip install --no-cache-dir -r requirements.txt`, `COPY src/ ./src/` (+
  `scripts/`), `ENTRYPOINT ["python","-m","src.bot"]`. **No** Xvfb/Chromium/noVNC.
  No secret files copied in. Stamp `GIT_SHA`/`BUILD_TIME` build args like bookhunt.
- **docker-compose.yml:** `name: grow-my-money`, single `app` service,
  `image: ericfaris/grow-my-money:latest`, `restart: unless-stopped`,
  `user: "1000:1000"`, `security_opt: [no-new-privileges:true]`. **No `ports:`**
  (nothing to serve; no Cloudflare Tunnel — out of scope). Volumes:
  - `./state:/app/state` (SQLite db, model pickle, KILL sentinel — read/write)
  - `~/.config/coinbase/grow-my-money.key:/run/secrets/coinbase.key:ro`
  - `~/.config/coinbase/grow-my-money.smtp:/run/secrets/smtp.env:ro`
  `environment:` block carries only **non-secret** tunables (MODE default paper,
  caps, interval, SMTP host/port/from, report hour) via `.env` passthrough — the
  brief's warning that Coinbase keys and the SMTP password must NOT ride the
  env-passthrough path is honored: those two come only from the `:ro` mounts.
- **Makefile** targets (Python analogue of bookhunt's `npm run docker:up`):
  `make venv`, `make test`, `make up` (exports `GIT_SHA`/`BUILD_TIME` then
  `docker compose up -d --build`), `make down`, `make logs`, `make stop`
  (kill switch), `make status`.

### 2.12 Persistent state (`state.py`) — SQLite
Single bind-mounted SQLite db `state/grow.db` (WAL mode) for atomicity across
crashes. Tables in §5. Model weights persist as `state/model.pkl` (+ metadata row
in db). The KILL sentinel is a file in the same bind-mounted dir. **On startup the
bot reads persisted mode, bankroll, open positions, cap-trip flags, kill state,
and the benchmark anchor from SQLite/sentinel — never assumes a clean slate.**
Rationale for SQLite over JSON: concurrent-safe single-writer, transactional
trade+position updates, and easy rolling-24h queries.

### 2.13 Benchmark hold-baseline (`benchmark.py`) — measure skill, not luck
**Why:** reporting P&L vs. bankroll alone conflates "the bot made good decisions"
with "the market went up." The success criterion going forward is that the bot's
return should **consistently beat a do-nothing buy-and-hold baseline** over time,
not merely be positive. So the bot maintains a hypothetical portfolio that bought
equal parts BTC/ETH/SOL at the start of the trading epoch and never traded, marks
it to market exactly like the real portfolio, and reports both side by side.

**Module choice:** a **dedicated `benchmark.py` module** (not folded into
`portfolio.py`). Rationale: `portfolio.py` tracks *real* mutable positions from
actual fills; the benchmark is a *fixed, never-mutated* hypothetical anchored once
per epoch. Keeping them separate avoids any risk of trade bookkeeping accidentally
touching the baseline, and makes `test_benchmark.py` able to assert immutability
in isolation. `benchmark.py` reuses `portfolio.py`'s mark-to-market price source
(same `get_spot_price` path) so both numbers are marked identically.

**Epoch model — one anchor per mode, recorded once, never recomputed:**
- The baseline is keyed by **epoch = the active mode** (`paper` or `live`). Each
  epoch gets **exactly one** anchor row, written the first time that epoch begins
  trading and **never overwritten**:
  - **paper-start:** the first decision cycle in paper mode with no `paper` anchor
    yet records it, using the paper starting bankroll (`PAPER_START_BANKROLL`
    config, the simulated cash the paper run begins with) as the baseline capital
    and the current BTC/ETH/SOL spot prices.
  - **go-live:** `cli set-mode live` records the `live` anchor at the same moment
    it records `go_live_bankroll` + `go_live_ts`, using `go_live_bankroll` as the
    baseline capital and the go-live spot prices. This is deliberately a *distinct*
    epoch from paper — going live starts a fresh, honest live-vs-market comparison
    against real money, and does not inherit the paper anchor.
- **Anchor contents** (per epoch): anchor timestamp (UTC), `start_bankroll`, the
  three recorded prices, and the three **unit counts** = `(start_bankroll / 3) /
  price_product` for each of BTC/ETH/SOL. Units are computed once at anchor time
  and stored; they are the invariant the baseline is made of.
- **Persistence & restart safety:** the anchor lives in the SQLite `benchmark`
  table (§5) in the same bind-mounted db as everything else. On startup the bot
  loads the current epoch's anchor if present; it is **only ever created via an
  INSERT-if-absent** — there is no code path that updates or recomputes an existing
  anchor from a later start point. A restart, a container recreate, a price move,
  or a nightly retrain must never re-anchor. (`test_benchmark.py` proves this.)

**Marking to market:** `benchmark.value(prices)` = `btc_units*price_btc +
eth_units*price_eth + sol_units*price_sol` using the same current spot prices used
for `portfolio.total_value()`. `benchmark.return_pct()` =
`(value - start_bankroll) / start_bankroll`. The comparison the reports show:
- **actual:** `portfolio.total_value()` and its return vs. the epoch's baseline
  capital (paper start bankroll or `go_live_bankroll`),
- **baseline:** `benchmark.value()` and `benchmark.return_pct()`,
- **delta:** `actual_return_pct - baseline_return_pct` (positive = beating the
  hold baseline; this is the number that decides success).

If the current epoch's anchor is missing or any spot price can't be read, the
reports show the benchmark as `n/a` rather than a wrong number (fail-closed on
display — never fabricate a baseline). This never blocks trading; the benchmark is
report-only and touches no order path or cap.

---

## 3. Step-by-step tasks (ordered, independently verifiable)

Each step should end green before the next. Use exact commands; nothing is on
`PATH` — always `.venv/bin/...`.

1. **Repo scaffolding & gitignore.** Create the tree in §2.1 (empty modules with
   docstrings), `state/.gitkeep`. Write `.gitignore` (`.venv/`, `state/*` except
   `.gitkeep`, `__pycache__/`, `.env`, `*.pkl`, `state/grow.db*`, `state/KILL`)
   and `.dockerignore` (`.venv`, `state`, `.env`, `.git`, `tests`, `__pycache__`,
   `*.pkl`). **Verify:** `git status` shows no secret/state files would be tracked.

2. **Dependencies & venv.** Write `requirements.txt`:
   `coinbase-advanced-py`, `pandas`, `numpy`, `scikit-learn`, `joblib`,
   `python-dotenv`, `pytest`, `freezegun` (for time-window tests). Then:
   `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
   **Verify:** `.venv/bin/python -c "import coinbase.rest, sklearn, pandas"`
   exits 0.

3. **`config.py` + `.env.example`.** `Config`/`RiskConfig` dataclasses loaded from
   env (via python-dotenv) with **paper-defaulting** and validation (invalid MODE
   → paper; caps must be sane fractions). Include `PAPER_START_BANKROLL` (baseline
   capital for the paper epoch). `.env.example` lists only non-secret tunables with
   comments. **Verify:** unit test that missing/garbage MODE yields `paper`.

4. **`secrets.py`.** Functions `coinbase_key_path()`, `smtp_credentials()` reading
   the mode-600 out-of-repo files (paths configurable, defaulting to
   `~/.config/coinbase/...` on host and `/run/secrets/...` in container —
   detect/accept both via config). Never return values in exceptions/logs.
   **Verify:** unit test with a temp file (mode check, missing-file raises clear
   error).

5. **`state.py`.** SQLite schema (§5), WAL, migration/`init_db()`, and typed
   accessors: `record_intent`, `record_fill`, `open_positions`,
   `trades_in_last_24h(now)`, `get_runtime`/`set_runtime`, `set_cap_trip`,
   `model_meta`, and the benchmark accessors `get_benchmark_anchor(epoch)` /
   `create_benchmark_anchor_if_absent(epoch, ...)` (INSERT-if-absent only, never
   UPDATE). **Verify:** `test_state_persistence.py` — write trades, reopen db,
   assert survival; assert rolling-24h query uses trailing window; assert a second
   `create_benchmark_anchor_if_absent` for the same epoch is a no-op.

6. **`killswitch.py`.** `engage()`, `clear()`, `is_engaged()` over `state/KILL`.
   **Verify:** `test_killswitch.py`.

7. **`coinbase_client.py`.** Wrapper over `RESTClient(key_file=...)` with the
   interface in §2.2, retry/backoff, 429-fail-closed. A fake/mocked client for
   tests (no network). **Verify:** unit test with a stub RESTClient that
   `get_balances`/`get_candles` map correctly; assert live order methods call
   `market_order_buy/sell` with a `client_order_id`.

8. **`marketdata.py` + `indicators.py`.** Candle fetch → pandas DataFrame; EMA(12/
   26), RSI(14), MACD(12/26/9), returns/volatility feature extraction. **Verify:**
   `test_indicators.py` against a hand-checked fixture series (known RSI/EMA
   values).

9. **`portfolio.py`.** Cash + positions, `total_value()` mark-to-market from
   current prices, `position_value(product)`, `bankroll`/`go_live_bankroll`.
   **Verify:** unit test valuation math with mocked prices.

10. **`benchmark.py` — buy-and-hold baseline.** Implement per §2.13: anchor
    creation (paper-start on first paper cycle; live at `set-mode live`), stored
    once per epoch via the INSERT-if-absent state accessor and never recomputed;
    `value(prices)`, `return_pct()`, and a `compare(portfolio, prices)` helper that
    returns actual value/return, baseline value/return, and delta (or `n/a` when
    the anchor or a price is missing). Reuses `portfolio.py`'s spot-price source so
    both sides mark identically. **Verify (acceptance-relevant):**
    `test_benchmark.py` — anchor units computed correctly from start bankroll and
    prices; mark-to-market matches hand math; re-invoking anchor creation after a
    simulated restart (reopen db) does **not** move the anchor even though prices
    changed; delta sign is correct when actual beats/loses to baseline; missing
    anchor/price yields `n/a` and never raises into the trade loop.

11. **`risk.py` — the chokepoint.** Implement `RiskManager.check(intent)` with the
    exact ordered checks in §2.7, fail-closed on any un-evaluable input, loud
    WARNING logs, config-driven thresholds, resize logic for per-position cap,
    persistent portfolio-halt trip. **Verify (acceptance-critical):**
    `test_risk_per_position_cap.py`, `test_risk_daily_trade_cap.py`,
    `test_risk_portfolio_halt.py` — each mocks state to the boundary and asserts
    reject/resize; include the kill-switch-first check.

12. **`execution.py`.** `OrderGateway.execute(intent)` → risk check → branch paper/
    live; `PaperFillSimulator` (fill at bid/ask ± `SLIPPAGE_BPS`), records
    simulated trade; live branch asserts `mode==live` and calls the client. Assert
    no fill without an approve/resize RiskDecision. **Verify:**
    `test_execution_paper.py` (paper never calls live methods; oversized intent is
    resized before fill), `test_mode_default_paper.py`.

13. **`signals.py` + `model.py`.** Rule signals from indicators; logistic-
    regression featurize/predict/retrain/persist with cold-start fallback and the
    §2.4 retrain trigger + promotion guard. **Verify:** `test_model.py` — cold
    start returns rule pseudo-prob; after synthetic labeled data, `fit` produces a
    usable `p_win`; retrain-promotion rejects a worse holdout.

14. **`email_report.py`.** `smtplib` Gmail SMTP (STARTTLS:587), `is_configured()`
    guard mirroring `bookhunt/src/smtp.js`, builds daily summary (balance, trades
    with the signal/reason that triggered each, P&L vs bankroll, **the
    actual-vs-hold-baseline comparison from `benchmark.compare()` — portfolio
    value/return, baseline value/return, and the delta side by side**, which caps
    fired, current mode). Reads SMTP creds via `secrets.py`. **Verify:**
    `test_email_report.py` mocks `smtplib.SMTP` and asserts message content
    (including the benchmark comparison line) + that no send happens when
    unconfigured; a `cli report-now --dry-run` prints the body without sending.

15. **`scheduler.py` + `bot.py`.** Wire the loop: every `DECISION_INTERVAL_MIN`,
    check kill → fetch data → indicators → model → intent → gateway → persist;
    ensure the paper-start benchmark anchor is created on the first paper cycle if
    absent (§2.13); daily timer → email + nightly model maintenance. Startup
    rehydrates state (incl. benchmark anchor). **Verify:** a `--once` flag runs a
    single cycle against a mocked client end to end without network, and creates
    the paper anchor exactly once.

16. **`cli.py`.** Subcommands: `run`, `once`, `stop`, `resume`, `status`,
    `set-mode {paper|live}` (live requires typed confirmation + balance check, and
    records the live benchmark anchor), `report-now`, `backtest`. **`status` prints
    the actual portfolio value/return, the hold-baseline value/return, and the
    delta side by side** (same `benchmark.compare()` data as the email), plus mode,
    open positions, caps state, and kill state. **Verify:** `set-mode live` without
    confirmation does not change persisted mode; `status` output includes the
    benchmark comparison (or `n/a` when no anchor).

17. **`scripts/backtest.py`.** Scriptable backtest over downloaded historical
    candles feeding the same indicators/model/risk path (no live orders).
    **Verify:** runs to completion on a saved fixture and prints summary P&L.

18. **`Dockerfile`, `docker-compose.yml`, `Makefile`, `.env.example`, `README.md`.**
    Per §2.11. README = operator runbook incl. **go-live checklist** (create trade-
    only key, place key/smtp files mode 600, run paper N days, review daily emails
    **incl. beating the hold-baseline**, decide `HALT_AUTO_FLATTEN`, `set-mode
    live`) and kill-switch instructions. **Verify:** `make up` builds and the
    container stays up; §7 image/secret inspection passes.

19. **Full test pass + secret/image audit.** `.venv/bin/python -m pytest` green;
    run the §7 credential-leak checks.

---

## 4. Data / model / API changes — n/a (greenfield). New schemas in §5.

---

## 5. State schema (SQLite `state/grow.db`, WAL)

```sql
-- one row per order intent (written BEFORE any live call, for crash idempotency)
CREATE TABLE intents (
  id INTEGER PRIMARY KEY,
  client_order_id TEXT UNIQUE NOT NULL,   -- deterministic idempotency key
  ts_utc TEXT NOT NULL,                   -- ISO8601 UTC, intent time
  product TEXT NOT NULL,                  -- BTC-USD | ETH-USD | SOL-USD
  side TEXT NOT NULL,                     -- buy | sell
  mode TEXT NOT NULL,                     -- paper | live
  requested_notional REAL,               -- USD requested
  risk_action TEXT,                       -- approve | resize | reject
  approved_notional REAL,                 -- after resize
  reason TEXT,                            -- signal/cap reason (also used in email)
  status TEXT NOT NULL DEFAULT 'pending'  -- pending | filled | rejected | error
);

-- one row per executed fill (paper or live)
CREATE TABLE trades (
  id INTEGER PRIMARY KEY,
  client_order_id TEXT NOT NULL REFERENCES intents(client_order_id),
  ts_utc TEXT NOT NULL,                   -- fill time (basis for rolling-24h count)
  product TEXT NOT NULL,
  side TEXT NOT NULL,
  mode TEXT NOT NULL,
  base_size REAL NOT NULL,                -- coin qty
  price REAL NOT NULL,                    -- fill price (incl. simulated slippage in paper)
  notional REAL NOT NULL,                 -- USD
  fee REAL NOT NULL DEFAULT 0
);

-- open position per product (cost basis for P&L + per-position cap)
CREATE TABLE positions (
  product TEXT PRIMARY KEY,
  base_size REAL NOT NULL,
  avg_entry REAL NOT NULL,
  opened_ts_utc TEXT
);

-- one labeled training row per CLOSED position (feeds model)
CREATE TABLE outcomes (
  id INTEGER PRIMARY KEY,
  product TEXT NOT NULL,
  entry_ts_utc TEXT NOT NULL,
  features_json TEXT NOT NULL,            -- feature vector at entry
  label INTEGER NOT NULL,                 -- 1 = profitable over horizon (fee-adj), else 0
  realized_pnl REAL
);

-- buy-and-hold benchmark anchor: EXACTLY ONE ROW PER EPOCH, write-once.
-- Recorded at paper-start (epoch='paper') and at go-live (epoch='live').
-- Created via INSERT-if-absent only; NEVER updated/recomputed (see §2.13).
CREATE TABLE benchmark (
  epoch TEXT PRIMARY KEY,                 -- 'paper' | 'live'
  anchor_ts_utc TEXT NOT NULL,           -- when the baseline was struck (UTC)
  start_bankroll REAL NOT NULL,          -- baseline capital (paper start bankroll | go_live_bankroll)
  btc_price REAL NOT NULL,               -- BTC-USD spot at anchor time
  eth_price REAL NOT NULL,               -- ETH-USD spot at anchor time
  sol_price REAL NOT NULL,               -- SOL-USD spot at anchor time
  btc_units REAL NOT NULL,               -- (start_bankroll/3)/btc_price, fixed forever
  eth_units REAL NOT NULL,               -- (start_bankroll/3)/eth_price, fixed forever
  sol_units REAL NOT NULL                -- (start_bankroll/3)/sol_price, fixed forever
);

-- key/value runtime state (survives restart; source of truth for mode etc.)
CREATE TABLE runtime (
  key TEXT PRIMARY KEY,                   -- 'mode','go_live_bankroll','go_live_ts',
  value TEXT                              -- 'portfolio_halt_tripped','halt_tripped_ts',
);                                        -- 'last_retrain_ts','model_n_samples', etc.

CREATE TABLE model_meta (
  id INTEGER PRIMARY KEY,
  trained_at_utc TEXT, n_samples INTEGER,
  holdout_logloss REAL, promoted INTEGER  -- 1 promoted, 0 rejected
);
```

Persistence rules: **mode**, **go_live_bankroll**, **portfolio_halt_tripped**, the
**benchmark anchor** (one write-once row per epoch), and the **KILL sentinel** all
live in bind-mounted state and are read on startup — an unattended restart resumes
killed/halted/paper exactly as it was, never optimistic, and never re-strikes the
buy-and-hold baseline from a later, more-convenient price. Model = `state/model.pkl`
(joblib) + `model_meta` rows.

---

## 6. Crash / restart safety

- **Mid-trade crash (live):** `intents` row (with `client_order_id`) is written and
  committed *before* the Coinbase call. On restart, for any `status='pending'` live
  intent, the bot **queries Coinbase order status by `client_order_id` before doing
  anything else** and reconciles (`filled`/`rejected`) rather than re-sending —
  idempotency key prevents double-submit.
- **Rolling-24h window** uses stored **UTC** fill timestamps and a trailing
  `now-24h` query, so restarts, DST, and container tz changes cannot widen the
  window. All timestamps stored/compared in UTC; only the daily-report *display*
  and report-hour trigger use local tz.
- **Kill switch & halt trip** persist (sentinel + runtime kv) — fail-closed across
  restarts until an explicit `resume`.
- **Benchmark anchor** persists in the `benchmark` table and is read on startup;
  anchor creation is INSERT-if-absent per epoch, so a restart, container recreate,
  or later price move never re-strikes the baseline — the buy-and-hold comparison
  stays anchored to the true epoch start (§2.13).

---

## 7. Testing & verification (maps to the brief's 9 acceptance criteria)

Run all: `.venv/bin/python -m pytest -v`

| # | Criterion | How proven |
|---|-----------|-----------|
| 1 | Paper mode, live prices, sim fills, never real order | `test_execution_paper.py` + `test_mode_default_paper.py`: default config is paper; gateway routes to simulator; assert `coinbase_client.place_market_*` is **never** called in paper. Manual: `make up` in paper, watch logs show live prices + `mode=paper` simulated trades. |
| 2 | Per-position 25% cap | `test_risk_per_position_cap.py`: mock bankroll + existing position, submit a buy that would exceed 25% → assert `resize` (or `reject` if no headroom), never `approve` at full size. |
| 3 | Daily 5/24h cap | `test_risk_daily_trade_cap.py` with `freezegun`: seed 5 fills inside 24h → 6th intent `reject`; advance clock past the oldest → a new one is allowed (proves trailing window, not calendar reset). |
| 4 | -30% portfolio halt | `test_risk_portfolio_halt.py`: mark-to-market below 0.70×bankroll → any buy `reject`, trip flag persisted, WARNING logged. |
| 5 | Kill switch | `test_killswitch.py` + risk test: engage sentinel → next cycle/`RiskManager.check` returns reject before any order (paper or live). Manual: `make stop` then observe next cycle places nothing; `make status` shows killed. |
| 6 | Daily email sends real content from out-of-repo creds | `test_email_report.py` mocks SMTP; asserts body includes balance/trades/P&L **and the actual-vs-hold-baseline comparison + delta**. **Manual without spamming:** `.venv/bin/python -m src.cli report-now --dry-run` prints the exact body to stdout (no send); then a single real send test: `.venv/bin/python -m src.cli report-now` once, confirm arrival at ericfaris@gmail.com, using creds from `~/.config/coinbase/grow-my-money.smtp`. |
| 7 | Explicit paper→live switch, default paper | `test_mode_default_paper.py` (fresh/garbage config → paper) + `cli set-mode live` requires typed confirmation; no automatic path exists (grep the tree for any assignment of live mode outside `cli set-mode`). |
| 8 | No creds in git or image | **git:** `git ls-files | grep -Ei 'key|secret|smtp|\.env$'` returns nothing but `.env.example`; `.gitignore` covers `.env`/`state`/`*.pkl`. **image:** after `make up`, `docker run --rm --entrypoint sh ericfaris/grow-my-money:latest -c 'ls -la /run/secrets 2>/dev/null; find / -name "*.key" 2>/dev/null'` and `docker history --no-trunc ericfaris/grow-my-money:latest` show **no** key/secret baked in (they only appear via the runtime `:ro` bind mount). |
| 9 | Docker lab pattern + state survives restart | `make up` (`docker compose up -d --build`), `restart: unless-stopped`; place a paper trade, `docker compose restart`, `make status` shows the trade history + mode intact from bind-mounted `state/grow.db`. |
| B | Benchmark hold-baseline correct & persistent (success measure) | `test_benchmark.py`: anchor units = `(start_bankroll/3)/price` per coin; `value()`/`return_pct()` match hand math on new prices; delta sign correct when actual beats/loses to baseline. **Persistence/immutability:** create anchor, reopen the db with *different* current prices, re-run anchor creation → assert the stored anchor (ts, prices, units) is unchanged (write-once, never re-struck across restarts). `n/a` when anchor/price missing, never raising into the loop. Manual: `make status` and a `report-now --dry-run` show actual vs. baseline vs. delta side by side. |

Also: `.venv/bin/python scripts/backtest.py <fixture>` runs the strategy path
offline as a pre-paper sanity check.

---

## 8. Risks & watch-outs

- **Kill switch vs in-flight cycle:** the check must be re-read inside
  `RiskManager.check()` immediately before order placement, not only at cycle top,
  so a kill set mid-cycle still blocks the order. Tested in #5.
- **Rolling-24h correctness:** must be a trailing `now-24h` query on UTC fill
  times, not a calendar-day counter, and must **not** replay a deferred 6th trade
  when the window rolls (reject, don't queue). Easy to get subtly wrong — test with
  `freezegun`.
- **Fail-closed everywhere:** any exception evaluating a cap (balance read fails,
  price missing, DB locked) must reject the order, not skip the check. Do not wrap
  cap checks in a bare `except: pass`.
- **Idempotency / double-submit on crash:** never send a live order without first
  committing the `client_order_id` intent and, on restart, reconciling pending
  live intents against Coinbase before new activity (§6).
- **Persisted-mode vs env drift:** persisted mode is source of truth; a stale
  `.env` MODE must not silently re-arm live nor silently downgrade a deliberately
  live bot — resolve precisely per §2.6 and cover with a test.
- **Model must never block de-risking:** the `p_win` threshold gates **buys only**;
  sells/stops/flatten always proceed (subject to kill switch). A bug that gates
  sells could trap the bot in a losing position.
- **Halt auto-flatten decision (§2.8):** do not enable flattening silently — ship
  default off, surface to the human. If enabled, document its trade-count-cap
  exemption and that it still honors the kill switch.
- **Benchmark anchor must be write-once:** the whole point of the baseline is a
  fixed reference. A bug that re-strikes the anchor on restart, on a price move, or
  when switching mode would silently reset the comparison and make "beating the
  market" unmeasurable. Creation is INSERT-if-absent per epoch only; never UPDATE.
  The live epoch is intentionally distinct from paper (fresh real-money baseline),
  not inherited. The benchmark is **report-only** — it must never touch an order
  path, cap, or the trade loop, and a missing anchor/price shows `n/a` rather than
  raising. Tested in row B.
- **Secret hygiene:** `RESTClient` errors and tracebacks can echo request context;
  keep the redaction log filter on and never log the key/secret/SMTP pass. Verify
  mode-600 on the credential files at startup (warn if looser).
- **Rate limits:** private endpoints allow ~30 req/s; the bot needs only a few
  reads per 30-min cycle, so no aggressive polling — but treat a 429 as
  fail-closed-for-this-cycle (skip trading), never a tight retry spin.
- **Timezone for the daily report** uses local tz for the trigger hour and display
  only; all trade math stays UTC.
- **scikit-learn / pickle compatibility:** pin versions in `requirements.txt`; a
  model pickle trained under one sklearn version may not load under another — on
  load failure, fall back to cold-start rules and retrain, don't crash.

---

## 9. Out of scope (v1) — do not build

- No web UI / dashboard (CLI + logs + one daily email only).
- No coins beyond BTC-USD, ETH-USD, SOL-USD.
- **No auto-liquidation requirement** on halt — halt-only is the default; the
  optional flatten is off-by-default and gated on human sign-off (§2.8).
- **Benchmark stays a single equal-weight (1/3 each) buy-and-hold baseline**
  struck once per epoch — no rebalancing, no alternative benchmarks (S&P, BTC-only,
  dollar-cost-averaging), no historical benchmark charting/graphs. It is a
  report-only number (email + `status`), not a UI feature.
- No Cloudflare Tunnel / public exposure / status page (no `ports:` mapping).
- No tax-lot accounting / tax reporting.
- No multi-user support (single account, single operator).
- No mobile app / push notifications beyond the one daily email.
- Backtest stays a scriptable CLI (`scripts/backtest.py`) — not a polished
  feature, no UI.
