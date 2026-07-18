# grow-my-money — Read-only Web Dashboard — Concept Brief

## Problem
`grow-my-money` is a paper/live crypto trading bot running as a headless Docker
container (`grow-my-money-app-1`, lab machine, `~/projects/grow-my-money`,
compose project `grow-my-money`). All state lives in a SQLite DB at
`state/grow.db` (bind-mounted into the container at `/app/state/grow.db`).
Right now the only way to see what the bot is doing is `docker compose logs`,
the CLI (`src/cli.py`: `status`, `report-now --dry-run`), or querying the
SQLite file directly. There's no at-a-glance view of positions, trade history,
performance vs. benchmark, or system/safety status.

## Goal
A small, good-looking, read-only web dashboard, served locally, that surfaces
what's happening in the bot at a glance and auto-refreshes while you watch it.

## In scope (v1)
- **Portfolio & positions**: current holdings per product (`positions` table:
  `base_size`, `avg_entry`, `opened_ts_utc`), cash balance (`runtime` table,
  key `paper_cash`), unrealized P&L per position (requires a live price fetch
  or last-known price — see Open Questions), and total portfolio value.
- **Trade / intent history**: reverse-chronological list from `trades` (filled
  orders: price, size, notional, fee) and `intents` (all attempted actions,
  including ones with `risk_action` != `approve` / `status` != `filled`, with
  the `reason` field shown so blocked/rejected intents are visible, not just
  successful trades).
- **Bot vs. benchmark**: compare bot performance against the buy-and-hold
  benchmark struck at epoch start (`benchmark` table: `start_bankroll`,
  `btc_price`/`eth_price`/`sol_price`, `btc_units`/`eth_units`/`sol_units` at
  anchor time, keyed by `epoch` e.g. `'paper'`). Present as a chart (equity
  curve or at minimum current bot value vs. current benchmark value) plus the
  headline numbers.
- **System status**: current `mode` (paper/live) and `paper_cash` from
  `runtime`; kill-switch / halt state (see `src/killswitch.py` and
  `src/risk.py` for how halt/kill state is represented — likely also in
  `runtime` or a sentinel file under `state/`); latest `model_meta` row
  (`trained_at_utc`, `n_samples`, `holdout_logloss`, `promoted`).
- **Auto-refresh**: the page polls and updates every ~10-30s without a full
  manual reload (simple JS `fetch`-and-redraw or `<meta refresh>`-style
  polling is fine — no need for websockets).
- **Nicer visual design**: this is not a bare HTML-table dashboard — invest in
  layout, a clean color/typography system, and at least one real chart
  (equity curve, bot vs. benchmark) rather than just numbers in tables. Use
  the `dataviz` skill's guidance for any chart/color work (see
  `~/.claude/skills/dataviz` — read it before writing chart code or picking
  chart colors). Keep it dependency-light: inline CSS/JS or a single small
  charting lib (e.g. Chart.js via CDN — but note this box may not always have
  outbound internet from the browser context the user views it in, so prefer
  vendoring/inlining over CDN `<script src>` if easy; if a CDN dependency is
  used, note it clearly in the plan so the user can decide).

## Out of scope (v1)
- Public exposure via Cloudflare Tunnel (local-only for now — bind
  `127.0.0.1` only, per lab convention seen in bookhunt/gttp/slipcast).
- Auth/login (not needed while local-only).
- Any write/control actions from the dashboard (no start/stop/kill buttons —
  read-only view only; killswitch stays a CLI-only operation for now).
- Historical equity curve reconstruction beyond what's cheaply derivable from
  existing tables (don't build a new time-series table/migration for v1
  unless the plan judges it trivial — a "current value vs. benchmark value"
  snapshot plus trade-by-trade points is an acceptable v1 chart).
- Websockets / server-push — polling is fine.
- Mobile-specific responsive design (nice-to-have, not required).

## Constraints
- Must run **inside the existing `app` container** (compose service `app`,
  image `ericfaris/grow-my-money:latest`) as a second lightweight web server
  process — not a separate container. The brief author (this session)
  confirmed this with the user: add FastAPI or Flask, reading `grow.db`
  directly (read-only connection / read-only queries), and add a new bound
  port to `docker-compose.yml`.
    - Concretely this likely means the container's entrypoint needs to run
      two things (the existing bot loop `python -m src.bot`, and the new web
      server) — figure out the cleanest way to do that (e.g. a small
      supervisor script, or `honcho`/`overmind`-style process runner, or a
      minimal `entrypoint.sh` that backgrounds one and execs the other with
      signal handling). Keep it simple; this is a single-container-two-process
      pattern, not a full process manager. Whatever is chosen must still
      respond correctly to `docker compose stop`/`restart unless-stopped`
      (i.e. SIGTERM should cleanly stop both processes, not leave orphans).
