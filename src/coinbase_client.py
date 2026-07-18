"""Thin wrapper over the official ``coinbase-advanced-py`` RESTClient.

Purpose: (1) present a tiny, testable interface so units can inject a fake, and
(2) keep the *only* real-order call sites in one place. Includes simple
retry/backoff on transient errors and treats HTTP 429 as fail-closed for the
cycle (raise ``RateLimited`` — the caller skips trading rather than spinning).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """Raised on HTTP 429 — the caller must fail closed for this cycle."""


class CoinbaseError(Exception):
    pass


def _to_dict(resp: Any) -> dict:
    """Coinbase SDK returns typed objects that support ``to_dict`` or dict()."""
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    for attr in ("to_dict", "__dict__"):
        if hasattr(resp, attr):
            val = getattr(resp, attr)
            return val() if callable(val) else dict(val)
    return dict(resp)


def _is_429(exc: Exception) -> bool:
    text = f"{getattr(exc, 'response', '')} {exc}".lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


class CoinbaseClient:
    """Wraps a ``RESTClient``-shaped object (real or fake).

    In production, construct with ``CoinbaseClient.from_key_file(path)``. In
    tests, pass any object exposing the SDK method surface via ``rest_client``.
    """

    def __init__(self, rest_client: Any, max_retries: int = 3, backoff_base: float = 0.5):
        self.rc = rest_client
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    @classmethod
    def from_key_file(cls, key_file: str, **kw) -> "CoinbaseClient":
        from coinbase.rest import RESTClient  # imported lazily so tests need no key

        return cls(RESTClient(key_file=key_file), **kw)

    def _call(self, fn, *args, **kwargs):
        last: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if _is_429(exc):
                    raise RateLimited(str(exc)) from exc
                last = exc
                sleep = self.backoff_base * (2 ** attempt)
                log.warning("Coinbase call failed (attempt %d): %s; retrying in %.1fs",
                            attempt + 1, exc, sleep)
                time.sleep(sleep)
        raise CoinbaseError(str(last)) from last

    # -- reads -------------------------------------------------------------
    def get_balances(self) -> dict[str, float]:
        """Return {currency: available_balance} across accounts."""
        resp = _to_dict(self._call(self.rc.get_accounts))
        out: dict[str, float] = {}
        for acct in resp.get("accounts", []):
            acct = _to_dict(acct)
            cur = acct.get("currency")
            bal = _to_dict(acct.get("available_balance"))
            try:
                out[cur] = float(bal.get("value", 0.0))
            except (TypeError, ValueError):
                out[cur] = 0.0
        return out

    def get_spot_price(self, product_id: str) -> float:
        """Mid price from best bid/ask."""
        resp = _to_dict(self._call(self.rc.get_best_bid_ask, product_ids=[product_id]))
        books = resp.get("pricebooks") or resp.get("price_books") or []
        if not books:
            raise CoinbaseError(f"no pricebook for {product_id}")
        book = _to_dict(books[0])
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid = float(_to_dict(bids[0]).get("price")) if bids else None
        ask = float(_to_dict(asks[0]).get("price")) if asks else None
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid or ask or 0.0

    def get_best_bid_ask(self, product_id: str) -> tuple[float, float]:
        resp = _to_dict(self._call(self.rc.get_best_bid_ask, product_ids=[product_id]))
        books = resp.get("pricebooks") or resp.get("price_books") or []
        if not books:
            raise CoinbaseError(f"no pricebook for {product_id}")
        book = _to_dict(books[0])
        bid = float(_to_dict(book["bids"][0]).get("price"))
        ask = float(_to_dict(book["asks"][0]).get("price"))
        return bid, ask

    def get_candles(self, product_id: str, granularity: str, start: str, end: str,
                    limit: Optional[int] = None) -> list[dict]:
        resp = _to_dict(
            self._call(self.rc.get_candles, product_id=product_id, start=start,
                       end=end, granularity=granularity, limit=limit)
        )
        candles = resp.get("candles", [])
        return [_to_dict(c) for c in candles]

    def get_order(self, order_id: str) -> dict:
        return _to_dict(self._call(self.rc.get_order, order_id=order_id))

    # -- writes (LIVE ORDERS — the only real-money call sites) -------------
    def place_market_buy(self, client_order_id: str, product_id: str,
                         quote_size: float) -> dict:
        resp = self._call(
            self.rc.market_order_buy,
            client_order_id=client_order_id,
            product_id=product_id,
            quote_size=str(quote_size),
        )
        return _to_dict(resp)

    def place_market_sell(self, client_order_id: str, product_id: str,
                          base_size: float) -> dict:
        resp = self._call(
            self.rc.market_order_sell,
            client_order_id=client_order_id,
            product_id=product_id,
            base_size=str(base_size),
        )
        return _to_dict(resp)
