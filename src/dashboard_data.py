"""Read-only data layer for the web dashboard.

STRICTLY READ-ONLY. This module never writes to the SQLite database and never
touches an order path, a cap, or the trade loop. It reuses ``Portfolio`` and
``Benchmark`` for computation but opens its **own** ``mode=ro`` connection so it
can neither write nor lock out the bot.

Critical gotcha (see the dashboard plan, section 2.2): do NOT instantiate
``src.state.State`` here — ``State.__init__`` runs ``executescript(SCHEMA)`` and
``PRAGMA journal_mode=WAL``, both of which are writes. ``ReadOnlyState`` below
duck-types only the read methods the reused modules call; every write method is
a silent no-op.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_BTC, _ETH, _SOL = "BTC-USD", "ETH-USD", "SOL-USD"
BENCH_PRODUCTS = (_BTC, _ETH, _SOL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# ReadOnlyState — a write-incapable duck-type of the read subset of State.
# ---------------------------------------------------------------------------
class ReadOnlyState:
    """Opens its own ``mode=ro`` SQLite connection and exposes only reads.

    Read method SQL/return shapes are copied verbatim from ``src.state.State``
    (we deliberately do not import ``State`` — its ``__init__`` writes). Write
    methods (``set_runtime``/``set_cap_trip``/``set_mode``) are no-ops so reused
    modules such as ``Portfolio`` keep working without mutating the DB.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = self._connect_readonly(self.db_path)
        self.conn.row_factory = sqlite3.Row

    @staticmethod
    def _connect_readonly(db_path: str) -> sqlite3.Connection:
        """Prefer ``file:...?mode=ro``; fall back to a normal connection with a
        connection-level ``PRAGMA query_only=ON`` write ban (section 2.2)."""
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=30)
        except sqlite3.OperationalError as exc:
            log.warning("mode=ro open failed (%s); falling back to query_only", exc)
            conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.OperationalError:
            pass
        return conn

    def close(self) -> None:
        self.conn.close()

    # -- write methods: intentional silent no-ops --------------------------
    def set_runtime(self, key: str, value: Any) -> None:  # noqa: D401 - no-op
        return None

    def set_mode(self, mode: str) -> None:
        return None

    def set_cap_trip(self, name: str, tripped: bool, ts: Optional[str] = None) -> None:
        return None

    # -- runtime kv --------------------------------------------------------
    def get_runtime(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM runtime WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def get_mode(self, default: str = "paper") -> str:
        return self.get_runtime("mode", default) or default

    def is_cap_tripped(self, name: str) -> bool:
        return self.get_runtime(name, "0") == "1"

    # -- positions ---------------------------------------------------------
    def open_positions(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return {
            r["product"]: {
                "base_size": r["base_size"],
                "avg_entry": r["avg_entry"],
                "opened_ts_utc": r["opened_ts_utc"],
            }
            for r in rows
        }

    def trades_in_last_24h(self, now: Optional[datetime] = None) -> int:
        now = now or _utcnow()
        cutoff = _iso(now - timedelta(hours=24))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE ts_utc >= ?", (cutoff,)
        ).fetchone()
        return int(row["n"])

    def get_benchmark_anchor(self, epoch: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM benchmark WHERE epoch=?", (epoch,)
        ).fetchone()
        return dict(row) if row else None

    def latest_model_meta(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM model_meta ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def all_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades ORDER BY ts_utc").fetchall()

    # -- ad-hoc read helpers for the activity feed -------------------------
    def recent_trades(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades ORDER BY ts_utc DESC LIMIT ?", (limit,)
        ).fetchall()

    def recent_intents(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM intents ORDER BY ts_utc DESC LIMIT ?", (limit,)
        ).fetchall()

    def intent_by_coid(self, client_order_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM intents WHERE client_order_id=?", (client_order_id,)
        ).fetchone()

    def last_trade_price(self, product: str) -> Optional[float]:
        row = self.conn.execute(
            "SELECT price FROM trades WHERE product=? ORDER BY ts_utc DESC LIMIT 1",
            (product,),
        ).fetchone()
        return float(row["price"]) if row and row["price"] is not None else None


# ---------------------------------------------------------------------------
# PriceProvider — own cached CoinbaseClient with a fallback chain.
# ---------------------------------------------------------------------------
class PriceProvider:
    """Spot prices for mark-to-market, with a process-local TTL cache.

    Live fetch (own ``CoinbaseClient``) → last trade price → position avg_entry.
    A missing key or a failed fetch degrades per-product and is labelled
    ``last_trade`` / ``entry`` (vs ``live``) so the UI can badge it. Never
    raises to the caller.
    """

    def __init__(self, cfg, ttl_sec: Optional[int] = None):
        self.cfg = cfg
        self.ttl_sec = ttl_sec if ttl_sec is not None else getattr(
            cfg, "dashboard_price_ttl_sec", 20
        )
        self._client = None
        self._client_tried = False
        self._cache: dict[str, tuple[float, str, float]] = {}  # product -> (price, source, ts)

    def _get_client(self):
        if self._client_tried:
            return self._client
        self._client_tried = True
        try:
            from . import secrets as secrets_mod
            from .coinbase_client import CoinbaseClient
            key_path = secrets_mod.coinbase_key_path(self.cfg.coinbase_key_file or None)
            self._client = CoinbaseClient.from_key_file(key_path)
        except Exception as exc:  # noqa: BLE001 — degrade to fallback prices
            log.info("dashboard: no Coinbase client (%s); using fallback prices", exc)
            self._client = None
        return self._client

    def get(self, product: str, ro_state: Optional[ReadOnlyState] = None) -> tuple[float, str]:
        """Return ``(price, source)`` where source is live|last_trade|entry."""
        now = time.monotonic()
        cached = self._cache.get(product)
        if cached is not None and (now - cached[2]) < self.ttl_sec:
            return cached[0], cached[1]

        price: Optional[float] = None
        source = "live"
        client = self._get_client()
        if client is not None:
            try:
                price = float(client.get_spot_price(product))
                source = "live"
            except Exception as exc:  # noqa: BLE001
                log.info("dashboard: live price fetch failed for %s (%s)", product, exc)
                price = None

        if (price is None or price <= 0) and ro_state is not None:
            lt = ro_state.last_trade_price(product)
            if lt and lt > 0:
                price, source = lt, "last_trade"

        if (price is None or price <= 0) and ro_state is not None:
            pos = ro_state.open_positions().get(product)
            if pos and pos.get("avg_entry"):
                price, source = float(pos["avg_entry"]), "entry"

        if price is None or price <= 0:
            price, source = 0.0, "entry"

        self._cache[product] = (price, source, now)
        return price, source

    def price_source(self, ro_state: Optional[ReadOnlyState] = None) -> Callable[[str], float]:
        """A plain ``product -> float`` callable for Portfolio/Benchmark."""
        def src(product: str) -> float:
            return self.get(product, ro_state)[0]
        return src

    def prices_and_sources(
        self, products, ro_state: Optional[ReadOnlyState] = None
    ) -> tuple[dict[str, float], dict[str, str]]:
        prices: dict[str, float] = {}
        sources: dict[str, str] = {}
        for p in products:
            price, source = self.get(p, ro_state)
            prices[p] = price
            sources[p] = source
        return prices, sources


# ---------------------------------------------------------------------------
# Equity-curve reconstruction (pure, testable — plan section 3.4).
# ---------------------------------------------------------------------------
def reconstruct_equity(
    trades,
    start_bankroll: float,
    current_prices: dict[str, float],
    anchor_ts: Optional[str] = None,
    generated_at: Optional[str] = None,
    current_total: Optional[float] = None,
) -> list[dict]:
    """Replay ``trades`` (ascending by ts) to build a bot equity curve.

    Uses only stored data + current prices. Each point is
    ``{"ts": iso, "value": cash + Σ holdings * last_known_price}``.
    A starting anchor point at ``start_bankroll`` is prepended and a final
    mark-to-market point at ``generated_at`` (equal to ``current_total`` when
    provided) is appended. Empty trades ⇒ a flat two-point line.

    Trades strictly before ``anchor_ts`` are dropped: an anchor marks a fresh
    epoch (e.g. a capital reset), and replaying pre-anchor trades on top of the
    new ``start_bankroll`` would both misstate cash and emit points that sort
    earlier than the anchor itself, breaking the ascending-ts line.
    """
    rows = list(trades)
    rows.sort(key=lambda t: t["ts_utc"])
    if anchor_ts is not None:
        rows = [t for t in rows if t["ts_utc"] >= anchor_ts]

    gen_ts = generated_at or _iso(_utcnow())
    first_ts = rows[0]["ts_utc"] if rows else gen_ts
    start_ts = anchor_ts or first_ts

    points: list[dict] = [{"ts": start_ts, "value": float(start_bankroll)}]

    cash = float(start_bankroll)
    holdings: dict[str, float] = {}
    last_price: dict[str, float] = {}
    for t in rows:
        side = t["side"]
        product = t["product"]
        base_size = float(t["base_size"])
        notional = float(t["notional"])
        fee = float(t["fee"] or 0.0)
        price = float(t["price"])
        if side == "buy":
            cash -= (notional + fee)
            holdings[product] = holdings.get(product, 0.0) + base_size
        else:  # sell
            cash += (notional - fee)
            holdings[product] = holdings.get(product, 0.0) - base_size
        last_price[product] = price
        value = cash + sum(
            holdings.get(p, 0.0) * current_prices.get(p, last_price[p]) for p in holdings
        )
        points.append({"ts": t["ts_utc"], "value": value})

    if current_total is not None:
        final_value = float(current_total)
    else:
        final_value = cash + sum(
            holdings.get(p, 0.0) * current_prices.get(p, last_price.get(p, 0.0))
            for p in holdings
        )
    points.append({"ts": gen_ts, "value": final_value})
    return points


# ---------------------------------------------------------------------------
# Activity feed (plan section 3.5).
# ---------------------------------------------------------------------------
def build_activity(ro_state: ReadOnlyState, limit: int = 100) -> list[dict]:
    """Reverse-chronological merged trade/intent feed.

    A filled intent already appears as its trade (same ``client_order_id``);
    show the trade and drop the duplicate intent, but KEEP non-filled/blocked
    intents so their ``reason`` stays visible.
    """
    items: list[dict] = []
    trade_coids: set[str] = set()

    for t in ro_state.recent_trades(limit):
        coid = t["client_order_id"]
        trade_coids.add(coid)
        intent = ro_state.intent_by_coid(coid)
        reason = intent["reason"] if intent and intent["reason"] else ""
        items.append({
            "kind": "trade",
            "ts_utc": t["ts_utc"],
            "product": t["product"],
            "side": t["side"],
            "base_size": float(t["base_size"]),
            "price": float(t["price"]),
            "notional": float(t["notional"]),
            "fee": float(t["fee"] or 0.0),
            "reason": reason,
            "status": "filled",
        })

    for i in ro_state.recent_intents(limit):
        coid = i["client_order_id"]
        status = i["status"] or "pending"
        # Drop the duplicate of an already-shown filled trade.
        if coid in trade_coids and status == "filled":
            continue
        # Keep blocked/rejected/pending intents (visibility is a core requirement).
        items.append({
            "kind": "intent",
            "ts_utc": i["ts_utc"],
            "product": i["product"],
            "side": i["side"],
            "requested_notional": (
                float(i["requested_notional"]) if i["requested_notional"] is not None else None
            ),
            "approved_notional": (
                float(i["approved_notional"]) if i["approved_notional"] is not None else None
            ),
            "risk_action": i["risk_action"],
            "reason": i["reason"] or "",
            "status": status,
        })

    items.sort(key=lambda x: x["ts_utc"], reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------
# Full payload assembly (plan section 3.1).
# ---------------------------------------------------------------------------
def build_payload(cfg, ro_state: ReadOnlyState, price_provider: PriceProvider) -> dict:
    """Assemble the full ``/api/data`` dict. Each sub-block is guarded so one
    bad field cannot 500 the whole endpoint (degrades to null)."""
    from .benchmark import Benchmark
    from .killswitch import KillSwitch
    from .portfolio import Portfolio
    from .risk import HALT_FLAG

    now = _utcnow()
    generated_at = _iso(now)
    mode = ro_state.get_mode("paper")

    payload: dict = {
        "generated_at": generated_at,
        "mode": mode,
        "status": None,
        "prices": {},
        "portfolio": None,
        "benchmark": None,
        "equity": [],
        "activity": [],
        "model": None,
        "refresh_sec": getattr(cfg, "dashboard_refresh_sec", 15),
    }

    # Prices (all three benchmark products, one fetch each per refresh window).
    prices, sources = price_provider.prices_and_sources(BENCH_PRODUCTS, ro_state)
    payload["prices"] = {
        p: {"price": prices[p], "source": sources[p]} for p in BENCH_PRODUCTS
    }
    price_source = price_provider.price_source(ro_state)

    # Status (all read-only sources — never call evaluate_portfolio_halt()).
    try:
        kill_engaged = KillSwitch(Path("state") / "KILL").is_engaged()
        halt_tripped = ro_state.is_cap_tripped(HALT_FLAG)
        halt_ts = ro_state.get_runtime(f"{HALT_FLAG}_ts")
        payload["status"] = {
            "kill_engaged": kill_engaged,
            "halt_tripped": halt_tripped,
            "halt_tripped_ts": halt_ts,
            "trades_24h": ro_state.trades_in_last_24h(now),
            "max_trades_24h": cfg.risk.max_trades_per_24h,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard: status block failed: %s", exc)

    portfolio = Portfolio(ro_state, cfg.paper_start_bankroll)
    benchmark = Benchmark(ro_state)

    # Portfolio + positions with unrealized P&L.
    try:
        cash = portfolio.cash
        positions = []
        for product, pos in ro_state.open_positions().items():
            base_size = float(pos["base_size"])
            avg_entry = float(pos["avg_entry"])
            price, psource = price_provider.get(product, ro_state)
            market_value = base_size * price
            unrealized_pnl = base_size * (price - avg_entry)
            unrealized_pnl_pct = (price - avg_entry) / avg_entry if avg_entry > 0 else None
            positions.append({
                "product": product,
                "base_size": base_size,
                "avg_entry": avg_entry,
                "price": price,
                "price_source": psource,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "opened_ts_utc": pos.get("opened_ts_utc"),
            })
        total_value = portfolio.total_value(price_source)
        total_fees_paid = sum(float(t["fee"] or 0.0) for t in ro_state.all_trades())
        payload["portfolio"] = {
            "cash": cash,
            "positions_value": total_value - cash,
            "total_value": total_value,
            "bankroll": portfolio.bankroll,
            "total_fees_paid": total_fees_paid,
            "positions": positions,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard: portfolio block failed: %s", exc)

    # Benchmark comparison (exactly like cli._print_status; epoch == mode).
    try:
        cmp = benchmark.compare(mode, portfolio, prices, price_source)
        payload["benchmark"] = {
            "actual_value": cmp["actual_value"],
            "actual_return_pct": cmp["actual_return_pct"],
            "baseline_value": cmp["baseline_value"],
            "baseline_return_pct": cmp["baseline_return_pct"],
            "delta_pct": cmp["delta_pct"],
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard: benchmark block failed: %s", exc)

    # Equity curve.
    try:
        anchor = ro_state.get_benchmark_anchor(mode)
        if anchor is not None:
            start_bankroll = float(anchor["start_bankroll"])
        elif mode == "live":
            raw = ro_state.get_runtime("go_live_bankroll")
            start_bankroll = float(raw) if raw is not None else cfg.paper_start_bankroll
        else:
            start_bankroll = cfg.paper_start_bankroll
        anchor_ts = anchor["anchor_ts_utc"] if anchor else None
        current_total = payload["portfolio"]["total_value"] if payload["portfolio"] else None
        all_trades = ro_state.all_trades()
        # Mark every traded product to its current price, not just the three
        # benchmark products in `prices` — otherwise non-benchmark holdings sit
        # frozen at their last trade price for every point except the final
        # one (which uses portfolio.total_value's live fetch), producing a
        # curve that's flat until a sudden jump at the very end.
        equity_prices = dict(prices)
        for product in {t["product"] for t in all_trades} - equity_prices.keys():
            equity_prices[product], _ = price_provider.get(product, ro_state)
        payload["equity"] = reconstruct_equity(
            all_trades, start_bankroll, equity_prices,
            anchor_ts=anchor_ts, generated_at=generated_at, current_total=current_total,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard: equity block failed: %s", exc)

    # Activity feed.
    try:
        payload["activity"] = build_activity(ro_state, limit=100)
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard: activity block failed: %s", exc)

    # Model metadata.
    try:
        m = ro_state.latest_model_meta()
        if m is not None:
            payload["model"] = {
                "trained_at_utc": m["trained_at_utc"],
                "n_samples": m["n_samples"],
                "holdout_logloss": m["holdout_logloss"],
                "promoted": bool(m["promoted"]),
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard: model block failed: %s", exc)

    return payload
