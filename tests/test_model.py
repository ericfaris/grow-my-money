"""Acceptance: cold-start pseudo-prob; fit produces usable p_win; promotion
guard rejects a worse holdout."""
from __future__ import annotations

import numpy as np

from src.indicators import Features
from src.model import Model, rule_pseudo_prob


def _feat(**kw) -> Features:
    base = dict(price=100.0, ema_fast=101.0, ema_slow=100.0, ema_gap_pct=0.01,
                ema_bullish=1, rsi=55.0, macd_line=0.5, macd_signal=0.2,
                macd_hist=0.3, macd_bullish=1, ret_recent=0.02, volatility=0.01)
    base.update(kw)
    return Features(**base)


def test_cold_start_returns_rule_pseudo_prob(config, state, tmp_path):
    m = Model(config, state, tmp_path / "model.pkl")
    p, tag = m.predict_p_win(_feat())
    assert tag == "coldstart"
    assert 0.0 <= p <= 1.0
    # bullish alignment should push above 0.5
    assert p > 0.5


def test_rule_pseudo_prob_direction():
    bull = rule_pseudo_prob(_feat(ema_bullish=1, macd_bullish=1).to_vector())
    bear = rule_pseudo_prob(_feat(ema_bullish=0, macd_bullish=0, ema_gap_pct=-0.01).to_vector())
    assert bull > bear


def _seed_outcomes(state, n, sep=0.3):
    """Seed separable labeled data: bullish features -> label 1, bearish -> 0."""
    rng = np.random.default_rng(0)
    for i in range(n):
        if i % 2 == 0:
            f = _feat(ema_gap_pct=0.02 + rng.normal(0, 0.005), ema_bullish=1,
                      macd_hist=sep, macd_bullish=1, rsi=60, ret_recent=0.03)
            label = 1
        else:
            f = _feat(ema_gap_pct=-0.02 + rng.normal(0, 0.005), ema_bullish=0,
                      macd_hist=-sep, macd_bullish=0, rsi=40, ret_recent=-0.03)
            label = 0
        state.record_outcome("BTC-USD", f"2026-01-01T00:{i:02d}:00+00:00",
                             f.to_vector(), label, 1.0 if label else -1.0)


def test_fit_produces_usable_p_win(config, state, tmp_path):
    config = config.__class__(**{**config.__dict__, "model_min_train_samples": 10})
    _seed_outcomes(state, 40)
    m = Model(config, state, tmp_path / "model.pkl")
    res = m.maybe_retrain()
    assert res["trained"] is True
    assert res["promoted"] is True
    p_bull, tag = m.predict_p_win(_feat(ema_gap_pct=0.03, ema_bullish=1, macd_hist=0.4,
                                        macd_bullish=1))
    p_bear, _ = m.predict_p_win(_feat(ema_gap_pct=-0.03, ema_bullish=0, macd_hist=-0.4,
                                      macd_bullish=0))
    assert tag == "model"
    assert p_bull > p_bear  # learned the separation


def test_below_min_samples_stays_cold(config, state, tmp_path):
    _seed_outcomes(state, 6)
    m = Model(config, state, tmp_path / "model.pkl")
    res = m.maybe_retrain()
    assert res["trained"] is False
    assert m.is_cold is True


def test_promotion_guard_rejects_worse_holdout(config, state, tmp_path):
    """The guard must KEEP the previous model when a refit's holdout log-loss
    regresses beyond tolerance. We drive the log-loss values deterministically so
    the decision itself is what is under test (not sklearn's fit noise)."""
    config = config.__class__(**{**config.__dict__, "model_min_train_samples": 10,
                                 "model_promote_max_regression": 0.02})
    _seed_outcomes(state, 40, sep=0.4)
    m = Model(config, state, tmp_path / "model.pkl")
    m.maybe_retrain()  # first fit: prev is None -> promoted
    assert not m.is_cold
    good_clf = m._clf

    # Seed more data so maybe_retrain proceeds, but force the log-loss so the
    # candidate scores clearly worse than prev + tolerance.
    _seed_outcomes(state, 10)  # -> 50 rows, both classes present

    def fake_logloss(clf, rows):
        # previous (good) model scores 0.30; any freshly-fit candidate scores 0.90
        return 0.30 if clf is good_clf else 0.90

    m._logloss = fake_logloss  # instance attr shadows the staticmethod
    res = m.maybe_retrain()
    assert res["trained"] is True
    assert res["promoted"] is False
    assert m._clf is good_clf  # previous model retained


def test_promotion_guard_accepts_non_regressing(config, state, tmp_path):
    config = config.__class__(**{**config.__dict__, "model_min_train_samples": 10,
                                 "model_promote_max_regression": 0.02})
    _seed_outcomes(state, 40, sep=0.4)
    m = Model(config, state, tmp_path / "model.pkl")
    m.maybe_retrain()
    good_clf = m._clf
    _seed_outcomes(state, 10)

    def fake_logloss(clf, rows):
        return 0.30 if clf is good_clf else 0.25  # candidate better

    m._logloss = fake_logloss
    res = m.maybe_retrain()
    assert res["promoted"] is True
    assert m._clf is not good_clf  # new model deployed
