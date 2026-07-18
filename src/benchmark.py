"""Buy-and-hold benchmark — measure skill, not luck.

A fixed hypothetical portfolio that bought equal parts BTC/ETH/SOL at the start
of the trading epoch and never traded. Marked to market exactly like the real
portfolio. The anchor is **write-once per epoch** (paper | live) — see the
plan's section 2.13. This module is report-only: it never touches an order
path, a cap, or the trade loop, and a missing anchor/price yields ``n/a``
rather than raising.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

PriceSource = Callable[[str], float]

# The benchmark is fixed to these three products, equal weight (plan section 9).
_BTC, _ETH, _SOL = "BTC-USD", "ETH-USD", "SOL-USD"


class Benchmark:
    def __init__(self, state):
        self.state = state

    def ensure_anchor(
        self,
        epoch: str,
        start_bankroll: float,
        prices: dict[str, float],
        anchor_ts_utc: Optional[str] = None,
    ) -> Optional[dict]:
        """Create the epoch anchor if absent (INSERT-if-absent), else return the
        existing one unchanged. Requires all three prices; returns None (and does
        NOT create) if any is missing/invalid — fail-closed on display, never a
        wrong baseline."""
        existing = self.state.get_benchmark_anchor(epoch)
        if existing is not None:
            return existing
        try:
            btc = float(prices[_BTC])
            eth = float(prices[_ETH])
            sol = float(prices[_SOL])
        except (KeyError, TypeError, ValueError):
            log.warning("Cannot strike %s benchmark anchor: missing/invalid prices", epoch)
            return None
        if btc <= 0 or eth <= 0 or sol <= 0:
            log.warning("Cannot strike %s benchmark anchor: non-positive price", epoch)
            return None
        anchor = self.state.create_benchmark_anchor_if_absent(
            epoch, start_bankroll, btc, eth, sol, anchor_ts_utc=anchor_ts_utc
        )
        log.info("Struck %s benchmark anchor: bankroll=%.2f units btc=%.6f eth=%.6f sol=%.6f",
                 epoch, start_bankroll, anchor["btc_units"], anchor["eth_units"],
                 anchor["sol_units"])
        return anchor

    def value(self, epoch: str, prices: dict[str, float]) -> Optional[float]:
        anchor = self.state.get_benchmark_anchor(epoch)
        if anchor is None:
            return None
        try:
            return (
                anchor["btc_units"] * float(prices[_BTC])
                + anchor["eth_units"] * float(prices[_ETH])
                + anchor["sol_units"] * float(prices[_SOL])
            )
        except (KeyError, TypeError, ValueError):
            return None

    def return_pct(self, epoch: str, prices: dict[str, float]) -> Optional[float]:
        anchor = self.state.get_benchmark_anchor(epoch)
        if anchor is None:
            return None
        val = self.value(epoch, prices)
        if val is None or anchor["start_bankroll"] == 0:
            return None
        return (val - anchor["start_bankroll"]) / anchor["start_bankroll"]

    def compare(self, epoch: str, portfolio, prices: dict[str, float],
                price_source: PriceSource) -> dict:
        """Return actual vs baseline vs delta. Any un-computable side is ``None``
        (rendered as ``n/a``). Never raises into the trade loop."""
        result = {
            "epoch": epoch,
            "actual_value": None,
            "actual_return_pct": None,
            "baseline_value": None,
            "baseline_return_pct": None,
            "delta_pct": None,
        }
        try:
            actual_val = portfolio.total_value(price_source)
            anchor = self.state.get_benchmark_anchor(epoch)
            base_capital = anchor["start_bankroll"] if anchor else None
            result["actual_value"] = actual_val
            if base_capital:
                result["actual_return_pct"] = (actual_val - base_capital) / base_capital
        except Exception as exc:  # report-only: never propagate
            log.warning("benchmark compare: actual side unavailable: %s", exc)

        result["baseline_value"] = self.value(epoch, prices)
        result["baseline_return_pct"] = self.return_pct(epoch, prices)

        if (result["actual_return_pct"] is not None
                and result["baseline_return_pct"] is not None):
            result["delta_pct"] = (
                result["actual_return_pct"] - result["baseline_return_pct"]
            )
        return result
