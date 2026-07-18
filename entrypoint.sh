#!/bin/bash
# Supervise the two container processes: the trading bot and the read-only web
# dashboard. Forward SIGTERM/SIGINT to both children and exit as soon as EITHER
# exits (so a crashed bot doesn't leave a half-alive container). Docker's
# `init: true` (tini) is PID 1 and reaps zombies / forwards signals to this
# script. Requires bash for `wait -n` (dash lacks it) — hence the bash shebang.
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
