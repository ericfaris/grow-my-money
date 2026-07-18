# Brief: grow-my-money — autonomous Coinbase crypto trading bot

## Problem
Eric wants to experiment with automated crypto trading using his real Coinbase
balance (~$1,100) to see how much a bot-driven strategy can grow it, without
manually watching charts or approving every trade.

## Goal
Build a standalone project, `grow-my-money`, that runs continuously and
autonomously: it watches BTC/ETH/SOL prices via the Coinbase Advanced Trade
API, decides buy/sell/hold using a strategy that combines technical
indicators with an adaptive/ML component that improves as it accumulates
trade-outcome data, and executes trades within hard safety caps — with **no
per-trade human approval**. It must prove itself in a paper-trading
(simulated-fill) mode before ever touching real funds, and once live, must
send a daily email summary and be killable at any time.

This is real money and full autonomy — the plan and build must treat safety
caps, kill-switch reliability, and paper-vs-live mode separation as first-class
requirements, not afterthoughts.

## In scope
- **New Python project** at `~/projects/grow-my-money` (git already
  initialized, currently empty). Rationale: the adaptive/ML component needs
  real ML tooling (scikit-learn/pandas/numpy-class libraries), which Python
  has and Node doesn't — confirmed with the user as an explicit deviation from
  sibling projects' Node convention (this is a different kind of project with
  different needs).
- **Market data + execution** via the Coinbase Advanced Trade API (the current
  Coinbase API — not the deprecated Coinbase Pro API). Use the official/most
  maintained Python SDK/client available for it; if none is well-maintained,
  direct authenticated REST+websocket calls are acceptable — planner's call,
  documented in the plan.
- **Trading pairs:** BTC-USD, ETH-USD, SOL-USD only. No other coins.
- **Strategy — hybrid, and must "get better over time":**
  - A technical-indicator layer (e.g. moving-average crossover, RSI, MACD —
    planner picks specifics) computes signal features per pair.
  - An adaptive/learned layer consumes those features (plus accumulated
    trade-outcome history) to decide buy/sell/hold and position sizing,
    and is periodically retrained/updated using outcomes from prior trades
    (paper and/or live) so its decisions improve as more data accumulates.
    A lightweight, explainable model (e.g. logistic regression / gradient
    boosting on engineered features) is preferred over deep learning — this
    is a hobby-scale bot on ~$1,100, not a hedge fund; keep it inspectable
    and debuggable. Document the exact retraining trigger/cadence chosen.
  - Decision evaluation cadence (how often it checks price and re-decides) is
    not fixed by the user — pick something sensible for a technical-indicator
    strategy on hourly-ish candles (e.g. every 15–60 min) and justify the
    choice in the plan; must be tunable via config, not hardcoded.
