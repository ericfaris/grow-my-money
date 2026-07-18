# grow-my-money — Read-only Web Dashboard — Implementation Plan

> Executor note: you have only this file and the repo at
> `/home/eric/projects/grow-my-money`. Read the referenced source files before
> writing code. Do **not** modify any safety-critical logic (`src/risk.py`,
> `src/killswitch.py`, `src/execution.py`, `src/bot.py` trade path). The
> dashboard is strictly read-only and additive.

---

## 1. Summary

Add a small, good-looking, **read-only** web dashboard that runs as a second
process **inside the existing `app` container** and surfaces what the trading bot
is doing at a glance: positions with unrealized P&L, cash, total value, a
reverse-chronological trade/intent feed (including blocked/rejected intents and
their `reason`), a bot-vs-buy-and-hold comparison with a chart, current
mode/halt/kill status, and the latest model metadata. It polls a JSON endpoint
every ~15s and redraws without a full reload. It opens its **own read-only**
SQLite connection so it can never write to or lock out the bot, reuses the
existing `Portfolio`/`Benchmark`/`CoinbaseClient` modules for computation, and is
served on `127.0.0.1:8420` (host loopback only). A supervising `entrypoint.sh`
plus Docker's `init: true` runs the bot loop and the web server together and
tears both down cleanly on `docker compose stop`/`restart`.

---

## 2. Approach & key decisions

### 2.1 Web framework — FastAPI + uvicorn (chosen)
- **Decision:** `fastapi` + `uvicorn` (plain, not `uvicorn[standard]`). Serve one
  static HTML page and one JSON data endpoint.
