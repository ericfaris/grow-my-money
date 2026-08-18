"""RiskManager — THE single chokepoint every order intent passes through.

This is the most safety-critical module. Every order (paper AND live) calls
``RiskManager.check(intent)`` and may only proceed on an ``approve`` or
``resize`` decision. The checks run in a FIXED order and every one FAILS CLOSED:
if any input needed for a check cannot be evaluated, the order is rejected and
the reason logged loudly.

Ordered checks (plan section 2.7):
  1. Kill switch (re-read here, immediately before any order).
  2. Portfolio-value halt (persistent trip flag; stays tripped until human resume).
  3. Per-position cap (resize down to headroom; reject if headroom < min order).
  4. Rolling-24h trade-count cap (trailing now-24h, reject — never queue).
Any un-evaluable input -> reject.

A fifth, DELIBERATELY FAIL-**OPEN** step (news-sentiment dampener/veto, buys
only) runs next. It is the sole exception to the fail-closed rule above: it
catches ALL of its own exceptions internally and proceeds unchanged on any
error, so a sentiment/API bug can never block a buy (the opposite of fail-safe
here would be silently rejecting every buy). It only ever softens or vetoes a
buy the four hard checks already approved; it never relaxes any of them.

A final, fail-closed minimum-order-size floor (``RiskConfig.min_order_usd``)
runs last, after any sentiment dampening — it rejects dust orders (mostly/only
fee) on either side, including ones shrunk below the floor by the dampener
itself, or by the per-position resize. Flatten sells are exempt, matching the
trade-count cap, so an emergency de-risk sell can never get stuck behind a
residual too small to clear the floor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

PriceSource = Callable[[str], float]


@dataclass
class TradeIntent:
    product: str
    side: str                    # 'buy' | 'sell'
    notional: float              # USD notional requested (buys) — for sells, notional to sell
    reason: str = ""             # signal/cap reason, propagated to the trade row
    base_size: Optional[float] = None  # for sells: coin qty (preferred); computed if None
    is_flatten: bool = False     # emergency de-risk sell: exempt from trade-count cap


@dataclass
class RiskDecision:
    action: str                  # 'approve' | 'resize' | 'reject'
    adjusted_notional: float
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action in ("approve", "resize")


HALT_FLAG = "portfolio_halt_tripped"


class RiskManager:
    def __init__(self, config, state, portfolio, killswitch, price_source: PriceSource,
                 *, sentiment=None):
        self.cfg = config                 # Config (has .risk RiskConfig)
        self.rc = config.risk
        self.state = state
        self.portfolio = portfolio
        self.killswitch = killswitch
        self.price_source = price_source
        # Optional, keyword-only, fail-OPEN news-sentiment provider. None -> inert.
        self.sentiment = sentiment

    def check(self, intent: TradeIntent, now=None) -> RiskDecision:
        try:
            return self._check(intent, now=now)
        except Exception as exc:  # fail-closed: any un-evaluable input rejects
            log.warning("RISK fail-closed: could not evaluate %s %s (%s) -> REJECT: %s",
                        intent.side, intent.product, intent.notional, exc)
            return RiskDecision("reject", 0.0, f"fail-closed: {exc}")

    def _check(self, intent: TradeIntent, now=None) -> RiskDecision:
        # 1) Kill switch — re-read immediately before any order.
        if self.killswitch.is_engaged():
            log.warning("RISK reject %s %s: KILL SWITCH engaged", intent.side, intent.product)
            return RiskDecision("reject", 0.0, "kill switch engaged")

        is_buy = intent.side == "buy"

        # Sells that are de-risking always proceed past halt & count caps... but
        # ONLY flatten sells are exempt from the trade-count cap (section 2.8).
        # Ordinary sells still honor the trade-count cap.

        # 2) Portfolio-value halt (buys only). Persistent trip.
        if is_buy:
            halt = self._check_halt()
            if halt is not None:
                return halt

        # 3) Per-position cap (buys only) — resize to headroom.
        if is_buy:
            resized = self._check_per_position(intent)
            if resized.action == "reject":
                return resized
            # carry the possibly-resized notional forward
            working_notional = resized.adjusted_notional
            resize_reason = resized.reason if resized.action == "resize" else ""
        else:
            working_notional = intent.notional
            resize_reason = ""

        # 3.5) Cash sufficiency (buys, paper only). Live orders self-limit at
        # the exchange (an over-budget market buy is simply rejected there);
        # portfolio.cash in live mode is display-only and fails soft to 0.0 on
        # a fetch glitch, so gating live buys on it here would be unsafe.
        # Without this, the per-trade budget is a fixed fraction of the
        # ORIGINAL bankroll (see bot.py), not of remaining cash — nothing
        # else stops repeated buys from driving paper cash negative.
        if is_buy and self.state.get_mode("paper") == "paper":
            cash_checked = self._check_cash(working_notional, intent.product)
            if cash_checked.action == "reject":
                return cash_checked
            if cash_checked.action == "resize":
                working_notional = cash_checked.adjusted_notional
                resize_reason = (f"{resize_reason}; {cash_checked.reason}"
                                  if resize_reason else cash_checked.reason)

        # 4) Rolling-24h trade-count cap. Flatten sells are exempt.
        if not (intent.is_flatten and not is_buy):
            count = self.state.trades_in_last_24h(now=now)
            if count >= self.rc.max_trades_per_24h:
                log.warning("RISK reject %s %s: trade-count cap %d/24h reached (%d)",
                            intent.side, intent.product, self.rc.max_trades_per_24h, count)
                return RiskDecision("reject", 0.0,
                                    f"daily trade cap {self.rc.max_trades_per_24h}/24h reached")

        # 5) News-sentiment dampener/veto (buys only; FAIL-OPEN — see docstring).
        # This block is DELIBERATELY the fail-open exception: it wraps its own
        # consultation in try/except so it can NEVER reach check()'s fail-closed
        # outer handler. A bug here must proceed, never reject.
        if is_buy and self.sentiment is not None:
            try:
                res = self.sentiment.lean(intent.product)
                if res is not None:
                    if res.score <= self.cfg.sentiment_veto_score:
                        panic_txt = (f", panic={res.panic:.0f}"
                                     if res.panic is not None else "")
                        log.warning(
                            "RISK reject buy %s: bearish sentiment (score=%.2f%s)",
                            intent.product, res.score, panic_txt,
                        )
                        return RiskDecision(
                            "reject", 0.0,
                            f"rejected: bearish sentiment (score={res.score:.2f}{panic_txt})",
                        )
                    if res.score <= self.cfg.sentiment_dampen_score:
                        factor = self.cfg.sentiment_dampen_factor
                        working_notional *= factor
                        dampen_reason = (
                            f"resized: bearish sentiment (score={res.score:.2f}) "
                            f"x{factor:.2f}"
                        )
                        resize_reason = (
                            f"{resize_reason}; {dampen_reason}"
                            if resize_reason else dampen_reason
                        )
                        log.warning(
                            "RISK resize buy %s: bearish sentiment (score=%.2f) "
                            "-> notional x%.2f = %.2f",
                            intent.product, res.score, factor, working_notional,
                        )
            except Exception as exc:  # noqa: BLE001 — FAIL-OPEN, never reject
                log.warning(
                    "RISK sentiment step failed for %s (%s); proceeding without "
                    "dampening", intent.product, exc,
                )

        # 6) Minimum order-size floor — final check, after any sentiment
        # dampening. Catches dust orders regardless of what shrank them.
        # Flatten sells are exempt (never block an emergency de-risk sell).
        if not (intent.is_flatten and not is_buy):
            if working_notional < self.rc.min_order_usd:
                log.warning("RISK reject %s %s: notional %.2f below min order %.2f",
                            intent.side, intent.product, working_notional,
                            self.rc.min_order_usd)
                return RiskDecision(
                    "reject", 0.0,
                    f"below minimum order size ({self.rc.min_order_usd:.2f})")

        action = "resize" if resize_reason else "approve"
        reason = resize_reason or "approved"
        return RiskDecision(action, working_notional, reason)

    # -- individual checks -------------------------------------------------
    def _check_halt(self) -> Optional[RiskDecision]:
        # Already-tripped stays tripped until a human resumes (fail-closed).
        if self.state.is_cap_tripped(HALT_FLAG):
            log.warning("RISK reject buy: portfolio halt already tripped (awaiting resume)")
            return RiskDecision("reject", 0.0, "portfolio halt tripped (awaiting resume)")

        if self.evaluate_portfolio_halt():
            return RiskDecision("reject", 0.0, "portfolio drawdown halt tripped")
        return None

    def evaluate_portfolio_halt(self) -> bool:
        """Mark-to-market the portfolio and trip the persistent halt flag if it is
        below the halt threshold. Returns True if the halt is (now or already)
        tripped. Raises on an un-markable portfolio so callers fail closed.

        Callable independently of a buy so the bot can detect the halt at cycle
        top (e.g. to auto-flatten when HALT_AUTO_FLATTEN is enabled)."""
        if self.state.is_cap_tripped(HALT_FLAG):
            return True
        bankroll = self.portfolio.bankroll
        total = self.portfolio.total_value(self.price_source)  # may raise -> fail-closed
        threshold = self.rc.portfolio_halt_fraction * bankroll
        if total < threshold:
            self.state.set_cap_trip(HALT_FLAG, True)
            log.warning(
                "RISK PORTFOLIO HALT TRIPPED: total=%.2f < %.2f (%.0f%% of bankroll %.2f); "
                "rejecting all buys until human resume",
                total, threshold, self.rc.portfolio_halt_fraction * 100, bankroll,
            )
            return True
        return False

    def _check_per_position(self, intent: TradeIntent) -> RiskDecision:
        bankroll = self.portfolio.bankroll
        cap = self.rc.per_position_fraction * bankroll
        current = self.portfolio.position_value(intent.product, self.price_source)
        headroom = cap - current
        requested = intent.notional

        if requested <= headroom + 1e-9:
            return RiskDecision("approve", requested, "within per-position cap")

        if headroom < self.rc.min_order_usd:
            log.warning(
                "RISK reject buy %s: no per-position headroom (pos=%.2f cap=%.2f "
                "headroom=%.2f < min %.2f)",
                intent.product, current, cap, headroom, self.rc.min_order_usd,
            )
            return RiskDecision("reject", 0.0, "per-position cap: headroom below min order")

        log.warning(
            "RISK resize buy %s: requested %.2f -> %.2f (per-position cap %.2f, pos %.2f)",
            intent.product, requested, headroom, cap, current,
        )
        return RiskDecision("resize", headroom, f"resized to per-position headroom {headroom:.2f}")

    def _check_cash(self, notional: float, product: str) -> RiskDecision:
        available = self.portfolio.cash
        fee_rate = self.cfg.fee_bps / 10_000.0
        required = notional * (1.0 + fee_rate)

        if required <= available + 1e-9:
            return RiskDecision("approve", notional, "within available cash")

        max_notional = available / (1.0 + fee_rate)
        if max_notional < self.rc.min_order_usd:
            log.warning(
                "RISK reject buy %s: insufficient paper cash (available=%.2f, "
                "required=%.2f)", product, available, required,
            )
            return RiskDecision("reject", 0.0, "insufficient paper cash")

        log.warning(
            "RISK resize buy %s: requested %.2f -> %.2f (paper cash available %.2f)",
            product, notional, max_notional, available,
        )
        return RiskDecision(
            "resize", max_notional, f"resized to available cash {max_notional:.2f}")
