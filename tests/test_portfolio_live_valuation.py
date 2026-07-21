"""Regression: Portfolio.total_value() in live mode used to be
`cash + positions_value` — but `cash` was never updated by a live fill
(execution.py only ever wrote paper_cash `if mode == "paper"`), and
`positions_value` only reflects trades the bot itself made, never
pre-existing account holdings. That meant total_value in live mode was
comparing a fabricated number against the real go_live_bankroll, which could
trip the -30% halt on nothing or mask a real drawdown entirely. Fixed to
query the real Coinbase balance in live mode."""
from __future__ import annotations

import pytest

from src.portfolio import Portfolio


class _FakeClient:
    def __init__(self, balances):
        self.balances = balances
        self.calls = 0

    def get_balances(self):
        self.calls += 1
        return dict(self.balances)


def _price_source(prices):
    def src(product_id):
        return prices[product_id]
    return src


def test_live_total_value_sums_real_balances_not_paper_cash(state):
    state.set_mode("live")
    state.set_runtime("paper_cash", "-999999")  # frozen garbage from a prior paper epoch
    client = _FakeClient({"USD": 10.0, "BTC": 0.01})
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=client)
    total = portfolio.total_value(_price_source({"BTC-USD": 60_000.0}))
    assert total == pytest.approx(10.0 + 0.01 * 60_000.0)


def test_live_total_value_ignores_bot_only_positions_table(state):
    """Even if the bot's own `positions` table has entries (e.g. left over
    from a prior live episode), live total_value must come from the real
    Coinbase balance, not from summing those rows."""
    state.set_mode("live")
    state._apply_fill_to_position("ETH-USD", "buy", base_size=100.0, price=1.0,
                                  ts="2026-01-01T00:00:00+00:00")
    client = _FakeClient({"USD": 50.0})
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=client)
    total = portfolio.total_value(_price_source({}))
    assert total == 50.0  # not 50 + 100 * eth_price


def test_paper_mode_total_value_unaffected(state):
    """Paper mode must keep using cash + positions_value exactly as before —
    this fix is live-mode only."""
    state.set_mode("paper")
    state.set_runtime("paper_cash", "1000")
    client = _FakeClient({"USD": 999999.0})  # must NOT be consulted in paper mode
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=client)
    total = portfolio.total_value(_price_source({}))
    assert total == 1000.0
    assert client.calls == 0


def test_live_total_value_raises_without_client(state):
    state.set_mode("live")
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=None)
    with pytest.raises(RuntimeError):
        portfolio.total_value(_price_source({}))


def test_live_total_value_fails_closed_on_unpriceable_balance(state):
    """An un-priceable held currency must raise (fail-closed), not silently
    skip and understate the total — this is the safety-critical halt-check
    path, unlike the lenient one-time go-live printout in cli.py."""
    state.set_mode("live")
    client = _FakeClient({"USD": 10.0, "WEIRDCOIN": 5.0})
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=client)
    with pytest.raises(KeyError):
        portfolio.total_value(_price_source({}))  # no price for WEIRDCOIN-USD


def test_refresh_live_balances_caches_within_a_cycle(state):
    state.set_mode("live")
    client = _FakeClient({"USD": 10.0})
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=client)
    portfolio.total_value(_price_source({}))
    portfolio.total_value(_price_source({}))
    assert client.calls == 1  # cached, not re-fetched
    portfolio.refresh_live_balances()
    portfolio.total_value(_price_source({}))
    assert client.calls == 2  # cache cleared -> re-fetched


def test_live_cash_reads_real_usd_balance(state):
    state.set_mode("live")
    state.set_runtime("paper_cash", "-999999")  # must NOT be used in live mode
    client = _FakeClient({"USD": 42.0})
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=client)
    assert portfolio.cash == 42.0


def test_live_cash_fails_soft_to_zero_on_error(state):
    class BrokenClient:
        def get_balances(self):
            raise RuntimeError("Coinbase is down")
    state.set_mode("live")
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=BrokenClient())
    assert portfolio.cash == 0.0  # display-only fallback, does not raise


def test_paper_cash_unaffected_by_live_fix(state):
    state.set_mode("paper")
    state.set_runtime("paper_cash", "1234.5")
    portfolio = Portfolio(state, paper_start_bankroll=10_000.0, coinbase_client=None)
    assert portfolio.cash == 1234.5
