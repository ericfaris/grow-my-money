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
    def __init__(self, state, paper_start_bankroll: float, coinbase_client=None):
        self.state = state
        self.paper_start_bankroll = paper_start_bankroll
        self.client = coinbase_client
        self._live_balances_cache: Optional[dict] = None

    def refresh_live_balances(self) -> None:
        """Clear the per-cycle live-balance cache. Call once at cycle top
        (mirrors Bot.refresh_prices) so repeated halt/cap checks within the
        same cycle don't each hit the Coinbase balances endpoint."""
        self._live_balances_cache = None

    def _live_balances(self) -> dict:
        if self._live_balances_cache is None:
            self._live_balances_cache = self.client.get_balances()
        return self._live_balances_cache

    # -- cash --------------------------------------------------------------
    @property
    def cash(self) -> float:
        """USD cash. In live mode, the real Coinbase USD balance (display-only
        — falls back to 0.0 on a fetch error rather than raising, since this
        is used for reporting, not the fail-closed halt check below). In
        paper mode, the simulated cash persisted in runtime kv, seeded to the
        paper starting bankroll on first read."""
        if self.state.get_mode("paper") == "live" and self.client is not None:
            try:
                return float(self._live_balances().get("USD", 0.0))
            except Exception as exc:  # noqa: BLE001 — display-only, fail soft
                log.warning("live cash fetch failed (%s); reporting 0.0", exc)
                return 0.0
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
        """Mark-to-market total account value used by the portfolio-halt
        check — this MUST reflect real capital, not just what the bot itself
        has bought.

        Paper mode: cash + mark-to-market of the bot's own tracked positions
        (the existing simulation — nothing else exists to value).

        Live mode: the REAL Coinbase balance (USD cash + every other held
        currency, priced at spot) — NOT `cash + positions_value`. Those two
        only ever reflect trades the bot itself made; they have no idea about
        pre-existing holdings, and `cash` was never updated by a live fill in
        the first place (see execution.py — only paper fills touch it). Using
        the paper-mode formula in live mode would compare the real bankroll
        (go_live_bankroll, correct) against a fabricated, essentially
        arbitrary total_value, which could trip the -30% halt on nothing or
        mask a real drawdown entirely.

        Raises on any un-markable balance/price (both branches) — the caller
        (risk) treats an un-markable portfolio as fail-closed, deliberately
        NOT the lenient/skip-and-continue behavior used by the one-time
        go-live confirmation printout in cli.py."""
        if self.state.get_mode("paper") == "live":
            if self.client is None:
                raise RuntimeError("live total_value requires a coinbase client")
            balances = self._live_balances()
            total = float(balances.get("USD", 0.0))
            for cur, amount in balances.items():
                if cur == "USD" or amount <= 0:
                    continue
                total += amount * price_source(f"{cur}-USD")
            return total
        return self.cash + self.positions_value(price_source)
