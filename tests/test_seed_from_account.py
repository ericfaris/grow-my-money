"""Regression: a fresh paper epoch should be able to start from the account's
real (non-staked) holdings instead of undifferentiated cash, so paper trading
reflects what a real go-live would actually inherit day one."""
from __future__ import annotations

import pytest

from src.cli import _seed_paper_from_balances
from src.state import iso, utcnow


def _price_source(prices):
    def src(product_id):
        if product_id not in prices:
            raise RuntimeError(f"no price for {product_id}")
        return prices[product_id]
    return src


# -- State.seed_position -------------------------------------------------

def test_seed_position_creates_a_position(state):
    state.seed_position("XRP-USD", 100.0, 1.15, iso(utcnow()))
    positions = state.open_positions()
    assert positions["XRP-USD"]["base_size"] == 100.0
    assert positions["XRP-USD"]["avg_entry"] == 1.15


def test_seed_position_refuses_to_overwrite_existing(state):
    state.seed_position("XRP-USD", 100.0, 1.15, iso(utcnow()))
    with pytest.raises(ValueError):
        state.seed_position("XRP-USD", 50.0, 2.0, iso(utcnow()))
    # untouched by the failed second call
    assert state.open_positions()["XRP-USD"]["base_size"] == 100.0


def test_seed_position_writes_no_trade_or_intent_rows(state):
    """Seeding is an initial condition, not a bot decision — it must never
    pollute the trade-count cap or outcome-tracking pipeline."""
    state.seed_position("XRP-USD", 100.0, 1.15, iso(utcnow()))
    assert state.all_trades() == []
    assert state.trades_in_last_24h() == 0


# -- _seed_paper_from_balances --------------------------------------------

def test_seeds_non_usd_balances_as_positions(state):
    balances = {"USD": 0.72, "BTC": 0.001, "XRP": 100.0}
    prices = {"BTC-USD": 60_000.0, "XRP-USD": 1.15}
    result = _seed_paper_from_balances(state, balances, _price_source(prices), iso(utcnow()))
    positions = state.open_positions()
    assert positions["BTC-USD"]["base_size"] == 0.001
    assert positions["BTC-USD"]["avg_entry"] == 60_000.0
    assert positions["XRP-USD"]["base_size"] == 100.0
    assert result["cash"] == 0.72


def test_usd_goes_to_paper_cash_not_a_position(state):
    balances = {"USD": 42.0}
    _seed_paper_from_balances(state, balances, _price_source({}), iso(utcnow()))
    assert "USD-USD" not in state.open_positions()
    assert float(state.get_runtime("paper_cash")) == 42.0


def test_zero_and_negative_balances_skipped(state):
    balances = {"USD": 10.0, "DOGE": 0.0, "SHIB": -1.0}
    _seed_paper_from_balances(state, balances, _price_source({}), iso(utcnow()))
    assert state.open_positions() == {}


def test_unpriceable_currency_excluded_not_fatal(state):
    balances = {"USD": 10.0, "BTC": 1.0, "WEIRDCOIN": 5.0}
    prices = {"BTC-USD": 60_000.0}
    warnings = []
    result = _seed_paper_from_balances(state, balances, _price_source(prices), iso(utcnow()),
                                       warn=warnings.append)
    assert "BTC-USD" in state.open_positions()
    assert "WEIRDCOIN-USD" not in state.open_positions()
    assert len(warnings) == 1 and "WEIRDCOIN" in warnings[0]
    assert result["seeded"][0]["value"] == 60_000.0


def test_total_seeded_value_matches_bankroll_style_sum(state):
    """cash + sum(seeded position values) should equal what
    _total_account_value_usd (cli.py) would compute for the same balances —
    seeding must not change the total, only how it's allocated."""
    balances = {"USD": 0.72, "BTC": 0.00248627, "XRP": 462.445611}
    prices = {"BTC-USD": 66610.695, "XRP-USD": 1.13995}
    result = _seed_paper_from_balances(state, balances, _price_source(prices), iso(utcnow()))
    total = result["cash"] + sum(r["value"] for r in result["seeded"])
    assert total == pytest.approx(0.72 + 165.61 + 527.16, abs=0.5)
