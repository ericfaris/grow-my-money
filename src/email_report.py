"""Gmail SMTP daily summary. Mirrors the shape of bookhunt/src/smtp.js:

``is_configured()`` gate + a small transport builder. Credentials (SMTP_USER /
SMTP_PASS) come from the out-of-repo mode-600 file via ``secrets.py`` — never
from the environment/.env. Non-secret settings (host/port/from/recipient) come
from config.

The report body includes balance, the day's trades with the signal/reason that
triggered each, P&L vs bankroll, the actual-vs-hold-baseline comparison (value,
return, and delta), which caps fired, and the current mode.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText

from . import secrets as secrets_mod
from .logging_setup import register_secret
from .risk import HALT_FLAG
from .state import utcnow

log = logging.getLogger(__name__)


def is_configured(config) -> bool:
    """True only if SMTP host/from and the out-of-repo credential file resolve."""
    if not config.smtp_host or not config.smtp_from:
        return False
    try:
        creds = secrets_mod.smtp_credentials(config.smtp_creds_file or None)
    except secrets_mod.SecretError:
        return False
    return bool(creds.get("user") and creds.get("pass"))


def _fmt_pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def _fmt_usd(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def build_report_body(config, state, portfolio, benchmark, price_source, prices) -> str:
    mode = state.get_mode("paper")
    lines: list[str] = []
    lines.append(f"grow-my-money daily report — mode={mode.upper()}")
    lines.append("=" * 52)

    # Balance / portfolio value
    try:
        total = portfolio.total_value(price_source)
        lines.append(f"Portfolio value: {_fmt_usd(total)}  (cash {_fmt_usd(portfolio.cash)})")
    except Exception as exc:  # report must not crash on a price gap
        lines.append(f"Portfolio value: n/a ({exc})")

    lines.append(f"Bankroll (cap base): {_fmt_usd(portfolio.bankroll)}")

    # Benchmark comparison — actual vs hold baseline vs delta
    cmp = benchmark.compare(mode, portfolio, prices, price_source)
    lines.append("")
    lines.append("Actual vs buy-and-hold baseline:")
    lines.append(f"  actual   value {_fmt_usd(cmp['actual_value'])}  return {_fmt_pct(cmp['actual_return_pct'])}")
    lines.append(f"  baseline value {_fmt_usd(cmp['baseline_value'])}  return {_fmt_pct(cmp['baseline_return_pct'])}")
    lines.append(f"  delta (beating market): {_fmt_pct(cmp['delta_pct'])}")

    # Trades in last 24h
    since = utcnow() - timedelta(hours=24)
    trades = state.trades_since(since)
    lines.append("")
    lines.append(f"Trades in last 24h: {len(trades)}")
    for t in trades:
        intent = state.conn.execute(
            "SELECT reason FROM intents WHERE client_order_id=?", (t["client_order_id"],)
        ).fetchone()
        reason = intent["reason"] if intent and intent["reason"] else ""
        lines.append(f"  {t['ts_utc']} {t['side']:4} {t['product']:8} "
                     f"base={t['base_size']:.6f} @ {t['price']:.2f} "
                     f"({_fmt_usd(t['notional'])}) {reason}")

    # Caps state
    lines.append("")
    lines.append("Caps / safety:")
    lines.append(f"  portfolio halt tripped: {state.is_cap_tripped(HALT_FLAG)}")
    lines.append(f"  trades in trailing 24h: {state.trades_in_last_24h()}"
                 f" / {config.risk.max_trades_per_24h}")
    return "\n".join(lines)


def send_report(config, body: str, subject: str = "grow-my-money daily report") -> bool:
    """Send the report via Gmail SMTP (STARTTLS:587). Returns True on send.

    No-ops (returns False) when unconfigured, mirroring smtp.js isConfigured()."""
    if not is_configured(config):
        log.info("SMTP not configured; skipping send (report available via report-now --dry-run)")
        return False
    creds = secrets_mod.smtp_credentials(config.smtp_creds_file or None)
    register_secret(creds["pass"])

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.smtp_from
    msg["To"] = config.report_recipient or config.smtp_from

    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.starttls()
        server.login(creds["user"], creds["pass"])
        server.send_message(msg)
    log.info("Daily report sent to %s", msg["To"])
    return True