- **Why:** matches the codebase's type-hinted style, trivial JSON responses,
  uvicorn shuts down cleanly on SIGTERM (needed for acceptance #5), and plain
  uvicorn avoids the heavier `uvloop`/`httptools` extras — fine for localhost.
- **Rejected:** Flask + waitress (also viable, slightly lighter) — FastAPI was
  listed first in the brief and gives cleaner JSON/async ergonomics. Django
  (far too heavy). Websockets/server-push (explicitly out of scope).

### 2.2 Read-only DB access — separate `mode=ro` connection, never `State.__init__` (chosen)
- **Decision:** the dashboard opens its **own** connection with
  `sqlite3.connect("file:<path>?mode=ro", uri=True)` and additionally issues
  `PRAGMA query_only=ON`. It does **not** instantiate `src.state.State`.
- **Why this matters (critical gotcha):** `State.__init__` runs
  `executescript(SCHEMA)` (`CREATE TABLE IF NOT EXISTS …`) and
  `PRAGMA journal_mode=WAL` — **both are writes**. Using `State` from the
  dashboard would defeat the read-only requirement (acceptance #4). We introduce
  a thin `ReadOnlyState` (section 4) that duck-types only the *read* methods the
  reused modules call, backed by the ro connection, with any write method
  (`set_runtime`, `set_cap_trip`) implemented as a **silent no-op**.
- **WAL note:** the bot keeps the DB open in WAL mode, so the `-wal`/`-shm`
  sidecar files already exist and are readable by uid 1000 (same user, same
  bind-mount). A `mode=ro` reader attaches to the existing shared-memory index
  fine in that situation. **Documented fallback** if `mode=ro` ever raises
  "unable to open database file" on this filesystem: open a normal read-write
  connection but immediately run `PRAGMA query_only=ON` (connection-level write
  ban — still cannot corrupt or block the bot). Prefer `mode=ro` first.
- The reused `Portfolio.cash` property lazily calls `state.set_runtime("paper_cash", …)`
  when the key is absent; making `ReadOnlyState.set_runtime` a no-op keeps
  `Portfolio` working read-only (in practice `paper_cash` is always already
  seeded by the running bot).

### 2.3 Unrealized-P&L price source — own cached `CoinbaseClient`, fallback to last trade / avg_entry (chosen = brief option (a) with a safety net)
- **Decision:** the dashboard builds its **own** `CoinbaseClient` from the same
  key file (exactly as `src/cli.py::cmd_status` does), and wraps
  `get_spot_price` in a **process-local TTL cache** (default 20s, config
  `DASHBOARD_PRICE_TTL_SEC`). All three benchmark products are fetched once per
  refresh and cached. If a live fetch fails (or no key), it **falls back
  per-product** to (1) the most recent `trades.price` for that product, else
  (2) the position's `avg_entry`, and marks that product's price as *stale* in
  the JSON so the UI can badge it.
- **Why:** the brief prefers reusing the working Coinbase client (Ed25519 key
  confirmed functional). `get_best_bid_ask` (what `get_spot_price` uses) is a
  cheap read. The TTL cache means the dashboard hits Coinbase at most once per
  ~20s regardless of page-poll rate or number of viewers, so it cannot hammer
  the API or race the bot's own fetches. The fallback keeps the page fully
  functional offline / when the key is unavailable, which also makes it testable
  without network.
- **Rejected:** sharing the bot's in-memory `_price_cache` (impossible across
  processes); writing prices to the DB from the bot for the dashboard to read
  (adds a write path + schema change for marginal benefit — out of scope).

### 2.4 Halt / kill-switch state source (resolved)
Authoritative sources, all read-only:
- **Kill:** sentinel file `state/KILL`. Read via
  `KillSwitch(state_dir/"KILL").is_engaged()` (pure `path.exists()`, no writes).
- **Halt:** persistent trip flag in the `runtime` kv table under key
  `portfolio_halt_tripped` (constant `src.risk.HALT_FLAG`), plus its timestamp
  `portfolio_halt_tripped_ts`. Read via `ReadOnlyState.is_cap_tripped(HALT_FLAG)`
  (a `SELECT` on `runtime`). This is exactly what `bot.py`'s startup line and
  `cli.py::_print_status` read.
- **Mode:** `runtime` key `mode` (`ReadOnlyState.get_mode()`).
- **Critical:** the dashboard must **never** call
  `RiskManager.evaluate_portfolio_halt()` — that method **writes** the trip flag.
  It only reads the already-persisted flag.

### 2.5 Process supervision — `entrypoint.sh` + Docker `init: true` (chosen)
- **Decision:** replace the Dockerfile `ENTRYPOINT` with a small `entrypoint.sh`
  (bash) that backgrounds both `python -m src.bot` and `python -m src.dashboard`,
  traps `SIGTERM`/`SIGINT` to forward the signal to both children, and uses
  `wait -n` so that if **either** process exits the script tears down the other
  and exits (so a crashed bot doesn't leave a half-alive container that
  `restart: unless-stopped` won't restart). Add `init: true` to the compose
  service so Docker injects **tini** as PID 1 to reap zombies and forward signals
  to the script.
- **Why:** simplest correct single-container/two-process pattern; no extra Python
  process-manager dependency (honcho/supervisor). `python:3.12-slim` (Debian
  bookworm) includes `/bin/bash`, so `wait -n` is available. tini via
  `init: true` is built into Docker/compose — no image change needed to get it.
- **Rejected:** `honcho`/`supervisord` (extra dep, heavier than warranted);
  bare `&` backgrounding without a trap (leaves orphans on stop — fails
  acceptance #5); running uvicorn as PID 1 and the bot as a child, or vice-versa
  (whichever is the child won't get clean SIGTERM without a supervisor).

### 2.6 Charting — hand-rolled inline SVG line chart, server-computed series, no external lib (chosen)
- **Decision:** **no charting library, no CDN.** The server reconstructs a bot
  **equity curve** from the `trades` table (section 3.4) and returns it as JSON
  points; a ~40-line vanilla-JS function in the page draws an inline `<svg>` line
  chart (bot equity over time) with a reference line at the starting bankroll and
  a labeled marker for the **current benchmark value** at the right edge. Headline
  numbers (bot value, benchmark value, delta %) come from `Benchmark.compare`.
- **Why:** the brief prefers offline/local-only robustness over a CDN
  `<script src>` (this box's browser context may lack outbound internet).
  Hand-rolled SVG keeps the image dependency-free, self-contained, and fully
  offline, and satisfies "at least one real chart / equity curve". Server-side
  reconstruction is unit-testable (section 6).
- **Chart colors/typography:** **before writing any chart or color code, invoke
  the `dataviz` skill** (via the Skill tool — it is a managed skill, not a repo
  file) and follow its palette/mark guidance. Use one accent hue for the bot
  series, a muted neutral for the benchmark reference, semantic green/red only for
  P&L sign. Support light/dark via `prefers-color-scheme`.
- **Rejected:** Chart.js via CDN (offline-fragile — explicitly discouraged);
  vendoring Chart.js (~200 KB blob to commit for one line chart — overkill);
  server-side matplotlib PNG (matplotlib already indirectly available via deps
  but adds render latency and a heavy import into the web process — unnecessary).

### 2.7 Binding — bind `0.0.0.0` **inside** the container, restrict to loopback at the compose port map (chosen)
- **Decision:** uvicorn binds `0.0.0.0:8420` inside the container; the compose
  `ports:` entry `"127.0.0.1:8420:8420"` restricts host exposure to loopback.
- **Why (gotcha):** if uvicorn bound `127.0.0.1` *inside* the container, the
  Docker port-mapping bridge could not reach it and `curl 127.0.0.1:8420` from
  the host would fail. Host-side loopback restriction is done by the `ports:`
  mapping prefix, not by the in-container bind address.

---

## 3. Data / computation design

### 3.1 The single JSON endpoint
`GET /api/data` returns everything the page needs in one payload (keeps polling
simple). Shape (all money as numbers, not pre-formatted strings — the client
formats):

```jsonc
{
  "generated_at": "2026-07-17T18:03:00+00:00",
  "mode": "paper",
  "status": {
    "kill_engaged": false,
    "halt_tripped": false,
    "halt_tripped_ts": null,
    "trades_24h": 2,
    "max_trades_24h": 5
  },
  "prices": { "BTC-USD": {"price": 61000.0, "source": "live"},
              "ETH-USD": {"price": 3000.0, "source": "last_trade"}, ... },
  "portfolio": {
    "cash": 8200.0,
    "positions_value": 1900.0,
    "total_value": 10100.0,
    "bankroll": 10000.0,
    "positions": [
      { "product": "BTC-USD", "base_size": 0.02, "avg_entry": 59000.0,
        "price": 61000.0, "price_source": "live",
        "market_value": 1220.0, "unrealized_pnl": 40.0, "unrealized_pnl_pct": 0.0339,
        "opened_ts_utc": "2026-07-10T…" }
    ]
  },
  "benchmark": {                    // from Benchmark.compare(mode, …)
    "actual_value": 10100.0, "actual_return_pct": 0.01,
    "baseline_value": 10250.0, "baseline_return_pct": 0.025,
    "delta_pct": -0.015
  },
  "equity": [ {"ts": "…", "value": 10000.0}, {"ts": "…", "value": 10040.0}, … ],
  "activity": [                     // reverse-chronological, capped (e.g. 100)
    { "kind": "trade", "ts_utc": "…", "product": "BTC-USD", "side": "buy",
      "base_size": 0.02, "price": 59000.0, "notional": 1180.0, "fee": 7.08,
      "reason": "ema_cross; p_win=0.61 model=lr", "status": "filled" },
    { "kind": "intent", "ts_utc": "…", "product": "ETH-USD", "side": "buy",
      "requested_notional": 1000.0, "approved_notional": 0.0,
      "risk_action": "reject", "reason": "daily trade cap 5/24h reached",
      "status": "pending" }
  ],
  "model": { "trained_at_utc": "…", "n_samples": 120,
             "holdout_logloss": 0.62, "promoted": true },
  "refresh_sec": 15
}
```

### 3.2 Positions + unrealized P&L
For each row in `positions` (`ReadOnlyState.open_positions()`):
`price = price_source(product)`; `market_value = base_size * price`;
`unrealized_pnl = base_size * (price - avg_entry)`;
`unrealized_pnl_pct = (price - avg_entry) / avg_entry` (guard `avg_entry > 0`).
`price_source` label comes from the price cache (`live` / `last_trade` / `entry`).

### 3.3 Portfolio value & benchmark — reuse existing modules
- Build `Portfolio(read_only_state, cfg.paper_start_bankroll)` and call
  `portfolio.total_value(price_source)` / `portfolio.cash` / `portfolio.bankroll`
  — unchanged code. `positions_value = total_value - cash`.
- Build `Benchmark(read_only_state)` and call
  `benchmark.compare(mode, portfolio, prices, price_source)` — exactly like
  `cli.py::_print_status`. `epoch == mode` ("paper"/"live").
- Both are report-only and never raise into the caller; still wrap the assembly
  in try/except so one bad field can't 500 the whole endpoint (degrade to
  `null`).

### 3.4 Equity-curve reconstruction (new, testable)
Pure function, e.g. `reconstruct_equity(trades, start_bankroll, current_prices)`
in the new data module:
- Start: `cash = start_bankroll` (paper) / `go_live_bankroll` (live, fall back to
  paper start if unset); `holdings = {}`; `last_price = {}`.
- Replay `trades` ordered by `ts_utc` ascending:
  - `buy`:  `cash -= (notional + fee)`; `holdings[p] += base_size`.
  - `sell`: `cash += (notional - fee)`; `holdings[p] -= base_size`.
  - `last_price[p] = price` (the recorded trade price).
  - Append `{ts: trade.ts_utc, value: cash + Σ holdings[p]*last_price[p]}`.
- Prepend a starting point `{ts: anchor_ts or first_trade_ts, value: start_bankroll}`.
- Append a final point at `generated_at` using **current** prices for
  mark-to-market so the curve's last value equals `portfolio.total_value`.
- Empty trades ⇒ a flat two-point line at `start_bankroll` → current total.
This uses only stored data + current prices; no historical price store (out of
scope). The benchmark is drawn as endpoints only (anchor value = start_bankroll →
current `baseline_value`) since we don't persist historical prices — label it
clearly in the UI.

### 3.5 Activity feed
- Trades: `SELECT * FROM trades ORDER BY ts_utc DESC LIMIT 100`, join each to its
  intent `reason`/`status` by `client_order_id` (mirror the join in
  `email_report.build_report_body`).
- Intents: `SELECT * FROM intents ORDER BY ts_utc DESC LIMIT 100` — include ones
  where `risk_action != 'approve'` or `status != 'filled'` so blocked/rejected
  actions with their `reason` are visible.
- Merge, de-dupe (a filled intent already appears as its trade — key on
  `client_order_id`; prefer the `trade` record and drop the duplicate intent, but
  **keep** non-filled/blocked intents), sort by `ts_utc` desc, cap at ~100.

---

## 4. New / changed files

### New files
1. **`src/dashboard_data.py`** — read-only data layer (no web framework import):
   - `class ReadOnlyState` — opens the `mode=ro` connection (with `query_only`
     PRAGMA + fallback per 2.2); implements the read subset used by reused
     modules and the endpoint: `get_runtime`, `get_mode`, `is_cap_tripped`,
     `open_positions`, `get_benchmark_anchor`, `latest_model_meta`,
     `trades_in_last_24h`, plus `.conn` for ad-hoc SELECTs (activity, last trade
     price). Write methods `set_runtime`/`set_cap_trip`/`set_mode` are no-ops.
     Copy the SQL/return shapes verbatim from `src/state.py` (do not import
     `State`). Expose `close()`.
   - `class PriceProvider` — builds a `CoinbaseClient` lazily from the key file
     (reuse `src.secrets.coinbase_key_path` + `CoinbaseClient.from_key_file`,
     same try/except as `cli.cmd_status`); `get(product) -> (price, source)` with
     a TTL cache; fallback to last trade price (query `trades`) then `avg_entry`;
     exposes a plain `price_source(product) -> float` callable for
     `Portfolio`/`Benchmark`, and a `dict` of the source labels.
   - `reconstruct_equity(...)` — section 3.4 (pure, no I/O).
   - `build_payload(cfg, ro_state, price_provider) -> dict` — assembles the full
     `/api/data` dict (sections 3.1–3.5), each sub-block try/except-guarded.
2. **`src/dashboard.py`** — FastAPI app + `main()`:
   - `GET /` → returns the page HTML (read from `src/dashboard_web/index.html`).
   - `GET /api/data` → `JSONResponse(build_payload(...))`. Open a fresh
     `ReadOnlyState` per request (cheap; avoids cross-thread SQLite issues) and
     `close()` it in a `finally`. Reuse a module-level `PriceProvider` (so the
     TTL cache persists across requests).
   - `main()` reads `DASHBOARD_HOST` (default `0.0.0.0`), `DASHBOARD_PORT`
     (default `8420`) from config; calls
     `uvicorn.run(app, host=…, port=…, access_log=False, log_level="info")`.
     `access_log=False` keeps the 15s polls from flooding `docker compose logs`.
   - `python -m src.dashboard` must work (add `if __name__ == "__main__": main()`).
     Call `setup_logging()` at startup so its log lines interleave with the bot's.
3. **`src/dashboard_web/index.html`** — the self-contained page: inline `<style>`
   (layout, typography, light/dark via `prefers-color-scheme`) and inline
   `<script>` (poll `/api/data` every `refresh_sec`, render cards + activity
   table + inline-SVG equity chart). No external `<script src>`/`<link>`. Lives
   under `src/` so the existing `COPY src/ ./src/` picks it up. Design per the
   `dataviz` skill; must not be a bare table dump (acceptance #7).
4. **`entrypoint.sh`** (repo root) — supervisor (section 2.5). Content:
   ```bash
   #!/bin/bash
   set -u
   term() {
     [ -n "${BOT_PID:-}" ] && kill -TERM "$BOT_PID" 2>/dev/null || true
     [ -n "${WEB_PID:-}" ] && kill -TERM "$WEB_PID" 2>/dev/null || true
   }
   trap term TERM INT
   python -m src.bot & BOT_PID=$!
   python -m src.dashboard & WEB_PID=$!
   # Exit as soon as either child exits; then stop the other and reap.
   wait -n
   term
   wait
   ```
5. **`tests/test_dashboard.py`** — data-layer tests (section 6).

### Changed files
6. **`requirements.txt`** — append (keep the `coinbase-advanced-py==1.8.4` bump):
   ```
   fastapi==0.115.6
   uvicorn==0.34.0
   ```
   (Pin to whatever the resolver installs cleanly for Python 3.12 if these exact
   pins are unavailable; both are pure-Python + starlette/pydantic which are
   already 3.12-compatible.) `httpx` is **not** required — tests hit the data
   layer directly, not the HTTP endpoint.
7. **`src/config.py`** — add three fields to `Config` (all with defaults, so
   existing `Config(...)` construction in `tests/conftest.py` is unaffected):
   `dashboard_host: str = "0.0.0.0"`, `dashboard_port: int = 8420`,
   `dashboard_refresh_sec: int = 15`, `dashboard_price_ttl_sec: int = 20`; wire
   them in `load_config` via the existing `_get/_get_int` helpers
   (`DASHBOARD_HOST`, `DASHBOARD_PORT`, `DASHBOARD_REFRESH_SEC`,
   `DASHBOARD_PRICE_TTL_SEC`).
8. **`.env.example`** — add a `# --- Dashboard ---` block documenting the four new
   vars with their defaults.
9. **`Dockerfile`** — `COPY entrypoint.sh ./`, ensure executable
   (`RUN chmod +x /app/entrypoint.sh` before the `chown -R 1000:1000 /app` line,
   or use `COPY --chmod=0755`), and change
   `ENTRYPOINT ["python", "-m", "src.bot"]` → `ENTRYPOINT ["/app/entrypoint.sh"]`.
   The existing `COPY src/ ./src/` already ships `dashboard.py`,
   `dashboard_data.py`, and `dashboard_web/`. `.dockerignore` already excludes
   `tests`, `state`, `.env`, etc. — no change needed.
10. **`docker-compose.yml`** — under service `app`: add
    ```yaml
    init: true
    ports:
      - "127.0.0.1:8420:8420"
    ```
    (Remove/replace the `# NOTE: no ports` comment.) Optionally add the four
    `DASHBOARD_*` vars to `environment:` if overriding defaults — not required
    since defaults are correct.
11. **`Makefile`** — (optional, nice) add a `dash:` helper target:
    `@echo open http://127.0.0.1:8420 ; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8420/`.
12. **`README.md`** — (optional) a short "Dashboard" subsection noting the
    local-only URL, the `init: true`/dual-process entrypoint, and that it is
    read-only.

---

## 5. Step-by-step tasks (ordered, independently verifiable)

1. **Config plumbing.** Add the four `DASHBOARD_*` fields + env wiring in
   `src/config.py`; add the `.env.example` block. Verify:
   `.venv/bin/python -c "from src.config import load_config; c=load_config(); print(c.dashboard_port, c.dashboard_refresh_sec)"` → `8420 15`. Run the
   existing suite to confirm no regression from the new fields.
2. **Read-only data layer — `ReadOnlyState`.** Implement in
   `src/dashboard_data.py`. Verify against a seeded temp DB (create via `State`,
   close, reopen ro): positions/runtime/model reads match; a `set_runtime` call
   is a no-op and does not raise; the underlying file is not modified (compare
   mtime/`PRAGMA quick_check` is out of scope — just assert reads work and the
   connection rejects writes, e.g. a raw `INSERT` raises).
3. **`PriceProvider`.** Implement live fetch + TTL cache + fallback chain. Verify
   with a fake client (inject a stub exposing `get_spot_price`) that: cache
   returns the same value within TTL without re-calling; on fetch exception it
   falls back to last trade price then `avg_entry` and labels the source.
4. **`reconstruct_equity`.** Implement pure function. Verify: empty trades → two
   points (`start_bankroll` → current total); a buy-then-sell sequence produces a
   monotonically-timestamped series whose final value equals cash+MTM at current
   prices.
5. **`build_payload`.** Assemble the full dict reusing `Portfolio` + `Benchmark`.
   Verify the returned dict has every top-level key from section 3.1 and that
   `portfolio.total_value ≈ cash + positions_value` and that a blocked intent
   appears in `activity` with its `reason`.
6. **FastAPI app — `src/dashboard.py`.** Endpoints + `main()`. Verify locally
   (host venv, no container):
   `DASHBOARD_PORT=8421 .venv/bin/python -m src.dashboard &` then
   `curl -s 127.0.0.1:8421/ | head` returns HTML and
   `curl -s 127.0.0.1:8421/api/data | python -m json.tool` returns valid JSON;
   kill it. (Coinbase key may be absent on the host → prices fall back to
   last_trade/entry; that is expected and fine.)
7. **Page — `src/dashboard_web/index.html`.** Invoke the `dataviz` skill first,
   then build the layout, cards, activity table, and inline-SVG equity chart with
   the polling loop. Verify by opening the page from step 6 in a browser (or the
   `webapp-testing` skill) — it renders, auto-refreshes, and draws the chart.
8. **Supervisor + Docker.** Add `entrypoint.sh`; edit `Dockerfile` ENTRYPOINT and
   `chmod`; edit `docker-compose.yml` (`init: true`, `ports`). Rebuild:
   `docker compose up -d --build`. Verify acceptance #1, #5 (section 6).
9. **Tests.** Add `tests/test_dashboard.py` covering steps 2–5. Run full suite.
10. **Docs/Makefile (optional).** README subsection + `make dash`.

---

## 6. Testing & verification (maps to acceptance criteria)

Run everything from `/home/eric/projects/grow-my-money`. Nothing is on PATH — use
`.venv/bin/python` / the venv, matching the Makefile.

- **AC#6 — existing suite still green (do this first and last):**
  `.venv/bin/python -m pytest -q`  → all pass. (New config fields have defaults;
  `conftest.py`'s `Config(...)` keeps working.)
- **New unit tests — `tests/test_dashboard.py`:** cover `ReadOnlyState` reads +
  write-rejection, `PriceProvider` cache+fallback (fake client), and
  `reconstruct_equity` (empty + buy/sell). No network, no real credentials —
  follow `tests/conftest.py` fixture style (`state`, `db_path`, `FakePriceSource`).
- **AC#1 — both processes in one container:**
  `docker compose up -d --build` then
  `docker compose logs app` shows the bot's `Startup: mode=… halt_tripped=… kill=…`
  line **and** a uvicorn "Uvicorn running on http://0.0.0.0:8420" line; and
  `curl -s -o /dev/null -w "%{http_code}\n" 127.0.0.1:8420/` → `200`.
- **AC#2 — page renders all sections:** open `http://127.0.0.1:8420/`; confirm
  positions+unrealized P&L, cash, total value, the reverse-chron trade/intent
  feed (with at least one non-filled intent showing its `reason` — force one if
  needed by exceeding the trade cap or a blocked buy), bot-vs-benchmark numbers +
  chart, mode/halt/kill status, and latest model metadata. No console/network
  errors.
- **AC#3 — live auto-refresh:** with the page open, force a paper trade on the
  host via `.venv/bin/python -m src.cli once` (writes to the same bind-mounted
  `state/grow.db`); the new trade appears in the feed within one poll interval
  (~15s) without a manual reload.
- **AC#4 — read-only, non-interfering:** after the dashboard has polled for
  several cycles, confirm the bot still logs successful decision cycles/trades in
  `docker compose logs -f app` (no "database is locked" / "readonly database"
  errors from either process). Optionally assert the dashboard connection is
  read-only by attempting a write through it in a test (`INSERT` raises).
- **AC#5 — clean stop/restart:**
  `docker compose restart app` then `docker compose ps` shows the service healthy
  and `docker compose logs app` shows both processes starting again with no
  orphan warnings; `docker compose stop` returns promptly (SIGTERM handled — the
  `entrypoint.sh` trap forwards to both children; `init: true`/tini is PID 1).
  Sanity: `docker compose exec app ps -e` (or inspect) shows no leftover zombie
  after a stop/start.
- **AC#7 — visual quality:** subjective check that the page has real layout,
  readable type, a coherent light/dark color system, and a genuine chart (the
  inline-SVG equity curve), not an unstyled `<table>` dump.

---

## 7. Risks & watch-outs

1. **Do not use `State` from the dashboard.** `State.__init__` writes (schema +
   WAL PRAGMA). Use `ReadOnlyState`. This is the single most important correctness
   point for AC#4.
2. **Never call `evaluate_portfolio_halt()`** (or anything that writes) from the
   dashboard — read the persisted `portfolio_halt_tripped` flag only.
3. **Bind `0.0.0.0` inside the container**, restrict at the compose `ports:`
   prefix (`127.0.0.1:8420:8420`). Binding `127.0.0.1` inside the container makes
   the host `curl` fail (section 2.7).
4. **`entrypoint.sh` signal handling:** must `trap … TERM INT` and forward to both
   PIDs; `wait -n` requires bash (present in the slim image) — shebang must be
   `#!/bin/bash`, not `/bin/sh` (dash lacks `wait -n`). Ensure the file is
   executable in the image (`chmod +x`) and owned appropriately (the `chown -R
   1000:1000 /app` already covers it if the `COPY` precedes it).
5. **`init: true` is required** so tini reaps zombies and forwards SIGTERM to the
   script; without it the bash script as PID 1 has non-standard signal semantics.
6. **SQLite WAL + `mode=ro`:** works while the bot holds WAL open (shm exists). If
   it ever raises "unable to open database file", use the documented `query_only`
   fallback (section 2.2). Do **not** "fix" it by dropping to a plain read-write
   connection without `query_only`.
7. **`Portfolio.cash` lazy write:** relies on `ReadOnlyState.set_runtime` being a
   no-op. Keep that no-op — do not raise from it (raising would break
   `Portfolio.cash` when `paper_cash` is unexpectedly absent).
8. **Coinbase rate limits / two callers:** keep the `PriceProvider` TTL cache
   (default 20s) and fetch all three products per refresh; never fetch per HTTP
   request uncached. The dashboard must degrade gracefully (fallback prices) when
   the key is missing or a fetch fails — it must never 500 the whole page over a
   price gap.
9. **Activity de-dup:** a filled intent also exists as a trade (same
   `client_order_id`). Show the trade, drop the duplicate intent, but keep
   non-filled/blocked intents (that visibility is a core requirement).
10. **uvicorn access logs:** set `access_log=False` or the 15s polls will spam
    `docker compose logs`.
11. **No CDN.** The page must be fully self-contained (inline CSS/JS/SVG). Do not
    add a `<script src="https://…">` — this box's browser context may be offline.
12. **Keep the `coinbase-advanced-py==1.8.4` bump** in `requirements.txt` (it's an
    intentional fix); only append the new deps.

---

## 8. Out of scope (do not build)

- Public exposure / Cloudflare Tunnel — local loopback only.
- Auth / login.
- **Any** write or control action from the dashboard (no start/stop/kill/resume
  buttons — kill switch stays CLI-only).
- A new time-series/equity table or migration — reconstruct the curve from
  existing `trades` at request time; do not persist historical prices.
- Websockets / server-push — polling only.
- Mobile-specific responsive design (basic light/dark theming is in; pixel-perfect
  mobile is not required).
- Historical benchmark curve from stored historical prices (we don't store them;
  draw the benchmark as anchor→current endpoints).
- Any change to the trading/risk/execution logic.
```
