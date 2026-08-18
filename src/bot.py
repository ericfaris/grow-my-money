"""Main run loop wiring everything together (daemon entry point).

Cycle: check kill -> fetch candles -> indicators -> model -> intent -> gateway
(risk chokepoint) -> persist. Ensures the paper-start benchmark anchor is created
on the first paper cycle. A daily timer fires the email + nightly model
maintenance. On startup it rehydrates persisted state (mode, halt flag, benchmark
anchor) and reconciles any pending LIVE intents against Coinbase before acting.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import email_report, marketdata, signals
from .benchmark import Benchmark
from .config import Config, load_config
from .coinbase_client import RateLimited
from .execution import OrderGateway
from .indicators import compute_features
from .killswitch import KillSwitch
from .logging_setup import setup_logging
from .model import Model
from .portfolio import Portfolio
from . import product_discovery
from .risk import RiskManager, TradeIntent
from .sentiment import SentimentProvider
from .scheduler import Scheduler
from .state import State, iso, utcnow

log = logging.getLogger(__name__)

STATE_DIR = Path("state")
DB_PATH = STATE_DIR / "grow.db"
MODEL_PATH = STATE_DIR / "model.pkl"
KILL_PATH = STATE_DIR / "KILL"

BENCH_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")


class Bot:
    def __init__(self, config: Config, state: State, coinbase_client=None,
                 state_dir: Path = STATE_DIR):
        self.cfg = config
        self.state = state
        self.client = coinbase_client
        self.state_dir = Path(state_dir)
        self.killswitch = KillSwitch(self.state_dir / "KILL")
        self.portfolio = Portfolio(state, config.paper_start_bankroll,
                                   coinbase_client=coinbase_client)
        self.benchmark = Benchmark(state)
        self.model = Model(config, state, self.state_dir / "model.pkl")
        self.scheduler = Scheduler(config.decision_interval_min, config.daily_report_hour)
        self._price_cache: dict[str, float] = {}
        # Dynamic product universe (see product_discovery.py). Starts as the
        # static config fallback; _refresh_products() replaces it once
        # discovery succeeds. Never mutated to empty — always fail open to
        # whatever list is already here.
        self.products: list[str] = list(config.products)
        self.product_volumes: dict[str, float] = {}  # product_id -> approx 24h USD volume
        # Fail-open news-sentiment provider; injected only when enabled. Inert
        # (fails open) until a CryptoPanic token file is present.
        self.sentiment = SentimentProvider(config) if config.sentiment_enabled else None
        self.risk = RiskManager(config, state, self.portfolio, self.killswitch,
                                self.price_source, sentiment=self.sentiment)
        self.gateway = OrderGateway(config, state, self.risk, self.price_source,
                                    coinbase_client=coinbase_client,
                                    volume_source=lambda p: self.product_volumes.get(p))

    # -- price source shared by portfolio, risk, benchmark -----------------
    def price_source(self, product: str) -> float:
        if product in self._price_cache:
            return self._price_cache[product]
        if self.client is None:
            raise RuntimeError(f"no price available for {product} (no client)")
        price = self.client.get_spot_price(product)
        self._price_cache[product] = price
        return price

    def refresh_prices(self) -> dict[str, float]:
        self._price_cache = {}
        prices = {}
        for p in BENCH_PRODUCTS:
            try:
                prices[p] = self.price_source(p)
            except Exception as exc:
                log.warning("price fetch failed for %s: %s", p, exc)
        return prices

    # -- startup rehydrate + reconcile -------------------------------------
    def rehydrate(self) -> None:
        mode = self.state.get_mode(self.cfg.mode)
        # seed persisted mode from config default only if absent
        if self.state.get_runtime("mode") is None:
            self.state.set_mode(self.cfg.mode)
            mode = self.cfg.mode
        log.info("Startup: mode=%s halt_tripped=%s kill=%s", mode,
                 self.state.is_cap_tripped("portfolio_halt_tripped"),
                 self.killswitch.is_engaged())
        self._reconcile_pending_live()
        self._refresh_products()

    def _refresh_products(self) -> None:
        """Re-discover the traded product universe from Coinbase's live
        catalog (see product_discovery.py). Fail-open: any error or empty
        result leaves self.products exactly as it was — never trades nothing
        because a discovery call had a bad day."""
        if not self.cfg.product_discovery_enabled or self.client is None:
            return
        try:
            discovered, volumes = product_discovery.discover(
                self.client, self.cfg.product_min_quote_volume_24h,
                self.cfg.product_discovery_max_count)
            log.info("Product discovery: %d products (was %d)",
                     len(discovered), len(self.products))
            self.products = discovered
            self.product_volumes = volumes
        except Exception as exc:  # noqa: BLE001
            log.warning("Product discovery failed (%s); keeping existing %d products",
                        exc, len(self.products))

    def _reconcile_pending_live(self) -> None:
        """Before any new activity, reconcile pending LIVE intents against
        Coinbase by client_order_id (idempotency — never double-submit)."""
        if self.client is None:
            return
        for row in self.state.pending_intents(mode="live"):
            coid = row["client_order_id"]
            try:
                order = self.client.get_order(coid)
                status = str(order.get("status", "")).lower()
                if status in ("filled", "done", "settled"):
                    self.state.set_intent_status(coid, "filled")
                    log.warning("Reconciled pending live intent %s -> filled", coid)
                elif status in ("cancelled", "rejected", "failed", "expired"):
                    self.state.set_intent_status(coid, "rejected")
                    log.warning("Reconciled pending live intent %s -> rejected", coid)
                else:
                    log.warning("Pending live intent %s still %s; leaving pending", coid, status)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not reconcile pending intent %s: %s (leaving pending)",
                            coid, exc)

    # -- one decision cycle ------------------------------------------------
    def run_cycle(self, now=None) -> dict:
        now = now or utcnow()
        summary = {"actions": [], "kill": False}
        if self.killswitch.is_engaged():
            log.warning("Kill switch engaged — skipping cycle (no orders)")
            summary["kill"] = True
            return summary

        mode = self.state.get_mode("paper")
        prices = self.refresh_prices()
        self.portfolio.refresh_live_balances()

        try:
            self._evaluate_due_outcomes(now=now)
        except Exception as exc:  # noqa: BLE001 — report-only, never blocks trading
            log.warning("outcome evaluation failed (%s); continuing cycle", exc)

        # Paper-start benchmark anchor: create on first paper cycle if absent.
        if mode == "paper":
            if all(p in prices for p in BENCH_PRODUCTS):
                self.benchmark.ensure_anchor("paper", self.cfg.paper_start_bankroll, prices)

        # Portfolio-halt detection at cycle top. On trip, buys are already blocked
        # by the risk chokepoint; if HALT_AUTO_FLATTEN is enabled, also de-risk to
        # cash (emergency sells route through the gateway, exempt from the trade-
        # count cap but NOT from the kill switch).
        try:
            if self.risk.evaluate_portfolio_halt():
                summary["halt"] = True
                if self.cfg.risk.halt_auto_flatten:
                    summary["flattened"] = self._flatten_all(now=now)
                return summary
        except Exception as exc:  # fail-closed: skip trading this cycle
            log.warning("halt evaluation failed (%s); skipping cycle", exc)
            return summary

        # Evaluate the discovered universe, plus any open position even if its
        # product has since dropped out of the liquidity filter — an existing
        # position must always stay sell-evaluated, never go unmanaged.
        cycle_products = sorted(set(self.products) | set(self.state.open_positions().keys()))
        for product in cycle_products:
            try:
                action = self._decide_product(product, now=now)
                if action:
                    summary["actions"].append(action)
            except RateLimited:
                log.warning("Rate limited on %s — failing closed for this cycle", product)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("cycle error for %s: %s", product, exc)
        return summary

    def _htf_trend(self, product: str) -> Optional[bool]:
        """Higher-timeframe trend bit for the buy-size dampener. Fail-OPEN:
        any failure returns None (dampener simply not applied)."""
        try:
            df = marketdata.fetch_candles(
                self.client, product, self.cfg.htf_candle_granularity,
                self.cfg.candle_limit)
            if df.empty:
                return None
            f = compute_features(
                df, ema_fast=self.cfg.ema_fast, ema_slow=self.cfg.ema_slow,
                rsi_period=self.cfg.rsi_period, macd_fast=self.cfg.macd_fast,
                macd_slow=self.cfg.macd_slow, macd_signal=self.cfg.macd_signal,
                volume_avg_window=self.cfg.volume_avg_window)
            return bool(f.ema_bullish)
        except Exception as exc:  # noqa: BLE001 — fail-open
            log.warning("HTF trend fetch failed for %s (%s); proceeding without "
                        "HTF dampening", product, exc)
            return None

    def _decide_product(self, product: str, now=None) -> Optional[dict]:
        df = marketdata.fetch_candles(self.client, product, self.cfg.candle_granularity,
                                      self.cfg.candle_limit)
        if df.empty:
            return None
        feats = compute_features(
            df, ema_fast=self.cfg.ema_fast, ema_slow=self.cfg.ema_slow,
            rsi_period=self.cfg.rsi_period, macd_fast=self.cfg.macd_fast,
            macd_slow=self.cfg.macd_slow, macd_signal=self.cfg.macd_signal,
            volume_avg_window=self.cfg.volume_avg_window,
        )
        sig = signals.evaluate(product, feats, self.cfg)

        if sig.direction == "hold":
            return None

        if sig.direction == "sell":
            pos = self.state.open_positions().get(product)
            if not pos or pos["base_size"] <= 0:
                return None  # nothing to sell; never gated by model
            opened = pos.get("opened_ts_utc")
            if opened:
                held_hours = ((now or utcnow()) - datetime.fromisoformat(opened)).total_seconds() / 3600.0
                if held_hours < self.cfg.min_hold_hours:
                    # Ordinary (non-flatten) exit signal firing before the
                    # position has had time to develop is a whipsaw, not a
                    # real reversal call — it just pays the round-trip fee
                    # for a move that never got a chance to happen. Emergency
                    # de-risk (flatten) sells are a separate code path and are
                    # NOT subject to this throttle.
                    log.info("SELL suppressed %s: held %.1fh < min hold %.1fh",
                             product, held_hours, self.cfg.min_hold_hours)
                    return None
            intent = TradeIntent(product=product, side="sell",
                                 notional=pos["base_size"] * feats.price,
                                 base_size=pos["base_size"], reason=sig.reason)
            res = self.gateway.execute(intent, now=now)
            return {"product": product, "side": "sell", "status": res["status"],
                    "reason": sig.reason}

        # BUY: liquidity/data-quality screen, then model gate + size scaling
        if feats.volatility > self.cfg.max_buy_volatility:
            log.info("BUY suppressed %s: volatility %.4f > max %.4f",
                     product, feats.volatility, self.cfg.max_buy_volatility)
            return None
        p_win, tag = self.model.predict_p_win(feats)
        if p_win < self.cfg.buy_probability_threshold:
            log.info("BUY suppressed %s: p_win=%.3f < %.3f (model=%s)",
                     product, p_win, self.cfg.buy_probability_threshold, tag)
            return None
        htf_bullish = self._htf_trend(product)
        if htf_bullish is False:
            # Hard-gate on higher-timeframe trend instead of just dampening size.
            # Every closed trade so far lost regardless of how strong the local
            # RSI/EMA-gap confirmation was (losses at RSI 55 and RSI 70 alike),
            # which points at broader-trend headwind rather than a bad local
            # entry. A soft 0.5x dampener still let the bot buy into that.
            log.info("BUY suppressed %s: HTF trend bearish", product)
            return None
        size_factor = signals.buy_size_factor(feats.volume_ratio, htf_bullish, self.cfg)
        budget = self.cfg.per_trade_budget_fraction * self.portfolio.bankroll
        # scale by conviction over the threshold
        scale = min(1.0, (p_win - self.cfg.buy_probability_threshold)
                    / max(1e-6, 1.0 - self.cfg.buy_probability_threshold) + 0.5)
        notional = budget * scale * size_factor
        htf_txt = "n/a" if htf_bullish is None else ("bull" if htf_bullish else "bear")
        reason = (f"{sig.reason}; p_win={p_win:.3f} model={tag}; "
                  f"vol_ratio={feats.volume_ratio:.2f} htf={htf_txt} size_x={size_factor:.2f}")
        intent = TradeIntent(product=product, side="buy", notional=notional, reason=reason)
        res = self.gateway.execute(intent, now=now)
        if res["status"] == "filled":
            self._record_pending_outcome(product, feats, res["fill"]["price"], now=now)
        return {"product": product, "side": "buy", "status": res["status"],
                "p_win": p_win, "reason": reason}

    def _record_pending_outcome(self, product: str, feats, entry_price: float, now=None) -> None:
        """Stash the buy's feature snapshot for horizon-based labeling later
        (see _evaluate_due_outcomes). Decoupled from how/when the position is
        eventually sold — this is what feeds the model's training data, so a
        failure here must never block the trade that already filled."""
        try:
            now = now or utcnow()
            due = now + timedelta(hours=self.cfg.model_horizon_hours)
            self.state.record_pending_outcome(
                product, iso(now), entry_price, feats.to_vector(), iso(due))
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to record pending outcome for %s: %s", product, exc)

    def _evaluate_due_outcomes(self, now=None) -> None:
        """Resolve any pending outcome whose horizon has elapsed into a labeled
        row in `outcomes`, then retrain if enough new labels have accumulated.
        Report-only w.r.t. trading: never touches an order/risk/kill path."""
        now = now or utcnow()
        due = self.state.due_pending_outcomes(iso(now))
        if not due:
            return
        roundtrip_fee = 2 * self.cfg.fee_bps / 10_000.0
        stale_cutoff = now - timedelta(hours=48)
        for row in due:
            try:
                price = self.price_source(row["product"])
            except Exception as exc:  # noqa: BLE001
                if row["due_ts_utc"] < iso(stale_cutoff):
                    log.warning("dropping stale pending outcome %s (%s): %s",
                                row["id"], row["product"], exc)
                    self.state.delete_pending_outcome(row["id"])
                continue
            try:
                pnl_pct = (price - row["entry_price"]) / row["entry_price"]
                # Label on raw direction, not on beating the fee. Gating the label
                # itself on roundtrip_fee meant "wins" required clearing ~1.2%
                # inside the horizon; with this signal's typical edge, that never
                # happened, every outcome landed label=0, and the classifier could
                # never train (needs both classes). The fee threshold still lives
                # at decision time via buy_probability_threshold.
                label = 1 if pnl_pct > 0 else 0
                self.state.record_outcome(
                    row["product"], row["entry_ts_utc"], json.loads(row["features_json"]),
                    label, pnl_pct - roundtrip_fee)
                self.state.delete_pending_outcome(row["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to resolve pending outcome %s: %s", row["id"], exc)

        try:
            self._maybe_trigger_retrain()
        except Exception as exc:  # noqa: BLE001
            log.warning("retrain trigger check failed: %s", exc)

    def _maybe_trigger_retrain(self) -> None:
        n = self.state.outcome_count()
        last = int(self.state.get_runtime("outcomes_at_last_retrain", "0"))
        if n - last >= self.cfg.retrain_every_n_closed_trades:
            log.info("Retrain trigger: %d new outcomes since last retrain (threshold %d)",
                      n - last, self.cfg.retrain_every_n_closed_trades)
            self.model.maybe_retrain()
            self.state.set_runtime("outcomes_at_last_retrain", n)

    def _flatten_all(self, now=None) -> list[dict]:
        """Emergency de-risk: sell every open position to cash. Each sell routes
        through the gateway with is_flatten=True (exempt from the trade-count cap,
        still subject to the kill switch)."""
        results = []
        for product, pos in self.state.open_positions().items():
            if pos["base_size"] <= 0:
                continue
            try:
                price = self.price_source(product)
            except Exception as exc:  # noqa: BLE001
                log.warning("flatten: no price for %s (%s); skipping", product, exc)
                continue
            intent = TradeIntent(product=product, side="sell",
                                 notional=pos["base_size"] * price,
                                 base_size=pos["base_size"], reason="HALT auto-flatten",
                                 is_flatten=True)
            res = self.gateway.execute(intent, now=now)
            results.append({"product": product, "status": res["status"]})
            log.warning("HALT auto-flatten %s: %s", product, res["status"])
        return results

    # -- daily maintenance -------------------------------------------------
    def daily_maintenance(self) -> None:
        prices = self.refresh_prices()
        try:
            body = email_report.build_report_body(
                self.cfg, self.state, self.portfolio, self.benchmark,
                self.price_source, prices)
            email_report.send_report(self.cfg, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("daily report failed: %s", exc)
        # nightly model maintenance tick
        try:
            self.model.maybe_retrain()
        except Exception as exc:  # noqa: BLE001
            log.warning("nightly retrain failed: %s", exc)
        self._refresh_products()

    # -- run loop ----------------------------------------------------------
    def run(self, once: bool = False) -> None:
        self.rehydrate()
        while True:
            self.run_cycle()
            if self.scheduler.due_for_daily():
                self.daily_maintenance()
            if once:
                return
            self.scheduler.sleep_interval(should_stop=self.killswitch.is_engaged)


def build_bot(config: Optional[Config] = None, with_client: bool = True) -> Bot:
    """Construct a Bot with a real Coinbase client (built from the key file)."""
    cfg = config or load_config()
    state = State(DB_PATH)
    client = None
    if with_client:
        from . import secrets as secrets_mod
        from .coinbase_client import CoinbaseClient
        key_path = secrets_mod.coinbase_key_path(cfg.coinbase_key_file or None)
        client = CoinbaseClient.from_key_file(key_path)
    return Bot(cfg, state, coinbase_client=client)


def main() -> None:  # entrypoint: python -m src.bot
    setup_logging()
    cfg = load_config()
    bot = build_bot(cfg, with_client=True)
    bot.run(once=False)


if __name__ == "__main__":
    main()
