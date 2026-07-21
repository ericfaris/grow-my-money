"""Operator CLI: run, once, stop, resume, status, set-mode, report-now, backtest.

Switching to live is the ONLY place mode becomes 'live', and it requires an
interactive typed confirmation phrase plus a readable Coinbase balance; it also
records the live benchmark anchor and go_live_bankroll. There is no automatic
path to live anywhere else in the tree.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .benchmark import Benchmark
from .config import load_config
from .killswitch import KillSwitch
from .logging_setup import setup_logging
from .portfolio import Portfolio
from .risk import HALT_FLAG
from .state import State, iso, utcnow

log = logging.getLogger(__name__)

CONFIRM_PHRASE = "GO LIVE WITH REAL MONEY"
STATE_DIR = "state"
DB_PATH = f"{STATE_DIR}/grow.db"
KILL_PATH = f"{STATE_DIR}/KILL"
BENCH_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")


def _open_state() -> State:
    return State(DB_PATH)


def _total_account_value_usd(balances: dict, price_source, warn=print) -> float:
    """Sum USD cash + every other nonzero currency priced at spot. A currency
    that can't be priced is excluded (warned, not fatal) rather than aborting
    the whole go-live. Does NOT see staked balances — Coinbase's
    available_balance excludes them entirely; see the caller's note."""
    total = float(balances.get("USD", 0.0))
    for cur, amount in balances.items():
        if cur == "USD" or amount <= 0:
            continue
        try:
            price = price_source(f"{cur}-USD")
        except Exception as exc:  # noqa: BLE001
            warn(f"Warning: could not price {cur} ({exc}); excluded from bankroll.")
            continue
        total += amount * price
    return total


def _seed_paper_from_balances(state, balances: dict, price_source, now_iso: str,
                              warn=print) -> dict:
    """Seed paper positions from real (non-staked) account balances, and
    paper_cash from the real USD balance — mirrors what a live go-live would
    actually inherit on day one, instead of starting from undifferentiated
    cash. Deliberately NOT a fill: writes no trades/intents row (see
    State.seed_position). A currency that can't be priced is excluded
    (warned, not fatal). Returns a summary dict for the caller to print."""
    seeded = []
    for cur, amount in balances.items():
        if cur == "USD" or amount <= 0:
            continue
        product = f"{cur}-USD"
        try:
            price = price_source(product)
        except Exception as exc:  # noqa: BLE001
            warn(f"Warning: could not price {cur} ({exc}); excluded from seed.")
            continue
        state.seed_position(product, amount, price, now_iso)
        seeded.append({"product": product, "base_size": amount, "avg_entry": price,
                       "value": amount * price})
    usd_cash = float(balances.get("USD", 0.0))
    state.set_runtime("paper_cash", usd_cash)
    return {"seeded": seeded, "cash": usd_cash}


def cmd_seed_from_account(args) -> int:
    state = _open_state()
    if state.get_mode("paper") != "paper":
        print("Refusing to seed: mode is not paper.")
        return 1
    if state.open_positions() or state.all_trades():
        print("Refusing to seed: this db already has positions or trade history "
              "(seed-from-account is for a fresh epoch only — wipe state first).")
        return 1

    cfg = load_config()
    try:
        from . import secrets as secrets_mod
        from .coinbase_client import CoinbaseClient
        key_path = secrets_mod.coinbase_key_path(cfg.coinbase_key_file or None)
        client = CoinbaseClient.from_key_file(key_path)
        balances = client.get_balances()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot seed: Coinbase key/balance check failed ({exc}).")
        return 1

    result = _seed_paper_from_balances(state, balances, client.get_spot_price, iso(utcnow()))
    print("Seeded paper positions from real account balances "
          "(entry price = spot at seed time — no real cost-basis data available; "
          "NOTE: staked holdings are invisible to this API and are NOT seeded):")
    total = result["cash"]
    for row in result["seeded"]:
        print(f"  {row['product']:10} base={row['base_size']:.6f}  "
              f"entry=${row['avg_entry']:,.4f}  value=${row['value']:,.2f}")
        total += row["value"]
    print(f"  {'cash':10} ${result['cash']:,.2f}")
    print(f"  {'TOTAL':10} ${total:,.2f}")
    return 0


