"""Read-only dashboard data-layer tests. No network, no real credentials."""
from __future__ import annotations

import sqlite3

import pytest

from src.config import Config, RiskConfig
from src.dashboard_data import (
    PriceProvider,
    ReadOnlyState,
    build_activity,
    build_payload,
    reconstruct_equity,
)
from src.risk import HALT_FLAG
from src.state import State, iso, utcnow


@pytest.fixture
def cfg():
    return Config(
        paper_start_bankroll=10_000.0,
        products=["BTC-USD", "ETH-USD", "SOL-USD"],
        risk=RiskConfig(max_trades_per_24h=5),
    )


def _seed(db_path):
    """Populate a real DB via State, then close so a ro reader can attach."""
    st = State(db_path)
    st.set_mode("paper")
    st.set_runtime("paper_cash", 8_000.0)
    # a filled buy
    st.record_intent("coid-buy-1", "BTC-USD", "buy", "paper", 2_000.0)
    st.update_intent_risk("coid-buy-1", "approve", 2_000.0, "ema_cross; p_win=0.61")
    st.record_fill("coid-buy-1", "BTC-USD", "buy", "paper", 0.02, 59_000.0, 1_180.0, 7.08)
    # a blocked/rejected intent (never filled) — must remain visible
    st.record_intent("coid-rej-1", "ETH-USD", "buy", "paper", 1_000.0)
    st.update_intent_risk("coid-rej-1", "reject", 0.0, "daily trade cap 5/24h reached")
    st.record_model_meta(iso(utcnow()), 120, 0.62, True)
    st.close()


# -- ReadOnlyState -----------------------------------------------------------
def test_readonly_reads_match(db_path):
    _seed(db_path)
    ro = ReadOnlyState(db_path)
    try:
        assert ro.get_mode() == "paper"
        assert ro.get_runtime("paper_cash") == "8000.0"
        positions = ro.open_positions()
        assert "BTC-USD" in positions
        assert positions["BTC-USD"]["base_size"] == pytest.approx(0.02)
        assert ro.trades_in_last_24h() == 1
        m = ro.latest_model_meta()
        assert m["n_samples"] == 120
        assert ro.last_trade_price("BTC-USD") == pytest.approx(59_000.0)
    finally:
        ro.close()


def test_readonly_rejects_writes(db_path):
    _seed(db_path)
    ro = ReadOnlyState(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute("INSERT INTO runtime(key,value) VALUES('x','y')")
    finally:
        ro.close()


def test_readonly_write_methods_are_noops(db_path):
    _seed(db_path)
    ro = ReadOnlyState(db_path)
    try:
        # must not raise (Portfolio.cash relies on this) and must not persist
        ro.set_runtime("paper_cash", 123.0)
        ro.set_mode("live")
        ro.set_cap_trip(HALT_FLAG, True)
        assert ro.get_runtime("paper_cash") == "8000.0"
        assert ro.get_mode() == "paper"
        assert ro.is_cap_tripped(HALT_FLAG) is False
    finally:
        ro.close()


# -- PriceProvider -----------------------------------------------------------
class _FakeClient:
    def __init__(self, prices, fail=False):
        self.prices = prices
        self.fail = fail
        self.calls = 0

    def get_spot_price(self, product):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return self.prices[product]


def _provider_with_client(cfg, client):
    pp = PriceProvider(cfg, ttl_sec=100)
    pp._client = client
    pp._client_tried = True
    return pp


def test_price_cache_within_ttl(cfg):
    client = _FakeClient({"BTC-USD": 60_000.0})
    pp = _provider_with_client(cfg, client)
    price, source = pp.get("BTC-USD")
    assert price == 60_000.0 and source == "live"
    price2, _ = pp.get("BTC-USD")
    assert price2 == 60_000.0
    assert client.calls == 1  # served from cache the second time


def test_price_fallback_to_last_trade_then_entry(db_path, cfg):
    _seed(db_path)
    ro = ReadOnlyState(db_path)
    try:
        client = _FakeClient({}, fail=True)
        pp = _provider_with_client(cfg, client)
        # BTC-USD has a recorded trade -> last_trade fallback
        price, source = pp.get("BTC-USD", ro)
        assert price == pytest.approx(59_000.0) and source == "last_trade"
        # SOL-USD has neither trade nor position -> zero/entry, never raises
        price2, source2 = pp.get("SOL-USD", ro)
        assert price2 == 0.0 and source2 == "entry"
    finally:
        ro.close()


# -- reconstruct_equity ------------------------------------------------------
def test_reconstruct_equity_empty():
    pts = reconstruct_equity([], 10_000.0, {}, current_total=10_500.0)
    assert len(pts) == 2
    assert pts[0]["value"] == 10_000.0
    assert pts[-1]["value"] == 10_500.0


def test_reconstruct_equity_buy_then_sell():
    trades = [
        {"ts_utc": "2026-07-10T00:00:00+00:00", "side": "buy", "product": "BTC-USD",
         "base_size": 0.02, "price": 59_000.0, "notional": 1_180.0, "fee": 7.08},
        {"ts_utc": "2026-07-11T00:00:00+00:00", "side": "sell", "product": "BTC-USD",
         "base_size": 0.02, "price": 61_000.0, "notional": 1_220.0, "fee": 7.32},
    ]
    pts = reconstruct_equity(trades, 10_000.0, {"BTC-USD": 61_000.0})
    tss = [p["ts"] for p in pts]
    assert tss == sorted(tss)  # monotonic timestamps
    # after a full round-trip all is cash: 10000 - 1180 - 7.08 + 1220 - 7.32
    assert pts[-1]["value"] == pytest.approx(10_025.60, abs=1e-6)


# -- build_activity & build_payload -----------------------------------------
def test_activity_keeps_blocked_intent_and_dedups_filled(db_path):
    _seed(db_path)
    ro = ReadOnlyState(db_path)
    try:
        activity = build_activity(ro)
        trades = [a for a in activity if a["kind"] == "trade"]
        intents = [a for a in activity if a["kind"] == "intent"]
        assert len(trades) == 1  # the filled buy shows as a trade
        # the filled intent is de-duped; the rejected one is kept with its reason
        rej = [i for i in intents if i["product"] == "ETH-USD"]
        assert len(rej) == 1
        assert rej[0]["risk_action"] == "reject"
        assert "daily trade cap" in rej[0]["reason"]
        assert not any(i["product"] == "BTC-USD" for i in intents)
    finally:
        ro.close()


def test_build_payload_shape(db_path, cfg):
    _seed(db_path)
    ro = ReadOnlyState(db_path)
    try:
        client = _FakeClient({"BTC-USD": 61_000.0, "ETH-USD": 3_000.0, "SOL-USD": 150.0})
        pp = _provider_with_client(cfg, client)
        payload = build_payload(cfg, ro, pp)
        for key in ("generated_at", "mode", "status", "prices", "portfolio",
                    "benchmark", "equity", "activity", "model", "refresh_sec"):
            assert key in payload
        p = payload["portfolio"]
        assert p["total_value"] == pytest.approx(p["cash"] + p["positions_value"])
        assert p["total_fees_paid"] == pytest.approx(7.08)
        assert payload["status"]["trades_24h"] == 1
        assert payload["model"]["n_samples"] == 120
        # blocked intent visible in activity with its reason
        assert any(a["kind"] == "intent" and "daily trade cap" in (a["reason"] or "")
                   for a in payload["activity"])
    finally:
        ro.close()