- **Hard safety caps (must be enforced in code, checked before every order,
  fail closed if a check can't be evaluated):**
  - **Bankroll:** the bot's tracked/allowed trading capital is the account's
    current balance at go-live (~$1,100 at brief time, but read the *actual*
    balance at go-live time, don't hardcode $1,100).
  - **Portfolio-value halt:** if total tracked portfolio value (cash + open
    positions, mark-to-market) drops below **70% of the bankroll at go-live**
    (i.e. a 30% drawdown), the bot must **immediately stop placing any new
    orders** and alert (see notifications below). It does not need to
    auto-liquidate — halting new trades is the requirement; whether to also
    flatten positions is the planner's call to propose, flagged as a decision
    point for the user in the plan.
  - **Per-position cap:** no single coin's position may exceed **25% of the
    bankroll** at the time of any buy decision — a buy that would exceed this
    must be rejected/resized, not silently allowed.
  - **Daily trade-count cap:** no more than **5 executed trades per rolling
    24h window** — an attempted 6th must be rejected/deferred, not queued to
    fire immediately after the window resets in a burst.
  - All caps must be config values (not magic numbers buried in logic), and
    violating a cap must be logged loudly (not just silently skipped).
- **Paper-trading (simulated) mode:**
  - Runs the full strategy loop against **live real-time prices** but does
    **not** call Coinbase's order-placement endpoints — fills are simulated
    (e.g. assume fill at observed price ± a modeled spread/slippage estimate).
  - Must produce the same daily summary/logging as live mode so the user can
    evaluate performance before switching.
  - **No fixed proving-period length** — the user decides when to flip to
    live based on results they see; the switch must be an explicit, deliberate
    action (e.g. a config flag `MODE=paper|live` or a CLI subcommand), never
    automatic, and must default to `paper` for any fresh/misconfigured start
    (fail closed toward paper, never toward live).
- **Kill switch:** a simple, reliable way to halt the bot immediately —
  e.g. a `stop` CLI command / a sentinel file / a config flag the running
  process polls and honors within one decision cycle. Must work even if the
  bot is mid-analysis (doesn't need to interrupt an in-flight API call, just
  must guarantee no *new* order is placed after the kill signal is set).
  Document exactly how the user invokes it.
- **Daily email summary:** balance, trades made (with reasoning/signal that
  triggered each), current P&L vs. bankroll, and whether any safety cap
  fired — sent once daily to **ericfaris@gmail.com**. Reuse the Gmail SMTP
  pattern from the sibling `bookhunt` project (`~/projects/bookhunt/src/smtp.js`,
  nodemailer-equivalent for Python is fine — e.g. `smtplib` or a small
  library — using Gmail SMTP + an App Password) rather than inventing a new
  notification channel.
- **Credential storage:** Coinbase API keys (create with **trade permission
  only — no withdraw/transfer permission**, if Coinbase's key-creation UI
  allows scoping that granularly) must live **outside the repo and outside
  any Docker-baked `.env`**, analogous to the existing convention documented
  for the Cloudflare token (`~/.config/cloudflare/mooseflip.token`, mode 600).
  Use something like `~/.config/coinbase/grow-my-money.key` (mode 600),
  read at runtime, never committed, never logged. Same treatment for the
  Gmail SMTP App Password used for the daily email.
- **Lab-style deployment**, matching sibling projects' Docker pattern
  (`Dockerfile` + `docker-compose.yml`, image `ericfaris/grow-my-money:latest`,
  `restart: unless-stopped`, run as non-root uid:gid 1000, bind-mounted
  persistent state for trade history/model state) — see
  `~/projects/bookhunt/docker-compose.yml` and `Dockerfile` as the reference
  pattern, adapted (no browser/Xvfb/noVNC needed here — this bot has no UI
  and no browser automation). This project does **not** need Cloudflare Tunnel
  exposure (nothing public-facing to serve) unless the plan identifies a
  reason to add a status page later — out of scope for v1.
- **Persistent state:** trade history, current mode (paper/live), model
  state/weights, and safety-cap trip state must survive container restarts
  (bind-mounted JSON/SQLite/similar — planner's call) — an unattended restart
  must not silently reset the bot into "assume nothing bad has happened."

## Out of scope (v1)
- No web UI/dashboard. CLI + logs + the daily email is sufficient for v1.
- No coins beyond BTC, ETH, SOL.
- No auto-liquidation requirement on halt (see portfolio-value halt above) —
  only "stop placing new orders" is required; auto-flatten is optional/a
  decision point the plan should surface, not silently decide either way.
- No tax-lot accounting / tax reporting features.
- No multi-user support — this is single-account, single-operator (Eric).
- No mobile app / push notifications beyond the one daily email.
- No backtesting UI — a scriptable/CLI backtest against historical data is
  encouraged (useful for validating the strategy before paper mode even
  starts) but not a polished feature.
- No Cloudflare Tunnel / public exposure in v1.

## Constraints
- Real money, real exchange API, full autonomy — treat every safety
  requirement above as mandatory, not best-effort.
- Coinbase Advanced Trade API rate limits apply — don't poll aggressively;
  respect documented limits.
- This machine: Python 3.12.3 at `/usr/bin/python3`, pip 26.0.1 available.
  Docker 29.3.0 available. Use a venv (`python3 -m venv .venv`) — do not
  assume global pip installs are acceptable.
- No existing code in this repo to match style-wise (it's brand new) — for
  Docker/deploy/credential-storage/notification conventions, follow the
  sibling `bookhunt` project's patterns (paths referenced above) as the house
  style, adapted for Python instead of Node where relevant.

## Acceptance criteria
1. Running the bot in paper mode (default/fresh config) makes decisions on a
   configurable interval using live Coinbase prices for BTC/ETH/SOL, logs a
   simulated trade when its strategy signals one, and never calls a
   real-money order-placement endpoint.
2. The per-position cap (25% of bankroll) is provably enforced: a scenario
   where the strategy would want to put >25% of bankroll into one coin
   results in the order being resized or rejected, not placed as requested —
   demonstrable via a unit/integration test with a mocked large signal.
3. The daily trade cap (5/24h) is provably enforced: a 6th signal within the
   same rolling 24h window is rejected/deferred, not executed —
   demonstrable via test.
4. The portfolio-value halt (-30%) is provably enforced: given a simulated
   portfolio state below 70% of bankroll, the bot refuses to place any new
   order and this is logged/alertable — demonstrable via test.
5. The kill switch reliably prevents new orders once triggered — demonstrable
   by triggering it and confirming the next decision cycle places no order
   (paper or live).
6. A daily email actually sends (in paper mode, using the user's real Gmail
   SMTP credentials once configured) with balance/trades/P&L content, using
   credentials read from the out-of-repo path, never from a committed file.
7. Switching from paper to live mode requires an explicit, documented action
   (not automatic) and defaults to paper on a fresh/misconfigured start.
8. Coinbase API credentials are never present in any file tracked by git or
   baked into the Docker image — verified by inspecting the built image layers
   / repo contents.
9. The project runs via the Docker lab pattern (`docker compose up -d --build`
   equivalent) and stays running (`restart: unless-stopped`), with trade
   history and mode state surviving a container restart.

## Open questions & decisions made
- **Language/stack:** Python — user's explicit choice, given the ML/adaptive
  requirement, overriding the Node convention used by sibling lab projects.
- **Plan review:** user wants to review the Opus plan before build starts
  (unlike the last bookhunt feature, where they skipped review) — given real
  money + full autonomy, this is the higher-stakes default.
- **Paper-trading duration:** no fixed period — user decides when to go live
  based on observed results; the plan must make the paper→live switch a
  deliberate, explicit, documented action.
- **Strategy type:** explicit user request for "a combination of both" —
  technical indicators AND an adaptive/ML piece that improves over time.
  Exact indicators/model choice is left to the Opus planner's judgment,
  documented with rationale; keep it explainable/debuggable over exotic.
- **Auto-liquidation on halt:** not decided — plan should propose an approach
  (e.g. "halt only" vs "halt + optionally flatten") and flag it as a
  user-facing decision point rather than silently picking one, since it has
  real financial consequences either way.
- **Coinbase API key scope:** user should create a **trade-only, no-withdraw**
  API key in Coinbase's own UI — this is a manual prerequisite for the user,
  not something the plan/build can automate (Claude should never receive or
  handle the actual key value in a way that gets logged/committed).

## Relevant files/areas (reference patterns from sibling project)
- `~/projects/bookhunt/docker-compose.yml` — Docker lab pattern: `name:`,
  single `app` service, `image: ericfaris/<project>:latest`,
  `restart: unless-stopped`, `user: "1000:1000"`,
  `security_opt: [no-new-privileges:true]`, `ports: ["127.0.0.1:PORT:PORT"]`,
  bind-mounted state files, `environment:` block reading from `.env` for
  *non-sensitive-enough-to-need-out-of-repo* config (note: Coinbase keys and
  SMTP pass here should NOT follow this env-passthrough pattern — they go in
  the out-of-repo file per the brief above; document clearly which secrets
  use which storage method and why).
- `~/projects/bookhunt/Dockerfile` — base image pattern (`FROM node:22-slim`
  → for this project use an appropriate Python base, e.g.
  `python:3.12-slim`), non-root uid 1000 conventions.
- `~/projects/bookhunt/src/smtp.js` — Gmail SMTP transport pattern
  (`SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, App Password auth,
  `isConfigured()` guard) — replicate the *shape* of this in Python.
- `~/.config/cloudflare/mooseflip.token` — reference example of the
  out-of-repo, mode-600, read-at-runtime credential storage convention to
  replicate for Coinbase keys and the SMTP password.
- This is a brand-new empty repo (git initialized, no commits yet) at
  `~/projects/grow-my-money` — there is no existing code to preserve or
  avoid breaking.

## Repo commands & tree state
- **Repo:** `~/projects/grow-my-money` — freshly `git init`'d, empty, no
  commits, no pre-existing files. Nothing to account for from prior work.
- **Python:** `/usr/bin/python3` (3.12.3), `pip` 26.0.1 available. Plan should
  specify creating `.venv` and a `requirements.txt`/`pyproject.toml`, and
  give exact invocation commands (e.g. `python3 -m venv .venv &&
  .venv/bin/pip install -r requirements.txt`, `.venv/bin/python -m pytest`,
  `.venv/bin/python -m src.bot` or similar — planner picks the exact entry
  point and records it precisely, since the build/verify phases must use the
  exact commands, not assume `python`/`pytest` are on `PATH`).
- **Docker:** Docker 29.3.0 available; `docker compose` (v2, no hyphen) is the
  CLI form used elsewhere in this environment (see bookhunt's `docker:up`
  npm script) — mirror `docker compose up -d --build` for this project too,
  ideally via an equivalent `make`/script target since there's no
  `package.json` here (Python project) — planner's call on the exact
  tooling (Makefile vs shell script), but record whichever is chosen exactly.