- Must not interfere with the trading bot's own SQLite access. `state.py`
  presumably already handles the bot's own connection; the dashboard should
  open its own separate, read-only SQLite connection (e.g. `sqlite3.connect(
  ..., uri=True)` with `?mode=ro`, or just a plain read-only-usage connection)
  so it can't corrupt or lock out the bot's writes. Check `src/state.py` for
  existing connection patterns/pragmas (e.g. WAL mode) before deciding the
  exact approach.
- Bind the new web server to `127.0.0.1:8420` on the host (port chosen to
  avoid collision with sibling lab apps: bookhunt=3000, gttp=8100,
  slipcast=8000). Add `- "127.0.0.1:8420:8420"` under the `app` service's
  `ports:` in `docker-compose.yml` (currently has no `ports:` block at all —
  add one).
- Keep the existing `Dockerfile`/`docker-compose.yml` conventions: non-root
  user 1000:1000, `no-new-privileges`, credentials only via the existing
  `:ro` mounts, no secrets baked into the image, `PYTHONUNBUFFERED=1`.
- Add any new Python deps (FastAPI/Flask/uvicorn, and a charting approach) to
  `requirements.txt` alongside the existing pinned deps (currently just
  bumped `coinbase-advanced-py` to `1.8.4` in an uncommitted change — see
  below).
- Follow existing code conventions: type hints, small focused modules (see
  `src/*.py` — e.g. `src/state.py`, `src/portfolio.py`, `src/benchmark.py` for
  how existing modules read this data) rather than one giant script.

## Acceptance criteria
1. `docker compose up -d --build` brings up the `app` container and **both**
   the trading bot loop and the new dashboard web server are running inside
   it (verify via `docker compose logs app` showing both starting, and `curl
   127.0.0.1:8420/` from the host returning HTML).
2. The dashboard page (`http://127.0.0.1:8420/`) renders, without errors,
   showing: current positions with unrealized P&L, cash balance, total
   portfolio value; a reverse-chronological trade/intent list including any
   non-filled/blocked intents with their `reason`; a bot-vs-benchmark
   comparison including a chart; current mode/halt/kill status; latest model
   metadata.
3. The page auto-refreshes (polls) without a manual reload, and reflects new
   data if the bot writes a new trade while the page is open (can be
   verified by forcing a paper trade via `.venv/bin/python -m src.cli once`
   or similar and observing the dashboard update within its poll interval).
4. The dashboard connection to `grow.db` is read-only and does not interfere
   with the bot's own writes — confirm by checking the bot continues to log
   successful trades/decision cycles after the dashboard has been running and
   polling for a few cycles.
5. `docker compose restart app` / `docker compose stop` cleanly stops both
   processes (no orphaned process, no container stuck in a bad state) —
   verify via `docker compose ps` and `docker compose logs` after a restart.
6. The full existing test suite still passes:
   `.venv/bin/python -m pytest -q` (root: `/home/eric/projects/grow-my-money`).
7. Visual quality: the dashboard is not a bare unstyled HTML table dump — it
   has a coherent layout, readable typography, a sensible color system (see
   `dataviz` skill for chart-color guidance), and at least one real chart.

## Open questions & decisions made
- **Unrealized P&L needs a current price.** The bot already fetches live
  prices via `src.coinbase_client`/`src.marketdata` for its decision loop.
  Decide in the plan whether the dashboard (a) makes its own lightweight
  price fetch (re-using `src.marketdata` / `src.coinbase_client`, which now
  works — Ed25519 CDP key confirmed functional as of this session), or (b)
  falls back to last trade price / avg_entry when a live price isn't cheaply
  available, to avoid hammering Coinbase's API from two places. Prefer (a)
  reusing existing client code if it's cheap and rate-limit-safe, but the
  planner should pick and justify.
