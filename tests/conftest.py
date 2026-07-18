"""Shared test fixtures. No network, no real credentials."""
from __future__ import annotations

import pytest

from src.config import Config, RiskConfig
from src.killswitch import KillSwitch
from src.portfolio import Portfolio
from src.state import State


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "grow.db"


@pytest.fixture
def state(db_path):
    st = State(db_path)
    yield st
    st.close()


@pytest.fixture
def config():
    return Config(
        mode="paper",
        paper_start_bankroll=10_000.0,
        products=["BTC-USD", "ETH-USD", "SOL-USD"],
        risk=RiskConfig(
            portfolio_halt_fraction=0.70,
            per_position_fraction=0.25,
            max_trades_per_24h=5,
            min_order_usd=10.0,
            halt_auto_flatten=False,
        ),
    )


@pytest.fixture
def killswitch(tmp_path):
    return KillSwitch(tmp_path / "KILL")


@pytest.fixture
def portfolio(state, config):
    return Portfolio(state, config.paper_start_bankroll)


class FakePriceSource:
    """Deterministic price source for tests."""

    def __init__(self, prices: dict[str, float]):
        self.prices = dict(prices)

    def __call__(self, product: str) -> float:
        if product not in self.prices:
            raise RuntimeError(f"no price for {product}")
        return self.prices[product]


@pytest.fixture
def prices():
    return {"BTC-USD": 60_000.0, "ETH-USD": 3_000.0, "SOL-USD": 150.0}


@pytest.fixture
def price_source(prices):
    return FakePriceSource(prices)
