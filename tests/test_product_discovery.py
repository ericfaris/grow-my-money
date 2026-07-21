"""Regression/acceptance for dynamic product-universe discovery: the bot no
longer trades a hand-maintained PRODUCTS list only — it discovers liquid USD
spot pairs from Coinbase's live catalog, refreshed at startup and nightly,
failing open to the previous list on any error."""
from __future__ import annotations

from src.bot import Bot
from src.coinbase_client import CoinbaseClient
from src.product_discovery import ALWAYS_INCLUDE, discover, select_products


def _product(product_id, base, quote="USD", volume=10_000_000.0, **overrides):
    p = dict(
        product_id=product_id, base_currency_id=base, quote_currency_id=quote,
        product_type="SPOT", status="online", trading_disabled=False,
        is_disabled=False, view_only=False, approximate_quote_24h_volume=str(volume),
    )
    p.update(overrides)
    return p


# -- select_products (pure filter/rank) --------------------------------------

def test_filters_out_thin_volume():
    products = [_product("BTC-USD", "BTC", volume=50_000_000),
               _product("DUST-USD", "DUST", volume=1_000)]
    selected = select_products(products, min_quote_volume_24h=1_000_000, max_count=40)
    assert "BTC-USD" in selected
    assert "DUST-USD" not in selected


def test_filters_out_non_usd_quote():
    products = [_product("BTC-EUR", "BTC", quote="EUR", volume=50_000_000)]
    selected = select_products(products, min_quote_volume_24h=0, max_count=40)
    assert selected == list(ALWAYS_INCLUDE)  # nothing else qualified


def test_filters_out_disabled_and_offline():
    products = [
        _product("A-USD", "A", trading_disabled=True),
        _product("B-USD", "B", is_disabled=True),
        _product("C-USD", "C", view_only=True),
        _product("D-USD", "D", status="delisted"),
    ]
    selected = select_products(products, min_quote_volume_24h=0, max_count=40)
    for pid in ("A-USD", "B-USD", "C-USD", "D-USD"):
        assert pid not in selected


def test_filters_out_stablecoins():
    products = [_product("USDT-USD", "USDT", volume=100_000_000)]
    selected = select_products(products, min_quote_volume_24h=0, max_count=40)
    assert "USDT-USD" not in selected


def test_always_include_pinned_regardless_of_volume():
    products = [_product("BTC-USD", "BTC", volume=1)]  # would fail the floor
    selected = select_products(products, min_quote_volume_24h=1_000_000, max_count=40)
    for pid in ALWAYS_INCLUDE:
        assert pid in selected


def test_ranked_by_volume_and_capped_at_max_count():
    products = [_product(f"C{i}-USD", f"C{i}", volume=float(i)) for i in range(10)]
    selected = select_products(products, min_quote_volume_24h=0, max_count=3)
    non_pinned = [p for p in selected if p not in ALWAYS_INCLUDE]
    assert non_pinned == ["C9-USD", "C8-USD", "C7-USD"]  # highest volume first


# -- discover (fetch + select) -----------------------------------------------

class _FakeClient:
    def __init__(self, products):
        self._products = products

    def get_products(self, quote_currency="USD"):
        return self._products


def test_discover_raises_on_empty_result():
    import pytest
    client = _FakeClient([])
    with pytest.raises(ValueError):
        discover(client, min_quote_volume_24h=1_000_000, max_count=40)


def test_discover_returns_selection_and_volume_map():
    client = _FakeClient([_product("BTC-USD", "BTC", volume=50_000_000)])
    selected, volumes = discover(client, min_quote_volume_24h=1_000_000, max_count=40)
    assert "BTC-USD" in selected
    assert volumes["BTC-USD"] == 50_000_000.0


def test_cancel_only_post_only_limit_only_excluded():
    """The bot only ever places MARKET orders — a product that can't take one
    live must not be paper-tradeable either, or paper P&L includes trades
    that could never actually execute."""
    products = [
        _product("A-USD", "A", cancel_only=True),
        _product("B-USD", "B", post_only=True),
        _product("C-USD", "C", limit_only=True),
        _product("D-USD", "D"),  # control: none of the flags set
    ]
    selected = select_products(products, min_quote_volume_24h=0, max_count=40)
    assert "A-USD" not in selected
    assert "B-USD" not in selected
    assert "C-USD" not in selected
    assert "D-USD" in selected


# -- CoinbaseClient.get_products ----------------------------------------------

class _FakeRestClient:
    def __init__(self, products):
        self._products = products

    def get_products(self, product_type=None, **kw):
        return {"products": self._products}


def test_coinbase_client_get_products_filters_quote_currency():
    raw = [_product("BTC-USD", "BTC", quote="USD"),
           _product("BTC-EUR", "BTC", quote="EUR")]
    client = CoinbaseClient(_FakeRestClient(raw))
    result = client.get_products(quote_currency="USD")
    ids = [p["product_id"] for p in result]
    assert ids == ["BTC-USD"]


# -- Bot._refresh_products wiring --------------------------------------------

def test_refresh_products_updates_on_success(config, state, tmp_path):
    config = config.__class__(**{**config.__dict__, "product_discovery_enabled": True,
                                 "product_min_quote_volume_24h": 1_000_000,
                                 "product_discovery_max_count": 40})
    client = _FakeClient([_product("XRP-USD", "XRP", volume=50_000_000)])
    bot = Bot(config, state, coinbase_client=client, state_dir=tmp_path)
    original = list(bot.products)
    bot._refresh_products()
    assert "XRP-USD" in bot.products
    assert bot.products != original


def test_refresh_products_fails_open_on_error(config, state, tmp_path):
    class BrokenClient:
        def get_products(self, quote_currency="USD"):
            raise RuntimeError("Coinbase is down")

    config = config.__class__(**{**config.__dict__, "product_discovery_enabled": True})
    bot = Bot(config, state, coinbase_client=BrokenClient(), state_dir=tmp_path)
    original = list(bot.products)
    bot._refresh_products()
    assert bot.products == original  # untouched, never emptied


def test_refresh_products_noop_when_disabled(config, state, tmp_path):
    config = config.__class__(**{**config.__dict__, "product_discovery_enabled": False})
    client = _FakeClient([_product("XRP-USD", "XRP", volume=50_000_000)])
    bot = Bot(config, state, coinbase_client=client, state_dir=tmp_path)
    original = list(bot.products)
    bot._refresh_products()
    assert bot.products == original


def test_refresh_products_noop_without_client(config, state, tmp_path):
    bot = Bot(config, state, coinbase_client=None, state_dir=tmp_path)
    original = list(bot.products)
    bot._refresh_products()
    assert bot.products == original
