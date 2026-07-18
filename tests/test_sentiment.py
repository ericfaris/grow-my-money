"""Acceptance #1 & #2: SentimentProvider parses real-shape data into a lean and
fails open (returns None, never raises) on every failure mode."""
from __future__ import annotations

import pytest

from src.config import Config
from src.secrets import SecretError
from src.sentiment import SentimentProvider, SentimentResult


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _cfg(**over) -> Config:
    return Config(sentiment_ttl_sec=900, sentiment_timeout_sec=4.0, **over)


def _provider(payload=None, status_code=200, http_get=None, token="tok"):
    if http_get is None:
        resp = _FakeResp(payload, status_code)

        def http_get(url, params=None, timeout=None):  # noqa: ARG001
            http_get.calls += 1
            http_get.last_params = params
            return resp

        http_get.calls = 0
        http_get.last_params = None
    return SentimentProvider(_cfg(), token_loader=lambda: token, http_get=http_get)


# -- parsing / lean ----------------------------------------------------------
def test_bullish_payload_positive_score():
    payload = {"results": [
        {"votes": {"positive": 8, "negative": 1}, "panic_score": 20},
        {"votes": {"positive": 5, "negative": 0}},
    ]}
    prov = _provider(payload)
    res = prov.lean("BTC-USD")
    assert isinstance(res, SentimentResult)
    assert res.score > 0
    assert res.score == pytest.approx((13 - 1) / 14)
    assert res.panic == 20


def test_strongly_bearish_payload_low_score_and_panic():
    payload = {"results": [
        {"votes": {"positive": 1, "negative": 9}, "panic_score": 82},
        {"votes": {"positive": 0, "negative": 6}, "panic_score": 55},
    ]}
    prov = _provider(payload)
    res = prov.lean("ETH-USD")
    assert res.score <= -0.6
    assert res.panic == 82  # worst (max) panic across posts


def test_symbol_mapping_uses_bare_symbol():
    payload = {"results": [{"votes": {"positive": 3, "negative": 1}}]}
    calls = {}

    def http_get(url, params=None, timeout=None):  # noqa: ARG001
        calls["params"] = params
        return _FakeResp(payload)

    prov = SentimentProvider(_cfg(), token_loader=lambda: "tok", http_get=http_get)
    prov.lean("SOL-USD")
    assert calls["params"]["currencies"] == "SOL"
    assert calls["params"]["auth_token"] == "tok"


# -- fail-open matrix (criterion 2) ------------------------------------------
def test_token_loader_raises_secret_error_returns_none():
    def bad_token():
        raise SecretError("no token file")

    prov = SentimentProvider(_cfg(), token_loader=bad_token,
                             http_get=lambda *a, **k: _FakeResp({"results": []}))
    assert prov.lean("BTC-USD") is None


def test_http_get_raises_returns_none():
    def boom(*a, **k):
        raise RuntimeError("timeout")

    prov = SentimentProvider(_cfg(), token_loader=lambda: "tok", http_get=boom)
    assert prov.lean("BTC-USD") is None


def test_non_200_returns_none():
    prov = _provider({"results": []}, status_code=500)
    assert prov.lean("BTC-USD") is None


def test_malformed_json_missing_results_returns_none():
    prov = _provider({"unexpected": True})
    assert prov.lean("BTC-USD") is None


def test_empty_results_returns_none():
    prov = _provider({"results": []})
    assert prov.lean("BTC-USD") is None


def test_zero_total_votes_returns_none():
    prov = _provider({"results": [{"votes": {"positive": 0, "negative": 0}}]})
    assert prov.lean("BTC-USD") is None


def test_post_without_votes_dict_contributes_nothing():
    prov = _provider({"results": [{"foo": "bar"}, {"votes": None}]})
    assert prov.lean("BTC-USD") is None


# -- TTL cache ---------------------------------------------------------------
def test_lean_cached_within_ttl_calls_http_once():
    payload = {"results": [{"votes": {"positive": 3, "negative": 1}}]}
    prov = _provider(payload)
    r1 = prov.lean("BTC-USD")
    r2 = prov.lean("BTC-USD")
    assert r1 == r2
    assert prov._http_get.calls == 1  # second served from cache


def test_none_results_are_cached_too():
    # no token -> None, cached so we don't hammer the API each cycle
    calls = {"n": 0}

    def bad_token():
        calls["n"] += 1
        raise SecretError("no token")

    prov = SentimentProvider(_cfg(), token_loader=bad_token,
                             http_get=lambda *a, **k: _FakeResp({"results": []}))
    assert prov.lean("BTC-USD") is None
    assert prov.lean("BTC-USD") is None
    assert calls["n"] == 1  # token_loader consulted once, None cached
