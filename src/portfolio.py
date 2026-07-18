"""Cash + positions, mark-to-market valuation, bankroll tracking.

Positions and cash are derived from persisted state (SQLite). ``total_value`` and
``position_value`` mark to market using a spot-price callable so the same price
source feeds both the portfolio and the benchmark.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

PriceSource = Callable[[str], float]


class Portfolio:
    def __init__(self, state, paper_start_bankroll: float):
        self.state = state
        self.paper_start_bankroll = paper_start_bankroll

    # -- cash --------------------------------------------------------------
    @property
    def cash(self) -> float:
        """Simulated USD cash (paper). Persisted in runtime kv; seeded to the
        paper starting bankroll on first read."""
        raw = self.state.get_runtime("paper_cash")
        if raw is None:
            self.state.set_runtime("paper_cash", self.paper_start_bankroll)
            return self.paper_start_bankroll
        return float(raw)

    def set_cash(self, value: float) -> None:
        self.state.set_runtime("paper_cash", value)

    # -- bankroll ----------------------------------------------------------
    @property
    def bankroll(self) -> float:
        """The capital base the caps are measured against.

        In live mode this is ``go_live_bankroll`` (recorded at go-live). In paper
        mode it is the paper starting bankroll. Falls back to paper start."""
        mode = self.state.get_mode("paper")
        if mode == "live":
            raw = self.state.get_runtime("go_live_bankroll")
            if raw is not None:
                return float(raw)
        return self.paper_start_bankroll

    @property
    def go_live_bankroll(self) -> Optional[float]:
        raw = self.state.get_runtime("go_live_bankroll")
        return float(raw) if raw is not None else None

    # -- positions ---------------------------------------------------------
    def position_value(self, product: str, price_source: PriceSource) -> float:
        pos = self.state.open_positions().get(product)
        if not pos:
            return 0.0
        return pos["base_size"] * price_source(product)

    def positions_value(self, price_source: PriceSource) -> float:
        total = 0.0
        for product in self.state.open_positions():
            total += self.position_value(product, price_source)
        return total

    def total_value(self, price_source: PriceSource) -> float:
        """Cash + mark-to-market value of all open positions.

        Raises whatever ``price_source`` raises — the caller (risk) treats an
        un-markable portfolio as fail-closed."""
        return self.cash + self.positions_value(price_source)
