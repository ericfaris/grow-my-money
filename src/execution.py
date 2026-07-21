"""OrderGateway — the split between paper simulation and live orders.

Every order (paper AND live) first calls ``RiskManager.check(intent)``. Nothing
places a fill without an ``approve``/``resize`` decision — this is enforced with
an assertion. The **live branch is the only place in the codebase that calls a
real Coinbase order endpoint**, guarded by an ``assert mode == 'live'``.
"""
from __future__ import annotations

import logging
import uuid

from .risk import RiskDecision, TradeIntent
from .state import iso, utcnow

log = logging.getLogger(__name__)


class PaperFillSimulator:
    """Simulates a fill at the current best bid/ask +/- slippage.

    Slippage scales with a product's 24h volume (see ``volume_source``, wired
    from the same discovery data used to pick the tradeable universe —
    src/product_discovery.py). ``slippage_bps`` alone is calibrated for a
    BTC/ETH-level book; the discovery floor now admits pairs down to
    ~$5M/day, where a flat 5bps assumption would understate real market-order
    slippage and make paper P&L look better than live could actually achieve.
    Thinner volume -> larger multiplier, capped at ``max_slippage_multiplier``.
    Unknown volume (no ``volume_source``, or it returns nothing) falls back to
    1x — i.e. today's flat behavior, never assumed worse than that."""

    def __init__(self, price_source, slippage_bps: float, fee_bps: float,
                volume_source=None, liquidity_reference_volume: float = 50_000_000.0,
                max_slippage_multiplier: float = 5.0):
        self.price_source = price_source
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.volume_source = volume_source
        self.liquidity_reference_volume = liquidity_reference_volume
        self.max_slippage_multiplier = max_slippage_multiplier

    def _slippage_multiplier(self, product: str) -> float:
        if self.volume_source is None:
            return 1.0
        try:
            volume = self.volume_source(product)
        except Exception:  # noqa: BLE001 — never let a lookup break a fill
            return 1.0
        if not volume or volume <= 0:
            return 1.0
        multiplier = (self.liquidity_reference_volume / volume) ** 0.5
        return max(1.0, min(multiplier, self.max_slippage_multiplier))

    def fill(self, product: str, side: str, notional: float, base_size=None):
        spot = self.price_source(product)
        slip = (self.slippage_bps * self._slippage_multiplier(product)) / 10_000.0
        # buys pay up, sells receive down (adverse slippage)
        price = spot * (1 + slip) if side == "buy" else spot * (1 - slip)
        if side == "buy":
            base = notional / price
        else:
            base = base_size if base_size is not None else notional / price
            notional = base * price
        fee = notional * (self.fee_bps / 10_000.0)
        return {"price": price, "base_size": base, "notional": notional, "fee": fee}


class OrderGateway:
    def __init__(self, config, state, risk_manager, price_source, coinbase_client=None,
                volume_source=None):
        self.cfg = config
        self.state = state
        self.risk = risk_manager
        self.price_source = price_source
        self.client = coinbase_client
        self.sim = PaperFillSimulator(
            price_source, config.slippage_bps, config.fee_bps,
            volume_source=volume_source,
            liquidity_reference_volume=config.slippage_liquidity_reference_volume,
            max_slippage_multiplier=config.slippage_max_multiplier)

    def execute(self, intent: TradeIntent, now=None) -> dict:
        """Risk-check then route to paper simulator or live order.

        Returns a result dict {'status', 'decision', 'fill'?}. Records the intent
        row (with a deterministic client_order_id) BEFORE any live call for crash
        idempotency.
        """
        mode = self.state.get_mode("paper")
        client_order_id = str(uuid.uuid4())

        # 1) Persist the intent BEFORE anything else (crash idempotency).
        self.state.record_intent(
            client_order_id=client_order_id,
            product=intent.product,
            side=intent.side,
            mode=mode,
            requested_notional=intent.notional,
        )

        # 2) Risk chokepoint — no fill may proceed without approve/resize.
        decision: RiskDecision = self.risk.check(intent, now=now)
        self.state.update_intent_risk(
            client_order_id, decision.action, decision.adjusted_notional, decision.reason
        )
        if not decision.allowed:
            self.state.set_intent_status(client_order_id, "rejected")
            log.info("Order rejected: %s %s (%s)", intent.side, intent.product, decision.reason)
            return {"status": "rejected", "decision": decision, "client_order_id": client_order_id}

        # HARD GUARD: never fill without an approve/resize RiskDecision.
        assert decision.allowed, "OrderGateway reached fill without an allowed RiskDecision"

        notional = decision.adjusted_notional
        try:
            if mode == "paper":
                fill = self._paper_fill(intent, notional)
            else:
                fill = self._live_fill(mode, client_order_id, intent, notional)
        except Exception as exc:  # noqa: BLE001
            self.state.set_intent_status(client_order_id, "error")
            log.warning("Order execution error for %s %s: %s", intent.side, intent.product, exc)
            return {"status": "error", "decision": decision, "error": str(exc),
                    "client_order_id": client_order_id}

        self.state.record_fill(
            client_order_id=client_order_id,
            product=intent.product,
            side=intent.side,
            mode=mode,
            base_size=fill["base_size"],
            price=fill["price"],
            notional=fill["notional"],
            fee=fill["fee"],
            ts_utc=iso(now) if now else iso(utcnow()),
        )
        # keep paper cash in sync
        if mode == "paper":
            self._update_paper_cash(intent.side, fill)
        log.info("Filled %s %s: base=%.6f price=%.2f notional=%.2f mode=%s reason=%s",
                 intent.side, intent.product, fill["base_size"], fill["price"],
                 fill["notional"], mode, intent.reason)
        return {"status": "filled", "decision": decision, "fill": fill,
                "client_order_id": client_order_id}

    def _paper_fill(self, intent: TradeIntent, notional: float) -> dict:
        return self.sim.fill(intent.product, intent.side, notional, base_size=intent.base_size)

    def _live_fill(self, mode: str, client_order_id: str, intent: TradeIntent,
                   notional: float) -> dict:
        # The ONLY real-order call site. Hard-guarded on mode.
        assert mode == "live", "live fill reached with non-live mode"
        if self.client is None:
            raise RuntimeError("live mode requires a Coinbase client")
        spot = self.price_source(intent.product)
        if intent.side == "buy":
            resp = self.client.place_market_buy(client_order_id, intent.product, notional)
            base = notional / spot
        else:
            base = intent.base_size if intent.base_size is not None else notional / spot
            resp = self.client.place_market_sell(client_order_id, intent.product, base)
            notional = base * spot
        fee = notional * (self.cfg.fee_bps / 10_000.0)
        log.info("LIVE order response: %s", _safe_order_summary(resp))
        return {"price": spot, "base_size": base, "notional": notional, "fee": fee}

    def _update_paper_cash(self, side: str, fill: dict) -> None:
        cash = self.state.get_runtime("paper_cash")
        cash = float(cash) if cash is not None else self.cfg.paper_start_bankroll
        if side == "buy":
            cash -= fill["notional"] + fill["fee"]
        else:
            cash += fill["notional"] - fill["fee"]
        self.state.set_runtime("paper_cash", cash)


def _safe_order_summary(resp) -> str:
    if isinstance(resp, dict):
        return str({k: resp.get(k) for k in ("success", "order_id", "product_id") if k in resp})
    return str(resp)[:200]