def cmd_run(args) -> int:
    from .bot import build_bot
    bot = build_bot(with_client=True)
    bot.run(once=False)
    return 0


def cmd_once(args) -> int:
    from .bot import build_bot
    bot = build_bot(with_client=True)
    bot.rehydrate()
    summary = bot.run_cycle()
    print(summary)
    return 0


def cmd_stop(args) -> int:
    KillSwitch(KILL_PATH).engage()
    print("Kill switch ENGAGED. No new orders will be placed until 'resume'.")
    return 0


def cmd_resume(args) -> int:
    KillSwitch(KILL_PATH).clear()
    state = _open_state()
    state.set_cap_trip(HALT_FLAG, False)
    state.set_runtime(f"{HALT_FLAG}_ts", None)
    print("Kill switch cleared and portfolio-halt trip flag reset. Bot re-armed.")
    return 0


def cmd_set_mode(args) -> int:
    state = _open_state()
    target = args.mode
    if target == "paper":
        state.set_mode("paper")
        print("Mode set to PAPER (de-risking is always allowed, no confirmation).")
        return 0

    # LIVE — deliberate, guarded.
    print("\n*** SWITCHING TO LIVE — REAL MONEY WILL BE TRADED AUTONOMOUSLY ***")
    print(f"Type exactly:  {CONFIRM_PHRASE}")
    try:
        typed = input("> ").strip()
    except EOFError:
        typed = ""
    if typed != CONFIRM_PHRASE:
        print("Confirmation phrase did not match. Mode NOT changed (still "
              f"{state.get_mode('paper')}).")
        return 1

    # verify key present and balance readable
    cfg = load_config()
    try:
        from . import secrets as secrets_mod
        from .coinbase_client import CoinbaseClient
        key_path = secrets_mod.coinbase_key_path(cfg.coinbase_key_file or None)
        client = CoinbaseClient.from_key_file(key_path)
        balances = client.get_balances()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot go live: Coinbase key/balance check failed ({exc}).")
        return 1

    prices = {}
    for p in BENCH_PRODUCTS:
        try:
            prices[p] = client.get_spot_price(p)
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot go live: price fetch failed for {p} ({exc}).")
            return 1

    # Baseline capital = total account value, not just the USD cash line — a
    # balance sitting in BTC/ETH/etc. (as opposed to USD) is still bankroll.
    # NOTE: this only sees what Coinbase's `available_balance` reports, which
    # excludes STAKED holdings (e.g. staked ETH/DOT/ADA) entirely — the
    # Advanced Trade API does not expose staked balances. If you hold staked
    # assets, verify the true total manually (Coinbase app) before going live;
    # this number will undercount.
    def _price_source(product_id):
        return prices.get(product_id) or client.get_spot_price(product_id)

    go_live_bankroll = _total_account_value_usd(balances, _price_source)
    now = iso(utcnow())
    state.set_mode("live")
    state.set_runtime("go_live_bankroll", go_live_bankroll)
    state.set_runtime("go_live_ts", now)
    # record the LIVE benchmark anchor (distinct epoch from paper)
    Benchmark(state).ensure_anchor("live", go_live_bankroll, prices, anchor_ts_utc=now)
    log.warning("MODE SWITCHED TO LIVE. go_live_bankroll=%.2f at %s", go_live_bankroll, now)
    print(f"\nMODE = LIVE. Baseline capital (go_live_bankroll) = ${go_live_bankroll:,.2f}.")
    print("Live buy-and-hold benchmark anchor recorded. Bot will trade real money.")
    return 0


def _price_source_from_client(client):
    def src(product):
        return client.get_spot_price(product)
    return src


