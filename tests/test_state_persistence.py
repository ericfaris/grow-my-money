"""Acceptance #9 (state) + benchmark write-once: state survives reopen; rolling
24h uses a trailing window; benchmark anchor is INSERT-if-absent only."""
from __future__ import annotations

from datetime import timedelta

from src.state import State, iso, utcnow


def test_trades_survive_reopen(db_path):
    st = State(db_path)
    st.set_mode("paper")
    st.record_intent("coid-1", "BTC-USD", "buy", "paper", 100.0)
    st.record_fill("coid-1", "BTC-USD", "buy", "paper", base_size=0.001, price=60000.0,
                   notional=100.0)
    st.close()

    st2 = State(db_path)
    assert st2.get_mode() == "paper"
    assert len(st2.all_trades()) == 1
    assert "BTC-USD" in st2.open_positions()
    st2.close()


def test_rolling_24h_is_trailing_window(db_path):
    st = State(db_path)
    now = utcnow()
    # one fill 25h ago (outside), two within
    for coid, hrs in [("a", 25), ("b", 5), ("c", 1)]:
        st.record_intent(coid, "BTC-USD", "buy", "paper", 10.0, ts_utc=iso(now - timedelta(hours=hrs)))
        st.record_fill(coid, "BTC-USD", "buy", "paper", base_size=0.0001, price=60000.0,
                       notional=10.0, ts_utc=iso(now - timedelta(hours=hrs)))
    assert st.trades_in_last_24h(now=now) == 2  # 25h-old one excluded
    st.close()


def test_position_avg_entry_and_close(db_path):
    st = State(db_path)
    st.record_intent("b1", "ETH-USD", "buy", "paper", 3000.0)
    st.record_fill("b1", "ETH-USD", "buy", "paper", base_size=1.0, price=3000.0, notional=3000.0)
    st.record_intent("b2", "ETH-USD", "buy", "paper", 3200.0)
    st.record_fill("b2", "ETH-USD", "buy", "paper", base_size=1.0, price=3200.0, notional=3200.0)
    pos = st.open_positions()["ETH-USD"]
    assert pos["base_size"] == 2.0
    assert pos["avg_entry"] == 3100.0  # (3000+3200)/2

    # sell everything -> position removed
    st.record_intent("s1", "ETH-USD", "sell", "paper", 6400.0)
    st.record_fill("s1", "ETH-USD", "sell", "paper", base_size=2.0, price=3300.0, notional=6600.0)
    assert "ETH-USD" not in st.open_positions()
    st.close()


def test_benchmark_anchor_insert_if_absent(db_path):
    st = State(db_path)
    a1 = st.create_benchmark_anchor_if_absent("paper", 9000.0, 60000.0, 3000.0, 150.0)
    assert a1["btc_units"] == (9000.0 / 3) / 60000.0
    assert a1["eth_units"] == (9000.0 / 3) / 3000.0
    assert a1["sol_units"] == (9000.0 / 3) / 150.0

    # second call with DIFFERENT prices must be a no-op (write-once)
    a2 = st.create_benchmark_anchor_if_absent("paper", 12000.0, 99999.0, 1.0, 1.0)
    assert a2["start_bankroll"] == 9000.0
    assert a2["btc_price"] == 60000.0
    assert a2["btc_units"] == a1["btc_units"]
    st.close()

    # survives reopen unchanged
    st3 = State(db_path)
    a3 = st3.get_benchmark_anchor("paper")
    assert a3["btc_units"] == a1["btc_units"]
    st3.close()


def test_cap_trip_persists(db_path):
    st = State(db_path)
    st.set_cap_trip("portfolio_halt_tripped", True)
    st.close()
    st2 = State(db_path)
    assert st2.is_cap_tripped("portfolio_halt_tripped") is True
    st2.close()
