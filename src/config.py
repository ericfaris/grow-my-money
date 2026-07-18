"""Configuration loading and validation.

Loads all tunables from the environment (optionally seeded by a local `.env`
via python-dotenv) into immutable dataclasses. The single most important
guarantee here is **fail-closed defaulting**: any missing or invalid ``MODE``
resolves to ``paper``, and the risk caps are validated to sane fractions. No
secrets are read here — credential file *paths* only (see ``secrets.py``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import List

try:  # python-dotenv is optional at import time (always present in prod)
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_a, **_k):
        return False


VALID_MODES = ("paper", "live")


def _get(env: dict, key: str, default: str) -> str:
    val = env.get(key)
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip()


def _get_float(env: dict, key: str, default: float) -> float:
    try:
        return float(_get(env, key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(env: dict, key: str, default: int) -> int:
    try:
        return int(float(_get(env, key, str(default))))
    except (TypeError, ValueError):
        return default


def _get_bool(env: dict, key: str, default: bool) -> bool:
    raw = _get(env, key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _coerce_mode(raw: str | None) -> str:
    """Any missing/garbage mode fails closed to ``paper``."""
    if raw is None:
        return "paper"
    val = str(raw).strip().lower()
    return val if val in VALID_MODES else "paper"


@dataclass(frozen=True)
class RiskConfig:
    """Hard safety caps. All order intents pass through these thresholds."""

    portfolio_halt_fraction: float = 0.70
    max_trades_per_24h: int = 5
    min_order_usd: float = 10.0
    halt_auto_flatten: bool = False

    def validate(self) -> None:
        if not (0.0 < self.portfolio_halt_fraction < 1.0):
            raise ValueError("PORTFOLIO_HALT_FRACTION must be in (0,1)")
        if self.max_trades_per_24h < 0:
            raise ValueError("MAX_TRADES_PER_24H must be >= 0")
        if self.min_order_usd < 0:
            raise ValueError("MIN_ORDER_USD must be >= 0")


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    paper_start_bankroll: float = 10_000.0

    products: List[str] = field(default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD"])
    candle_granularity: str = "ONE_HOUR"
    candle_limit: int = 300

    decision_interval_min: int = 30
    daily_report_hour: int = 8

    slippage_bps: float = 5.0
    fee_bps: float = 60.0
    per_trade_budget_fraction: float = 0.10

    model_horizon_hours: int = 6
    buy_probability_threshold: float = 0.55
    model_min_train_samples: int = 40
    retrain_every_n_closed_trades: int = 10
    model_promote_max_regression: float = 0.02

    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_from: str = ""
    report_recipient: str = ""

    coinbase_key_file: str = ""
    smtp_creds_file: str = ""

    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8420
    dashboard_refresh_sec: int = 15
    dashboard_price_ttl_sec: int = 20

    # News/sentiment (fail-open; inert until a CryptoPanic token file exists).
    cryptopanic_token_file: str = ""
    sentiment_enabled: bool = True
    sentiment_ttl_sec: int = 900
    sentiment_api_base: str = "https://cryptopanic.com/api/developer/v2/posts/"
    sentiment_dampen_score: float = -0.3
    sentiment_veto_score: float = -0.6
    sentiment_dampen_factor: float = 0.5
    sentiment_timeout_sec: float = 4.0

    # Volume confirmation + higher-timeframe (HTF) trend dampener (buys only).
    volume_avg_window: int = 20
    volume_confirm_ratio: float = 1.2
    volume_thin_ratio: float = 0.7
    volume_thin_factor: float = 0.6
    htf_candle_granularity: str = "SIX_HOUR"
    htf_disagree_factor: float = 0.5

    risk: RiskConfig = field(default_factory=RiskConfig)

    def validate(self) -> None:
        self.risk.validate()
        if self.paper_start_bankroll <= 0:
            raise ValueError("PAPER_START_BANKROLL must be > 0")
        if not self.products:
            raise ValueError("PRODUCTS must not be empty")


def load_config(env: dict | None = None, use_dotenv: bool = True) -> Config:
    """Build a validated ``Config`` from ``env`` (defaults to ``os.environ``).

    ``MODE`` fails closed to ``paper``. Note this only seeds a fresh state DB;
    the persisted mode in SQLite is the runtime source of truth (see state.py).
    """
    if use_dotenv:
        load_dotenv()
    if env is None:
        env = dict(os.environ)

    products = [p.strip() for p in _get(env, "PRODUCTS", "BTC-USD,ETH-USD,SOL-USD").split(",") if p.strip()]

    risk = RiskConfig(
        portfolio_halt_fraction=_get_float(env, "PORTFOLIO_HALT_FRACTION", 0.70),
        max_trades_per_24h=_get_int(env, "MAX_TRADES_PER_24H", 5),
        min_order_usd=_get_float(env, "MIN_ORDER_USD", 10.0),
        halt_auto_flatten=_get_bool(env, "HALT_AUTO_FLATTEN", False),
    )

    cfg = Config(
        mode=_coerce_mode(env.get("MODE")),
        paper_start_bankroll=_get_float(env, "PAPER_START_BANKROLL", 10_000.0),
        products=products,
        candle_granularity=_get(env, "CANDLE_GRANULARITY", "ONE_HOUR"),
        candle_limit=_get_int(env, "CANDLE_LIMIT", 300),
        decision_interval_min=_get_int(env, "DECISION_INTERVAL_MIN", 30),
        daily_report_hour=_get_int(env, "DAILY_REPORT_HOUR", 8),
        slippage_bps=_get_float(env, "SLIPPAGE_BPS", 5.0),
        fee_bps=_get_float(env, "FEE_BPS", 60.0),
        per_trade_budget_fraction=_get_float(env, "PER_TRADE_BUDGET_FRACTION", 0.10),
        model_horizon_hours=_get_int(env, "MODEL_HORIZON_HOURS", 6),
        buy_probability_threshold=_get_float(env, "BUY_PROBABILITY_THRESHOLD", 0.55),
        model_min_train_samples=_get_int(env, "MODEL_MIN_TRAIN_SAMPLES", 40),
        retrain_every_n_closed_trades=_get_int(env, "RETRAIN_EVERY_N_CLOSED_TRADES", 10),
        model_promote_max_regression=_get_float(env, "MODEL_PROMOTE_MAX_REGRESSION", 0.02),
        ema_fast=_get_int(env, "EMA_FAST", 12),
        ema_slow=_get_int(env, "EMA_SLOW", 26),
        rsi_period=_get_int(env, "RSI_PERIOD", 14),
        macd_fast=_get_int(env, "MACD_FAST", 12),
        macd_slow=_get_int(env, "MACD_SLOW", 26),
        macd_signal=_get_int(env, "MACD_SIGNAL", 9),
        rsi_overbought=_get_float(env, "RSI_OVERBOUGHT", 70.0),
        rsi_oversold=_get_float(env, "RSI_OVERSOLD", 30.0),
        smtp_host=_get(env, "SMTP_HOST", "smtp.gmail.com"),
        smtp_port=_get_int(env, "SMTP_PORT", 587),
        smtp_from=_get(env, "SMTP_FROM", ""),
        report_recipient=_get(env, "REPORT_RECIPIENT", ""),
        coinbase_key_file=_get(env, "COINBASE_KEY_FILE", ""),
        smtp_creds_file=_get(env, "SMTP_CREDS_FILE", ""),
        dashboard_host=_get(env, "DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=_get_int(env, "DASHBOARD_PORT", 8420),
        dashboard_refresh_sec=_get_int(env, "DASHBOARD_REFRESH_SEC", 15),
        dashboard_price_ttl_sec=_get_int(env, "DASHBOARD_PRICE_TTL_SEC", 20),
        cryptopanic_token_file=_get(env, "CRYPTOPANIC_TOKEN_FILE", ""),
        sentiment_enabled=_get_bool(env, "SENTIMENT_ENABLED", True),
        sentiment_ttl_sec=_get_int(env, "SENTIMENT_TTL_SEC", 900),
        sentiment_api_base=_get(
            env, "SENTIMENT_API_BASE",
            "https://cryptopanic.com/api/developer/v2/posts/",
        ),
        sentiment_dampen_score=_get_float(env, "SENTIMENT_DAMPEN_SCORE", -0.3),
        sentiment_veto_score=_get_float(env, "SENTIMENT_VETO_SCORE", -0.6),
        sentiment_dampen_factor=_get_float(env, "SENTIMENT_DAMPEN_FACTOR", 0.5),
        sentiment_timeout_sec=_get_float(env, "SENTIMENT_TIMEOUT_SEC", 4.0),
        volume_avg_window=_get_int(env, "VOLUME_AVG_WINDOW", 20),
        volume_confirm_ratio=_get_float(env, "VOLUME_CONFIRM_RATIO", 1.2),
        volume_thin_ratio=_get_float(env, "VOLUME_THIN_RATIO", 0.7),
        volume_thin_factor=_get_float(env, "VOLUME_THIN_FACTOR", 0.6),
        htf_candle_granularity=_get(env, "HTF_CANDLE_GRANULARITY", "SIX_HOUR"),
        htf_disagree_factor=_get_float(env, "HTF_DISAGREE_FACTOR", 0.5),
        risk=risk,
    )
    cfg.validate()
    return cfg
