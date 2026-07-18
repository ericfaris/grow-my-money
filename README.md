# grow-my-money

An autonomous crypto trading bot for **Coinbase Advanced Trade** (BTC-USD,
ETH-USD, SOL-USD). It watches hourly candles, computes EMA/RSI/MACD indicators,
feeds them to a small logistic-regression model, and decides buy/sell/hold — but
**every order passes through a single risk chokepoint** enforcing hard safety
caps and a kill switch.

> **This bot trades real money with no per-trade human approval once live.** The
> safety caps, paper/live separation, and kill switch are correctness
> requirements, not features. It ships in **paper mode by default** and only
> touches real funds after a deliberate, documented `set-mode live`.

## What it does

- **Paper mode (default):** real live prices, *simulated* fills. No real orders.
- **Live mode (opt-in):** real market orders via Coinbase, still behind all caps.
- **Safety caps (single chokepoint, `src/risk.py`):**
  - Kill switch checked first (sentinel file `state/KILL`).
  - **Portfolio-drawdown halt:** if value < 70% of bankroll, reject all buys and
    trip a persistent flag until a human `resume`s.
  - **Per-position cap:** no product may exceed 25% of bankroll — oversized buys
    are *resized* down to headroom, or rejected if headroom < min order.
  - **Rolling 24h trade cap:** at most 5 trades in any trailing 24h window.
  - **Fail-closed:** any un-evaluable input (bad price, DB error) rejects.
- **Buy-and-hold benchmark:** measures skill vs luck. Reports actual return vs an
  equal-weight BTC/ETH/SOL buy-and-hold baseline struck once per epoch, plus the
  delta (the number that means "beating the market").
- **Daily email** to the operator with balance, trades, P&L, caps state, and the
  benchmark comparison.
- **Everything persists** to a bind-mounted SQLite db (`state/grow.db`) — an
  unattended restart resumes killed/halted/paper exactly as it was, and never
  re-strikes the benchmark baseline.

## Layout

```
src/            bot modules (see each file's docstring)
tests/          unit tests (safety caps, killswitch, benchmark, model, ...)
scripts/        backtest.py (scriptable, no live orders)
state/          bind-mounted: grow.db, model.pkl, KILL sentinel (gitignored)
```

## Local setup

```bash
make venv                       # create .venv and install requirements
make test                       # run the full suite
cp .env.example .env            # edit NON-SECRET tunables only
```

Nothing is on PATH — always use `.venv/bin/python`, e.g.:

```bash
.venv/bin/python -m src.cli status
.venv/bin/python -m src.cli once           # one decision cycle
.venv/bin/python -m src.cli report-now --dry-run
```

## Credentials (never in the repo or image)

Two mode-600 files live **outside** the repo (host defaults; the container mounts
them read-only at `/run/secrets/...`):

| Purpose | Host path | Container path |
|---|---|---|
| Coinbase CDP key JSON | `~/.config/coinbase/grow-my-money.key` | `/run/secrets/coinbase.key` |
| SMTP app password | `~/.config/coinbase/grow-my-money.smtp` | `/run/secrets/smtp.env` |

- **Coinbase key:** create a CDP API key in the Coinbase UI with **trade
  permission only — NO withdraw/transfer**. Save the downloaded JSON to the path
  above and `chmod 600` it.
- **SMTP file:** two lines — `SMTP_USER=you@gmail.com` and
  `SMTP_PASS=<Gmail App Password>` — then `chmod 600`.

These are read at runtime; they are **never** copied into the Docker image and
**never** put in `.env` or the compose `environment:` block.

## Docker deploy (lab pattern)

```bash
make up        # docker compose up -d --build, stamps GIT_SHA/BUILD_TIME
make logs
make status
make down
```

No `ports:` are exposed (the bot serves nothing). `restart: unless-stopped`,
non-root uid 1000, `no-new-privileges`. `state/` is bind-mounted so trade
history, mode, halt flag, benchmark anchor, and the KILL sentinel survive
restarts.

## Kill switch

```bash
make stop      # or: .venv/bin/python -m src.cli stop   (creates state/KILL)
touch state/KILL                                        # documented fallback
make resume    # clears KILL *and* the portfolio-halt trip flag (re-arm)
```

A killed bot stays killed across restarts until an explicit `resume`. This is
intentional fail-closed behavior.

## Go-live checklist

1. Run `make test` — all green.
2. Create a **trade-only** Coinbase CDP key (no withdraw) and place it at
   `~/.config/coinbase/grow-my-money.key` (`chmod 600`).
3. Place the SMTP app-password file (`chmod 600`).
4. `make up` in **paper** mode. Watch `make logs` — you should see live prices
   and `mode=paper` simulated trades.
5. Run for **several days in paper**. Review the daily emails, especially whether
   the bot is **beating the buy-and-hold baseline** (the delta). Positive paper
   P&L that *loses* to the baseline is not success.
6. **Decide `HALT_AUTO_FLATTEN`** (see below) and set it in `.env`.
7. Only then: `.venv/bin/python -m src.cli set-mode live`. This requires typing
   the confirmation phrase, verifies the key + a readable balance, records the
   live bankroll, and strikes a **fresh live benchmark anchor**. There is no
   automatic path to live.
8. Watch closely. `make stop` is always one command away.

Going back to paper (`set-mode paper`) is always allowed with no confirmation —
de-risking is easy, arming is hard.

## HALT_AUTO_FLATTEN — a decision you must make before go-live

When the -30% portfolio halt trips, the bot always **stops new buys** (default,
`HALT_AUTO_FLATTEN=false`). Optionally it can also **sell all positions to cash**
(`HALT_AUTO_FLATTEN=true`):

- **Pro (flatten):** caps further loss in a regime the strategy isn't handling;
  selling is a de-risking action.
- **Con (flatten):** auto-selling into a sharp dip can crystallize a loss right
  before a bounce, and it is an irreversible real-money action with no human in
  the loop.

It ships **off by default**. If you enable it, note that flatten sells are an
emergency de-risk: they are **exempt from the 5/24h trade-count cap** but still
honor the **kill switch**. Decide deliberately.

## Backtest (pre-paper sanity check)

```bash
.venv/bin/python scripts/backtest.py path/to/candles.json --product BTC-USD
```

Runs the same indicators/signals path over saved candles (no network, no orders)
and prints strategy vs buy-and-hold P&L.

## Configuration

All tunables are in `.env` (see `.env.example`) — caps, interval, model
thresholds, SMTP host/from. Secrets are **never** here. `MODE` defaults to
`paper`; once you run `set-mode`, the **persisted** mode in SQLite is the source
of truth across restarts (a stale `.env` MODE cannot silently re-arm live).