- **Halt/kill-switch state representation** — the brief author did not fully
  trace where halt-tripped/kill state lives (log line at bot startup shows
  `halt_tripped=False kill=False`, sourced from `src/bot.py` — check
  `src/killswitch.py`, `src/risk.py`, and how `bot.py` builds that startup
  log line to find the authoritative source before wiring the dashboard to
  it).
- **Process supervision inside one container** — the planner should decide
  the concrete mechanism (entrypoint script, tini + backgrounding, etc.) and
  document exactly what changes in `Dockerfile`/`docker-compose.yml`
  (`ENTRYPOINT`/`CMD`).
- **Charting approach** — planner decides Chart.js-via-CDN vs. vendored JS
  vs. server-side chart image (e.g. matplotlib rendering a PNG) vs. inline
  SVG/D3-lite. Note the tradeoff (external CDN dependency vs. build
  complexity) explicitly in the plan for visibility, but don't re-ask the
  user — pick a sensible default (vendoring a small charting lib or
  hand-rolled SVG/Canvas is preferable to a CDN fetch, to keep it working
  fully offline/local-only).
- Plan approval gate was **skipped** at the user's request — plan will be
  sanity-checked by this session, not shown to the user before build starts.

## Relevant files/areas
- `state/grow.db` — SQLite DB, schema seen live: `intents`, `trades`,
  `positions`, `outcomes`, `benchmark`, `runtime`, `model_meta` (columns
  listed above under "In scope").
- `src/state.py` — existing DB access patterns/connection handling; follow
  its conventions for the dashboard's read path.
- `src/portfolio.py`, `src/benchmark.py` — likely already contain logic for
  computing portfolio value / benchmark comparison that the dashboard should
  reuse rather than reimplementing.
- `src/killswitch.py`, `src/risk.py` — halt/kill state source of truth.
- `src/bot.py` — main loop; also where the "Startup: mode=... halt_tripped=...
  kill=..." log line is built (useful reference for what fields exist).
- `src/cli.py` — existing `status`/`report-now` commands may already assemble
  similar summary data worth reusing.
- `src/marketdata.py`, `src/coinbase_client.py` — live price fetching, if the
  plan chooses to fetch current prices for unrealized P&L.
- `src/config.py` — env/config conventions (`.env`, `.env.example`) if the
  dashboard needs any new config values (e.g. port, refresh interval).
- `Dockerfile`, `docker-compose.yml` — need updating for the new port and
  dual-process entrypoint.
- `requirements.txt` — add new deps here (currently pinned versions, see
  constraints above).
- `~/.claude/skills/dataviz` — read before writing any chart/color code.

## Repo commands & tree state
- **Test**: `.venv/bin/python -m pytest -q` (run from
  `/home/eric/projects/grow-my-money`; do not assume `pytest`/`python` are on
  PATH — the venv at `.venv` must be used explicitly, matching the
  `Makefile`'s `test:` target).
- **Build/deploy container**: `docker compose up -d --build` (from the same
  directory; `Makefile`'s `up:` target additionally stamps `GIT_SHA`/
  `BUILD_TIME` — `GIT_SHA=$(git rev-parse --short HEAD) BUILD_TIME=$(date -u
  +%Y-%m-%dT%H:%M:%SZ) docker compose up -d --build`, but a plain `docker
  compose up -d --build` works fine for iteration).
- **Logs**: `docker compose logs -f --tail=200` (or `Makefile`'s `logs:`).
- **CLI** (host venv, not container): `.venv/bin/python -m src.cli status`,
  `.venv/bin/python -m src.cli once`, `.venv/bin/python -m src.cli
  report-now --dry-run`, `.venv/bin/python -m src.cli stop`/`resume`.
- **Git tree state**: this repo has not had its initial commit yet — `git
  status --short` shows the entire project tree as newly staged (`A`) from a
  prior session's initial scaffold, EXCEPT `requirements.txt` which is
  `AM` (staged + modified): it was bumped from
  `coinbase-advanced-py==1.8.2` to `==1.8.4` earlier in this session (fixes
  Ed25519 CDP key support — confirmed working). This bump is a real,
  intentional fix, not stray work — keep it. The dashboard build will add
  further deps to this same file.
- **Running container**: `grow-my-money-app-1` (compose project
  `grow-my-money`) is currently up and trading in paper mode against live
  Coinbase market data — the build should not assume a clean/stopped state,
  though rebuilding via `docker compose up -d --build` is expected and fine.
