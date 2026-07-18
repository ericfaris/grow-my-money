"""Acceptance #5: kill switch — sentinel file blocks orders before placement."""
from __future__ import annotations

from src.killswitch import KillSwitch
from src.risk import RiskManager, TradeIntent


def test_engage_clear_is_engaged(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    assert ks.is_engaged() is False
    ks.engage()
    assert ks.is_engaged() is True
    assert (tmp_path / "KILL").exists()
    ks.clear()
    assert ks.is_engaged() is False


def test_clear_when_absent_is_noop(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    ks.clear()  # should not raise
    assert ks.is_engaged() is False


def test_risk_rejects_first_when_kill_engaged(config, state, portfolio, killswitch, price_source):
    killswitch.engage()
    mgr = RiskManager(config, state, portfolio, killswitch, price_source)
    d = mgr.check(TradeIntent("BTC-USD", "buy", 100.0))
    assert d.action == "reject"
    assert "kill" in d.reason.lower()


def test_kill_persists_as_file(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    ks.engage()
    # a fresh KillSwitch over the same path still sees it (survives restart)
    ks2 = KillSwitch(tmp_path / "KILL")
    assert ks2.is_engaged() is True