def _print_status(state, cfg, client=None) -> None:
    mode = state.get_mode("paper")
    portfolio = Portfolio(state, cfg.paper_start_bankroll, coinbase_client=client)
    benchmark = Benchmark(state)
    print(f"mode: {mode}")
    print(f"kill switch: {'ENGAGED' if KillSwitch(KILL_PATH).is_engaged() else 'clear'}")
    print(f"portfolio halt tripped: {state.is_cap_tripped(HALT_FLAG)}")
    print(f"trades in trailing 24h: {state.trades_in_last_24h()} / "
          f"{cfg.risk.max_trades_per_24h}")
    print("open positions:")
    for prod, pos in state.open_positions().items():
        print(f"  {prod}: base={pos['base_size']:.6f} avg_entry={pos['avg_entry']:.2f}")

    if client is not None:
        prices = {}
        for p in BENCH_PRODUCTS:
            try:
                prices[p] = client.get_spot_price(p)
            except Exception:
                pass
        src = _price_source_from_client(client)
        cmp = benchmark.compare(mode, portfolio, prices, src)

        def pct(v):
            return "n/a" if v is None else f"{v * 100:+.2f}%"

        def usd(v):
            return "n/a" if v is None else f"${v:,.2f}"

        print("\nActual vs buy-and-hold baseline:")
        print(f"  actual   value {usd(cmp['actual_value'])}  return {pct(cmp['actual_return_pct'])}")
        print(f"  baseline value {usd(cmp['baseline_value'])}  return {pct(cmp['baseline_return_pct'])}")
        print(f"  delta:   {pct(cmp['delta_pct'])}")
    else:
        print("\n(benchmark comparison needs live prices — run with a client to see actual vs baseline)")


def cmd_status(args) -> int:
    state = _open_state()
    cfg = load_config()
    client = None
    try:
        from . import secrets as secrets_mod
        from .coinbase_client import CoinbaseClient
        key_path = secrets_mod.coinbase_key_path(cfg.coinbase_key_file or None)
        client = CoinbaseClient.from_key_file(key_path)
    except Exception:
        client = None
    _print_status(state, cfg, client=client)
    return 0


def cmd_report_now(args) -> int:
    from . import email_report
    from .bot import build_bot
    cfg = load_config()
    try:
        bot = build_bot(cfg, with_client=True)
    except Exception as exc:  # noqa: BLE001 — allow dry-run without a client
        if not args.dry_run:
            print(f"Cannot build report (no client): {exc}")
            return 1
        bot = None

    if bot is not None:
        prices = bot.refresh_prices()
        body = email_report.build_report_body(cfg, bot.state, bot.portfolio,
                                              bot.benchmark, bot.price_source, prices)
    else:
        state = _open_state()
        portfolio = Portfolio(state, cfg.paper_start_bankroll)
        body = email_report.build_report_body(cfg, state, portfolio, Benchmark(state),
                                              lambda p: 0.0, {})
    if args.dry_run:
        print(body)
        return 0
    sent = email_report.send_report(cfg, body)
    print("sent" if sent else "not sent (SMTP not configured)")
    return 0


def cmd_backtest(args) -> int:
    from scripts import backtest as bt
    return bt.run(args.fixture)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grow-my-money", description="autonomous crypto bot")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the decision loop forever").set_defaults(func=cmd_run)
    sub.add_parser("once", help="run a single decision cycle").set_defaults(func=cmd_once)
    sub.add_parser("stop", help="engage the kill switch").set_defaults(func=cmd_stop)
    sub.add_parser("resume", help="clear kill switch + halt trip").set_defaults(func=cmd_resume)
    sub.add_parser("status", help="print mode, caps, positions, benchmark").set_defaults(func=cmd_status)
    sub.add_parser("seed-from-account",
                   help="seed a fresh paper epoch with real (non-staked) account holdings"
                   ).set_defaults(func=cmd_seed_from_account)

    sm = sub.add_parser("set-mode", help="switch paper/live (live needs confirmation)")
    sm.add_argument("mode", choices=["paper", "live"])
    sm.set_defaults(func=cmd_set_mode)

    rn = sub.add_parser("report-now", help="build/send the daily report")
    rn.add_argument("--dry-run", action="store_true", help="print body, do not send")
    rn.set_defaults(func=cmd_report_now)

    bt = sub.add_parser("backtest", help="run a backtest over a candle fixture")
    bt.add_argument("fixture", help="path to a candle CSV/JSON fixture")
    bt.set_defaults(func=cmd_backtest)
    return p


def main(argv=None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
