"""Dynamic product-universe discovery: which USD spot pairs the bot watches.

Replaces a hand-maintained PRODUCTS list with an objective, liquidity-gated
selection from Coinbase's full product catalog. Refreshed periodically (see
Bot._refresh_products, called at startup and on the nightly tick) — never on
every decision cycle, since it fetches the entire catalog in one call. Fails
soft: any error or empty result must leave the bot's existing product list
(the static PRODUCTS config on first run, or yesterday's discovered list
thereafter) untouched — see the caller, not this module, for that guarantee.
"""
from __future__ import annotations

# USD-pegged stablecoins have ~zero directional price movement — a
# momentum/technical strategy has no edge on them and would just churn fees.
STABLE_BASE_DENYLIST = {"USDT", "USDC", "DAI", "PYUSD", "GUSD", "EURC", "USDP", "LUSD"}

# Pinned regardless of rank/threshold — the buy-and-hold benchmark anchor is
# fixed to these three (see benchmark.py) and needs their prices every cycle.
ALWAYS_INCLUDE = ("BTC-USD", "ETH-USD", "SOL-USD")


def _quote_volume(p: dict) -> float:
    try:
        return float(p.get("approximate_quote_24h_volume") or 0)
    except (TypeError, ValueError):
        return 0.0


def select_products(raw_products: list[dict], min_quote_volume_24h: float,
                    max_count: int) -> list[str]:
    """Pure filter/rank over Coinbase's raw product list (see
    CoinbaseClient.get_products). Returns product_ids, most-liquid first,
    always including ALWAYS_INCLUDE regardless of their rank or the volume
    threshold."""
    candidates = []
    for p in raw_products:
        if p.get("quote_currency_id") != "USD":
            continue
        if p.get("product_type") != "SPOT":
            continue
        if p.get("status") != "online":
            continue
        if p.get("trading_disabled") or p.get("is_disabled") or p.get("view_only"):
            continue
        # The bot only ever places MARKET orders (execution.py). A product
        # restricted to cancel/post/limit-only would reject a real market
        # order in live mode — paper mode must not "trade" it either, or
        # paper P&L includes trades that could never actually happen live.
        if p.get("cancel_only") or p.get("post_only") or p.get("limit_only"):
            continue
        if p.get("base_currency_id") in STABLE_BASE_DENYLIST:
            continue
        if _quote_volume(p) < min_quote_volume_24h:
            continue
        candidates.append(p)

    candidates.sort(key=_quote_volume, reverse=True)
    selected = [p["product_id"] for p in candidates[:max_count] if p.get("product_id")]

    for pinned in ALWAYS_INCLUDE:
        if pinned not in selected:
            selected.append(pinned)
    return selected


def discover(client, min_quote_volume_24h: float,
            max_count: int) -> tuple[list[str], dict[str, float]]:
    """Fetch + select. Returns (product_ids, volumes) — volumes maps each
    selected product to its approximate 24h USD volume, for slippage scaling
    (see execution.PaperFillSimulator). Raises on a hard client failure or an
    empty result — the caller must catch and fail open to the previous
    product list.

    NOTE: select_products always pins ALWAYS_INCLUDE regardless of what
    cleared the volume filter, so an empty *catalog fetch* (client returned
    nothing — a real outage/failure, not "nothing was liquid enough") must be
    caught here, before selection, or it would silently "succeed" with just
    the three pinned names instead of raising."""
    raw = client.get_products(quote_currency="USD")
    if not raw:
        raise ValueError("product discovery: Coinbase returned no products")
    selected = select_products(raw, min_quote_volume_24h, max_count)
    if not selected:
        raise ValueError("product discovery returned an empty list")
    volumes = {p["product_id"]: _quote_volume(p) for p in raw
              if p.get("product_id") in selected}
    return selected, volumes
