"""Regression tests for the horizon-based outcome-tracking pipeline (Option A):
buys stash a pending row, a per-cycle sweep resolves due rows into labeled
`outcomes`, stale rows get dropped, and enough new outcomes trigger a retrain.
Root cause under test: previously nothing ever called `state.record_outcome`,
so the model was permanently stuck cold-start."""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.bot import Bot
from src.indicators import Features
from src.state import iso, utcnow


def _feat(**kw) -> Features:
    base = dict(price=100.0, ema_fast=101.0, ema_slow=100.0, ema_gap_pct=0.01,
                ema_bullish=1, rsi=55.0, macd_line=0.5, macd_signal=0.2,
                macd_hist=0.3, macd_bullish=1, ret_recent=0.02, volatility=0.01)
    base.update(kw)
    return Features(**base)


@pytest.fixture
def bot(config, state, tmp_path):
    return Bot(config, state, coinbase_client=None, state_dir=tmp_path)


# -- state-layer roundtrip ---------------------------------------------------

def test_pending_outcome_roundtrip(state):
    now = utcnow()
    state.record_pending_outcome("BTC-USD", iso(now), 100.0, {"rsi": 55.0},
                                  iso(now + timedelta(hours=6)))
    assert state.due_pending_outcomes(iso(now)) == []
    due = state.due_pending_outcomes(iso(now + timedelta(hours=7)))
    assert len(due) == 1
    assert due[0]["product"] == "BTC-USD"
    state.delete_pending_outcome(due[0]["id"])
    assert state.due_pending_outcomes(iso(now + timedelta(hours=7))) == []


# -- buy hook -----------------------------------------------------------------

def test_buy_records_pending_outcome_due_at_horizon(bot, state, config):
    now = utcnow()
    bot._record_pending_outcome("BTC-USD", _feat(), entry_price=100.0, now=now)
    assert state.due_pending_outcomes(iso(now)) == []
    due = state.due_pending_outcomes(
        iso(now + timedelta(hours=config.model_horizon_hours, minutes=1)))
    assert len(due) == 1
    assert due[0]["entry_price"] == 100.0


def test_record_pending_outcome_failure_does_not_raise(bot, state, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(state, "record_pending_outcome", boom)
    bot._record_pending_outcome("BTC-USD", _feat(), entry_price=100.0)  # must not raise


# -- resolving due rows into labeled outcomes --------------------------------

def test_due_outcome_resolves_win_when_price_clears_roundtrip_fee(bot, state, config):
    now = utcnow()
    entry = now - timedelta(hours=config.model_horizon_hours, minutes=1)
    state.record_pending_outcome("BTC-USD", iso(entry), 100.0, _feat().to_vector(), iso(now))
    roundtrip = 2 * config.fee_bps / 10_000.0
    bot.price_source = lambda product: 100.0 * (1 + roundtrip) * 1.01  # clears fee + margin

    bot._evaluate_due_outcomes(now=now)

    assert state.outcome_count() == 1
    outcome = state.all_outcomes()[0]
    assert outcome["label"] == 1
    assert state.due_pending_outcomes(iso(now)) == []


def test_due_outcome_resolves_loss_when_price_does_not_clear_fee(bot, state, config):
    now = utcnow()
    entry = now - timedelta(hours=config.model_horizon_hours, minutes=1)
    state.record_pending_outcome("BTC-USD", iso(entry), 100.0, _feat().to_vector(), iso(now))
    bot.price_source = lambda product: 100.5  # up, but not enough to clear round-trip fee

    bot._evaluate_due_outcomes(now=now)

    outcome = state.all_outcomes()[0]
    assert outcome["label"] == 0


def test_not_yet_due_outcome_is_left_pending(bot, state, config):
    now = utcnow()
    entry = now - timedelta(hours=1)  # horizon (6h default) hasn't elapsed
    state.record_pending_outcome("BTC-USD", iso(entry), 100.0, _feat().to_vector(),
                                  iso(entry + timedelta(hours=config.model_horizon_hours)))
    bot.price_source = lambda product: 999.0

    bot._evaluate_due_outcomes(now=now)

    assert state.outcome_count() == 0
    assert len(state.due_pending_outcomes(iso(now))) == 0  # not due yet at `now`


# -- stale-row handling (price source failing, e.g. the PEM bug) ------------

def test_stale_due_outcome_dropped_after_grace_window(bot, state):
    now = utcnow()
    long_overdue = now - timedelta(hours=49)  # past the 48h grace window
    state.record_pending_outcome("BTC-USD", iso(long_overdue), 100.0, _feat().to_vector(),
                                  iso(long_overdue))

    def always_fails(product):
        raise RuntimeError("price unavailable")
    bot.price_source = always_fails

    bot._evaluate_due_outcomes(now=now)

    assert state.due_pending_outcomes(iso(now)) == []
    assert state.outcome_count() == 0  # dropped, not labeled


def test_recently_due_outcome_kept_pending_when_price_fails(bot, state):
    now = utcnow()
    recently_due = now - timedelta(hours=1)  # within the 48h grace window
    state.record_pending_outcome("BTC-USD", iso(recently_due), 100.0, _feat().to_vector(),
                                  iso(recently_due))

    def always_fails(product):
        raise RuntimeError("price unavailable")
    bot.price_source = always_fails

    bot._evaluate_due_outcomes(now=now)

    assert len(state.due_pending_outcomes(iso(now))) == 1  # retried next cycle
    assert state.outcome_count() == 0


# -- retrain trigger ----------------------------------------------------------

def test_enough_new_outcomes_triggers_retrain(bot, state, config, monkeypatch):
    calls = []
    monkeypatch.setattr(bot.model, "maybe_retrain", lambda: calls.append(1))

    for i in range(config.retrain_every_n_closed_trades):
        state.record_outcome("BTC-USD", f"2026-01-01T00:{i:02d}:00+00:00",
                              _feat().to_vector(), 1, 0.05)

    bot._maybe_trigger_retrain()

    assert len(calls) == 1
    assert int(state.get_runtime("outcomes_at_last_retrain")) == config.retrain_every_n_closed_trades


def test_retrain_not_triggered_below_threshold(bot, state, config, monkeypatch):
    calls = []
    monkeypatch.setattr(bot.model, "maybe_retrain", lambda: calls.append(1))

    for i in range(config.retrain_every_n_closed_trades - 1):
        state.record_outcome("BTC-USD", f"2026-01-01T00:{i:02d}:00+00:00",
                              _feat().to_vector(), 1, 0.05)

    bot._maybe_trigger_retrain()

    assert calls == []


def test_retrain_trigger_is_incremental_not_repeated(bot, state, config, monkeypatch):
    """Once triggered, the same outcomes must not re-trigger a second retrain."""
    calls = []
    monkeypatch.setattr(bot.model, "maybe_retrain", lambda: calls.append(1))
    for i in range(config.retrain_every_n_closed_trades):
        state.record_outcome("BTC-USD", f"2026-01-01T00:{i:02d}:00+00:00",
                              _feat().to_vector(), 1, 0.05)
    bot._maybe_trigger_retrain()
    assert len(calls) == 1

    bot._maybe_trigger_retrain()  # no new outcomes since
    assert len(calls) == 1
