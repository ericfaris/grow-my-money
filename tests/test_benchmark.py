"""Acceptance B: buy-and-hold benchmark correct & persistent (success measure)."""
from __future__ import annotations

from src.benchmark import Benchmark
from src.portfolio import Portfolio
from src.state import State
from tests.conftest import FakePriceSource


def test_anchor_units_from_bankroll_and_prices(state, prices):
    b = Benchmark(state)
    anchor = b.ensure_anchor("paper", 9000.0, prices)
    assert anchor["btc_units"] == (9000.0 / 3) / 60000.0
    assert anchor["eth_units"] == (9000.0 / 3) / 3000.0
    assert anchor["sol_units"] == (9000.0 / 3) / 150.0


def test_value_and_return_hand_math(state, prices):
    b = Benchmark(state)
    b.ensure_anchor("paper", 9000.0, prices)
    # unchanged prices -> value == start bankroll, return 0
    assert b.value("paper", prices) == 9000.0
    assert b.return_pct("paper", prices) == 0.0
    # each price up 10% -> whole basket up 10%
    up = {k: v * 1.1 for k, v in prices.items()}
    assert abs(b.value("paper", up) - 9900.0) < 1e-6
    assert abs(b.return_pct("paper", up) - 0.10) < 1e-9


def test_anchor_write_once_across_restart(db_path, prices):
    st = State(db_path)
    Benchmark(st).ensure_anchor("paper", 9000.0, prices)
    orig = st.get_benchmark_anchor("paper")
    st.close()

    # reopen with DIFFERENT current prices and re-run anchor creation
    st2 = State(db_path)
    b2 = Benchmark(st2)
    changed = {"BTC-USD": 100000.0, "ETH-USD": 5000.0, "SOL-USD": 500.0}
    b2.ensure_anchor("paper", 50000.0, changed)  # must NOT re-strike
    after = st2.get_benchmark_anchor("paper")
    assert after["btc_units"] == orig["btc_units"]
    assert after["btc_price"] == orig["btc_price"]
    assert after["start_bankroll"] == orig["start_bankroll"]
    assert after["anchor_ts_utc"] == orig["anchor_ts_utc"]
    st2.close()


def test_paper_and_live_are_distinct_epochs(state, prices):
    b = Benchmark(state)
    b.ensure_anchor("paper", 9000.0, prices)
    live_prices = {"BTC-USD": 70000.0, "ETH-USD": 3500.0, "SOL-USD": 175.0}
    b.ensure_anchor("live", 5000.0, live_prices)
    pa = state.get_benchmark_anchor("paper")
    la = state.get_benchmark_anchor("live")
    assert pa["btc_price"] == 60000.0
    assert la["btc_price"] == 70000.0
    assert la["start_bankroll"] == 5000.0


def test_delta_sign_correct(state, prices):
    b = Benchmark(state)
    b.ensure_anchor("paper", 9000.0, prices)
    portfolio = Portfolio(state, 9000.0)
    # Give the portfolio cash so actual value > baseline (baseline flat at 9000).
    state.set_runtime("paper_cash", 10000.0)  # actual 10000 vs baseline 9000
    ps = FakePriceSource(prices)
    cmp = b.compare("paper", portfolio, prices, ps)
    assert cmp["actual_return_pct"] is not None
    assert cmp["baseline_return_pct"] == 0.0
    assert cmp["delta_pct"] > 0  # beating the hold baseline


def test_missing_anchor_yields_na(state, prices):
    b = Benchmark(state)  # no anchor struck
    portfolio = Portfolio(state, 10000.0)
    ps = FakePriceSource(prices)
    cmp = b.compare("paper", portfolio, prices, ps)
    assert cmp["baseline_value"] is None
    assert cmp["baseline_return_pct"] is None
    assert cmp["delta_pct"] is None  # never raises


def test_missing_price_yields_na(state):
    b = Benchmark(state)
    b.ensure_anchor("paper", 9000.0, {"BTC-USD": 60000.0, "ETH-USD": 3000.0, "SOL-USD": 150.0})
    # now a price is missing
    assert b.value("paper", {"BTC-USD": 60000.0}) is None
    assert b.return_pct("paper", {"BTC-USD": 60000.0}) is None
