"""Regression: paper slippage used to be a flat slippage_bps regardless of a
product's liquidity. Once product discovery widened the tradeable universe
down to ~$5M/day-volume pairs, that flat assumption could make paper P&L
look better than live execution could actually achieve on a thin book.
PaperFillSimulator now scales slippage by sqrt(reference_volume/volume),
clamped to [1x, max_multiplier]."""
from __future__ import annotations

from src.execution import PaperFillSimulator


def _sim(volumes, slippage_bps=5.0, reference=50_000_000.0, max_mult=5.0):
    return PaperFillSimulator(
        price_source=lambda p: 100.0, slippage_bps=slippage_bps, fee_bps=0.0,
        volume_source=lambda p: volumes.get(p),
        liquidity_reference_volume=reference, max_slippage_multiplier=max_mult)


def test_reference_volume_gets_unscaled_slippage():
    sim = _sim({"BTC-USD": 50_000_000.0})
    fill = sim.fill("BTC-USD", "buy", 1000.0)
    assert fill["price"] == 100.0 * 1.0005  # 5bps, 1x


def test_thin_volume_gets_larger_slippage():
    sim = _sim({"THIN-USD": 5_000_000.0})  # 1/10th reference -> sqrt(10) ~= 3.16x
    fill = sim.fill("THIN-USD", "buy", 1000.0)
    expected_multiplier = (50_000_000.0 / 5_000_000.0) ** 0.5
    expected_price = 100.0 * (1 + (5.0 * expected_multiplier) / 10_000.0)
    assert abs(fill["price"] - expected_price) < 1e-9


def test_multiplier_clamped_at_max():
    sim = _sim({"MICRO-USD": 1_000.0}, max_mult=5.0)  # would be huge unclamped
    fill = sim.fill("MICRO-USD", "buy", 1000.0)
    expected_price = 100.0 * (1 + (5.0 * 5.0) / 10_000.0)  # clamped to 5x
    assert abs(fill["price"] - expected_price) < 1e-9


def test_multiplier_never_below_one_for_superliquid_product():
    sim = _sim({"SUPER-USD": 500_000_000.0})  # 10x more liquid than reference
    fill = sim.fill("SUPER-USD", "buy", 1000.0)
    assert fill["price"] == 100.0 * 1.0005  # still just 1x, never scaled down


def test_unknown_volume_falls_back_to_1x():
    sim = _sim({})  # no volume data at all
    fill = sim.fill("MYSTERY-USD", "buy", 1000.0)
    assert fill["price"] == 100.0 * 1.0005


def test_no_volume_source_preserves_flat_behavior():
    sim = PaperFillSimulator(price_source=lambda p: 100.0, slippage_bps=5.0, fee_bps=0.0)
    fill = sim.fill("ANY-USD", "buy", 1000.0)
    assert fill["price"] == 100.0 * 1.0005


def test_volume_source_exception_falls_back_to_1x():
    def boom(product):
        raise RuntimeError("lookup failed")
    sim = PaperFillSimulator(price_source=lambda p: 100.0, slippage_bps=5.0, fee_bps=0.0,
                             volume_source=boom)
    fill = sim.fill("ANY-USD", "buy", 1000.0)
    assert fill["price"] == 100.0 * 1.0005


def test_sell_side_slippage_also_scales():
    sim = _sim({"THIN-USD": 5_000_000.0})
    fill = sim.fill("THIN-USD", "sell", notional=1000.0, base_size=10.0)
    expected_multiplier = (50_000_000.0 / 5_000_000.0) ** 0.5
    expected_price = 100.0 * (1 - (5.0 * expected_multiplier) / 10_000.0)
    assert abs(fill["price"] - expected_price) < 1e-9
