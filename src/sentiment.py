"""News/sentiment provider — a lightweight, FAIL-OPEN awareness layer.

Fetches recent CryptoPanic community posts for a product's bare symbol and
aggregates a single net "lean" score in [-1, +1] (negative = bearish) from the
posts' up/down votes. The result is consumed by ``RiskManager`` (buys only) to
optionally dampen or veto a buy the technicals + model would otherwise approve.

Design contract (mirrors ``dashboard_data.PriceProvider``):
  * ``lean`` NEVER raises — any failure (no token, HTTP error, timeout, non-200,
    malformed/empty JSON, zero votes) degrades to ``None`` ("no signal"), which
    the caller treats as "proceed unchanged". This is the fail-open guarantee.
  * A process-local TTL cache stores results INCLUDING ``None`` so a missing
    token or a down API is not re-fetched every cycle.
  * ``token_loader`` and ``http_get`` are constructor-injected so tests use
    fakes with no network and no real token.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class SentimentResult:
    product: str
    score: float           # [-1, +1], negative = bearish
    panic: Optional[float]  # worst panic_score seen (0-100), or None
    n_posts: int


class SentimentProvider:
    """Per-product news lean with a TTL cache. Never raises to the caller."""

    def __init__(self, cfg, token_loader: Optional[Callable[[], str]] = None,
                 http_get: Optional[Callable] = None):
        self.cfg = cfg
        if token_loader is None:
            from . import secrets as secrets_mod

            def token_loader() -> str:
                return secrets_mod.cryptopanic_token(cfg.cryptopanic_token_file or None)
        if http_get is None:
            import requests

            http_get = requests.get
        self._token_loader = token_loader
        self._http_get = http_get
        # product -> (result | None, monotonic_ts)
        self._cache: dict[str, tuple[Optional[SentimentResult], float]] = {}

    def lean(self, product: str) -> Optional[SentimentResult]:
        """Return the net sentiment lean for ``product``, or ``None`` (no signal).

        Fail-open: any error path returns ``None`` and is cached, never raised.
        """
        now = time.monotonic()
        cached = self._cache.get(product)
        if cached is not None and (now - cached[1]) < self.cfg.sentiment_ttl_sec:
            return cached[0]

        result = self._fetch(product)
        self._cache[product] = (result, now)
        return result

    # -- internals ---------------------------------------------------------
    def _fetch(self, product: str) -> Optional[SentimentResult]:
        try:
            try:
                token = self._token_loader()
            except Exception as exc:  # SecretError or anything else -> no signal
                log.info(
                    "sentiment unavailable for %s: %s; proceeding without dampening",
                    product, exc,
                )
                return None

            symbol = product.split("-")[0]
            resp = self._http_get(
                self.cfg.sentiment_api_base,
                params={"auth_token": token, "currencies": symbol},
                timeout=self.cfg.sentiment_timeout_sec,
            )
            status = getattr(resp, "status_code", 200)
            if status != 200:
                log.info(
                    "sentiment unavailable for %s: HTTP %s; proceeding without dampening",
                    product, status,
                )
                return None

            data = resp.json()
            return self._score(product, data)
        except Exception as exc:  # noqa: BLE001 — fail-open: never raise
            log.info(
                "sentiment unavailable for %s: %s; proceeding without dampening",
                product, exc,
            )
            return None

    @staticmethod
    def _score(product: str, data) -> Optional[SentimentResult]:
        results = (data or {}).get("results") or []
        pos = 0
        neg = 0
        panic: Optional[float] = None
        for post in results:
            votes = post.get("votes") if isinstance(post, dict) else None
            if isinstance(votes, dict):
                try:
                    pos += int(votes.get("positive") or 0)
                    neg += int(votes.get("negative") or 0)
                except (TypeError, ValueError):
                    pass
            ps = post.get("panic_score") if isinstance(post, dict) else None
            if ps is not None:
                try:
                    ps_f = float(ps)
                    panic = ps_f if panic is None else max(panic, ps_f)
                except (TypeError, ValueError):
                    pass

        total = pos + neg
        if total == 0:
            return None  # no vote data -> no signal (fail-open)
        score = (pos - neg) / total
        return SentimentResult(product=product, score=score, panic=panic,
                               n_posts=len(results))
