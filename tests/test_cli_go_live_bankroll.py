"""Regression: go-live bankroll used to be `balances["USD"]` only (src/cli.py),
silently ignoring every non-USD holding (BTC/ETH/etc. already in the account).
Discovered when the real account's USD cash was $0.72 against $836+ of actual
crypto holdings. _total_account_value_usd fixes this by summing all priceable
non-zero balances at spot."""
from __future__ import annotations

import pytest

from src.cli import _total_account_value_usd


def _price_source(prices):
    def src(product_id):
        if product_id not in prices:
            raise RuntimeError(f"no price for {product_id}")
        return prices[product_id]
    return src


def test_sums_usd_cash_plus_priced_crypto_holdings():
    balances = {"USD": 0.72, "BTC": 0.00248627, "ETH": 0.03434226, "XRP": 462.445611}
    prices = {"BTC-USD": 66610.695, "ETH-USD": 1934.025, "XRP-USD": 1.13995}
    total = _total_account_value_usd(balances, _price_source(prices))
    assert total == pytest.approx(0.72 + 165.61 + 66.42 + 527.16, abs=0.5)


def test_usd_only_balance_no_longer_undercounts():
    """The exact regression: USD cash alone was $0.72 while real holdings were
    ~$836 — the old code would have returned 0.72."""
    balances = {"USD": 0.72, "BTC": 0.00248627}
    prices = {"BTC-USD": 66610.695}
    total = _total_account_value_usd(balances, _price_source(prices))
    assert total > 100  # old buggy behavior would assert this fails (== 0.72)


def test_zero_and_negative_balances_excluded():
    balances = {"USD": 10.0, "DOGE": 0.0, "SHIB": -1.0}
    total = _total_account_value_usd(balances, _price_source({}))
    assert total == 10.0  # never asked to price DOGE/SHIB


def test_unpriceable_currency_excluded_not_fatal():
    balances = {"USD": 10.0, "BTC": 1.0, "SOMEWEIRDCOIN": 5.0}
    prices = {"BTC-USD": 60_000.0}  # no price for SOMEWEIRDCOIN
    warnings = []
    total = _total_account_value_usd(balances, _price_source(prices),
                                     warn=warnings.append)
    assert total == pytest.approx(10.0 + 60_000.0)
    assert len(warnings) == 1
    assert "SOMEWEIRDCOIN" in warnings[0]
