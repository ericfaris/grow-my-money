"""Adaptive logistic-regression layer: featurize, predict, retrain, persist.

Predicts ``p_win`` = probability that a buy now is profitable over
``MODEL_HORIZON_HOURS`` net of round-trip fees. Used to gate buys (fire only if
``p_win >= BUY_PROBABILITY_THRESHOLD``) and scale position size. Sells are never
gated by the model.

Cold start: until ``MODEL_MIN_TRAIN_SAMPLES`` closed trades exist, returns a
rule-derived pseudo-probability and logs ``model=coldstart``.

Retrain: on N new closed trades OR the nightly tick; refit on full history,
evaluate on a time-ordered holdout, and PROMOTE only if holdout log-loss does not
regress beyond ``MODEL_PROMOTE_MAX_REGRESSION`` — otherwise keep the old model.
On a pickle load failure, fall back to cold-start (never crash).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from .indicators import Features
from .state import iso, utcnow

log = logging.getLogger(__name__)

# Ordered numeric feature names fed to the model.
FEATURE_KEYS = ["ema_gap_pct", "ema_bullish", "rsi", "macd_hist", "macd_bullish",
                "ret_recent", "volatility"]


def features_to_row(feats) -> list[float]:
    if isinstance(feats, Features):
        d = feats.to_vector()
    else:
        d = feats
    return [float(d.get(k, 0.0)) for k in FEATURE_KEYS]


def rule_pseudo_prob(feats) -> float:
    """Cold-start probability derived from indicator alignment, mapped to ~[0.4,0.7]."""
    d = feats.to_vector() if isinstance(feats, Features) else feats
    score = 0.0
    score += 0.10 if d.get("ema_bullish") else -0.10
    score += 0.10 if d.get("macd_bullish") else -0.10
    rsi = d.get("rsi", 50.0)
    if rsi < 30:
        score += 0.05
    elif rsi > 70:
        score -= 0.10
    score += max(-0.05, min(0.05, d.get("ema_gap_pct", 0.0) * 2.0))
    return float(max(0.0, min(1.0, 0.5 + score)))


class Model:
    def __init__(self, config, state, model_path: str | Path):
        self.cfg = config
        self.state = state
        self.model_path = Path(model_path)
        self._clf = None
        self._n_trained = 0
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.model_path.exists():
            return
        try:
            import joblib
            data = joblib.load(self.model_path)
            self._clf = data["clf"]
            self._n_trained = data.get("n_samples", 0)
            log.info("Loaded model from %s (n_samples=%d)", self.model_path, self._n_trained)
        except Exception as exc:  # noqa: BLE001 — fall back to cold-start, never crash
            log.warning("Model load failed (%s); falling back to cold-start rules", exc)
            self._clf = None

    def _save(self, clf, n_samples: int) -> None:
        import joblib
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"clf": clf, "n_samples": n_samples}, self.model_path)

    # -- prediction --------------------------------------------------------
    @property
    def is_cold(self) -> bool:
        return self._clf is None

    def predict_p_win(self, feats) -> tuple[float, str]:
        """Return (p_win, mode_tag). mode_tag is 'coldstart' or 'model'."""
        if self._clf is None:
            return rule_pseudo_prob(feats), "coldstart"
        try:
            x = np.array([features_to_row(feats)], dtype=float)
            p = float(self._clf.predict_proba(x)[0][1])
            return p, "model"
        except Exception as exc:  # noqa: BLE001
            log.warning("predict failed (%s); using cold-start rule prob", exc)
            return rule_pseudo_prob(feats), "coldstart"

    # -- training / retrain with promotion guard ---------------------------
    def _fit(self, rows: list[dict]):
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline

        X = np.array([features_to_row(r["features"]) for r in rows], dtype=float)
        y = np.array([int(r["label"]) for r in rows], dtype=int)
        pipe = Pipeline([
            ("scale", StandardScaler()),
            ("lr", LogisticRegression(max_iter=1000, C=1.0)),
        ])
        pipe.fit(X, y)
        return pipe

    @staticmethod
    def _logloss(clf, rows: list[dict]) -> float:
        from sklearn.metrics import log_loss
        X = np.array([features_to_row(r["features"]) for r in rows], dtype=float)
        y = np.array([int(r["label"]) for r in rows], dtype=int)
        p = clf.predict_proba(X)[:, 1]
        return float(log_loss(y, p, labels=[0, 1]))

    def maybe_retrain(self, force: bool = False) -> dict:
        """Retrain if enough new data (or forced). Promote only if holdout does
        not regress. Returns a dict describing what happened."""
        rows = self.state.all_outcomes()
        n = len(rows)
        result = {"trained": False, "promoted": False, "n_samples": n, "reason": ""}

        if n < self.cfg.model_min_train_samples and not force:
            result["reason"] = "below min train samples (cold start)"
            return result

        # need both classes present to train a classifier
        labels = {int(r["label"]) for r in rows}
        if len(labels) < 2:
            result["reason"] = "only one outcome class present"
            return result

        # time-ordered holdout (last 20%, at least 1)
        rows = sorted(rows, key=lambda r: r["entry_ts_utc"])
        holdout_n = max(1, int(n * 0.2))
        train_rows, holdout_rows = rows[:-holdout_n], rows[-holdout_n:]
        if len({int(r["label"]) for r in train_rows}) < 2:
            result["reason"] = "training split lacks both classes"
            return result

        candidate = self._fit(train_rows)
        try:
            cand_loss = self._logloss(candidate, holdout_rows)
        except Exception:
            cand_loss = float("inf")

        prev_loss = None
        if self._clf is not None:
            try:
                prev_loss = self._logloss(self._clf, holdout_rows)
            except Exception:
                prev_loss = None

        promote = (
            prev_loss is None
            or cand_loss <= prev_loss + self.cfg.model_promote_max_regression
        )
        result.update({"trained": True, "holdout_logloss": cand_loss,
                        "prev_logloss": prev_loss})

        if promote:
            # refit on full data for the deployed model
            deployed = self._fit(rows)
            self._clf = deployed
            self._n_trained = n
            self._save(deployed, n)
            result["promoted"] = True
            result["reason"] = "promoted (holdout not worse)"
            log.info("Model PROMOTED: n=%d holdout_logloss=%.4f (prev=%s)",
                     n, cand_loss, prev_loss)
        else:
            result["promoted"] = False
            result["reason"] = (f"rejected: holdout {cand_loss:.4f} regressed beyond "
                                f"{self.cfg.model_promote_max_regression} vs {prev_loss:.4f}")
            log.info("Model retrain REJECTED: candidate=%.4f prev=%.4f tol=%.4f",
                     cand_loss, prev_loss, self.cfg.model_promote_max_regression)

        self.state.record_model_meta(
            iso(utcnow()), n, cand_loss if cand_loss != float("inf") else None,
            result["promoted"],
        )
        self.state.set_runtime("last_retrain_ts", iso(utcnow()))
        self.state.set_runtime("model_n_samples", n)
        return result
