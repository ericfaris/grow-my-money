"""Acceptance #6: daily email content includes balance/trades/P&L and the
actual-vs-hold-baseline comparison + delta; no send when unconfigured; creds come
from the out-of-repo file (mocked)."""
from __future__ import annotations

from unittest import mock

from src import email_report
from src.benchmark import Benchmark
from src.config import Config, RiskConfig
from src.portfolio import Portfolio
from tests.conftest import FakePriceSource


def _cfg(**kw):
    base = dict(smtp_host="smtp.gmail.com", smtp_port=587, smtp_from="ericfaris@gmail.com",
                report_recipient="ericfaris@gmail.com", paper_start_bankroll=10_000.0)
    base.update(kw)
    return Config(**base)


def _seed_trade(state):
    state.record_intent("c1", "BTC-USD", "buy", "paper", 500.0)
    state.update_intent_risk("c1", "approve", 500.0, "EMA bullish; p_win=0.61 model=coldstart")
    state.record_fill("c1", "BTC-USD", "buy", "paper", base_size=0.008, price=60000.0,
                      notional=500.0)


def test_body_includes_balance_trades_and_benchmark(state, price_source, prices):
    cfg = _cfg()
    state.set_mode("paper")
    _seed_trade(state)
    portfolio = Portfolio(state, cfg.paper_start_bankroll)
    bench = Benchmark(state)
    bench.ensure_anchor("paper", cfg.paper_start_bankroll, prices)

    body = email_report.build_report_body(cfg, state, portfolio, bench, price_source, prices)
    assert "Portfolio value" in body
    assert "BTC-USD" in body  # the trade line
    assert "buy-and-hold baseline" in body.lower()
    assert "actual" in body.lower()
    assert "baseline" in body.lower()
    assert "delta" in body.lower()
    assert "mode=PAPER" in body


def test_send_noop_when_unconfigured():
    cfg = _cfg(smtp_from="")  # not configured
    with mock.patch("smtplib.SMTP") as smtp:
        sent = email_report.send_report(cfg, "body")
    assert sent is False
    smtp.assert_not_called()


def test_send_uses_mocked_smtp_and_out_of_repo_creds():
    cfg = _cfg()
    fake_creds = {"user": "ericfaris@gmail.com", "pass": "app-password-xyz"}
    with mock.patch.object(email_report.secrets_mod, "smtp_credentials", return_value=fake_creds), \
         mock.patch("smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value.__enter__.return_value
        sent = email_report.send_report(cfg, "hello body", subject="test")
    assert sent is True
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("ericfaris@gmail.com", "app-password-xyz")
    smtp.send_message.assert_called_once()


def test_is_configured_requires_creds():
    cfg = _cfg()
    with mock.patch.object(email_report.secrets_mod, "smtp_credentials",
                           side_effect=email_report.secrets_mod.SecretError("missing")):
        assert email_report.is_configured(cfg) is False
    with mock.patch.object(email_report.secrets_mod, "smtp_credentials",
                           return_value={"user": "u", "pass": "p"}):
        assert email_report.is_configured(cfg) is True
