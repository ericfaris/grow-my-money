"""SQLite persistence layer (WAL). The bot's single source of truth across
restarts: mode, bankroll, cap-trip flags, trades, positions, model meta, and the
write-once benchmark anchor. See the plan's section 5 for the schema.

All timestamps are stored as ISO-8601 UTC strings. The rolling-24h trade count
uses a trailing ``now - 24h`` query on stored fill times, never a calendar day.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
  id INTEGER PRIMARY KEY,
  client_order_id TEXT UNIQUE NOT NULL,
  ts_utc TEXT NOT NULL,
  product TEXT NOT NULL,
  side TEXT NOT NULL,
  mode TEXT NOT NULL,
  requested_notional REAL,
  risk_action TEXT,
  approved_notional REAL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY,
  client_order_id TEXT NOT NULL REFERENCES intents(client_order_id),
  ts_utc TEXT NOT NULL,
  product TEXT NOT NULL,
  side TEXT NOT NULL,
  mode TEXT NOT NULL,
  base_size REAL NOT NULL,
  price REAL NOT NULL,
  notional REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
  product TEXT PRIMARY KEY,
  base_size REAL NOT NULL,
  avg_entry REAL NOT NULL,
  opened_ts_utc TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY,
  product TEXT NOT NULL,
  entry_ts_utc TEXT NOT NULL,
  features_json TEXT NOT NULL,
  label INTEGER NOT NULL,
  realized_pnl REAL
);

CREATE TABLE IF NOT EXISTS benchmark (
  epoch TEXT PRIMARY KEY,
  anchor_ts_utc TEXT NOT NULL,
  start_bankroll REAL NOT NULL,
  btc_price REAL NOT NULL,
  eth_price REAL NOT NULL,
  sol_price REAL NOT NULL,
  btc_units REAL NOT NULL,
  eth_units REAL NOT NULL,
  sol_units REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS model_meta (
  id INTEGER PRIMARY KEY,
  trained_at_utc TEXT,
  n_samples INTEGER,
  holdout_logloss REAL,
  promoted INTEGER
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class State:
    """Thin typed wrapper over a single SQLite connection."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # some filesystems (e.g. certain mounts) reject WAL; fall back
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_db()

    def init_db(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- runtime kv --------------------------------------------------------
    def get_runtime(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM runtime WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_runtime(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO runtime(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, None if value is None else str(value)),
        )
        self.conn.commit()

    # -- mode --------------------------------------------------------------
    def get_mode(self, default: str = "paper") -> str:
        return self.get_runtime("mode", default) or default

    def set_mode(self, mode: str) -> None:
        self.set_runtime("mode", mode)

    # -- cap-trip flags ----------------------------------------------------
    def set_cap_trip(self, name: str, tripped: bool, ts: Optional[str] = None) -> None:
        self.set_runtime(name, "1" if tripped else "0")
        if tripped:
            self.set_runtime(f"{name}_ts", ts or iso(utcnow()))

    def is_cap_tripped(self, name: str) -> bool:
        return self.get_runtime(name, "0") == "1"

    # -- intents & trades --------------------------------------------------
    def record_intent(
        self,
        client_order_id: str,
        product: str,
        side: str,
        mode: str,
        requested_notional: float,
        ts_utc: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO intents(client_order_id,ts_utc,product,side,mode,"
            "requested_notional,status) VALUES(?,?,?,?,?,?, 'pending')",
            (client_order_id, ts_utc or iso(utcnow()), product, side, mode, requested_notional),
        )
        self.conn.commit()

    def update_intent_risk(
        self, client_order_id: str, risk_action: str, approved_notional: float, reason: str
    ) -> None:
        self.conn.execute(
            "UPDATE intents SET risk_action=?, approved_notional=?, reason=? "
            "WHERE client_order_id=?",
            (risk_action, approved_notional, reason, client_order_id),
        )
        self.conn.commit()

    def set_intent_status(self, client_order_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE intents SET status=? WHERE client_order_id=?",
            (status, client_order_id),
        )
        self.conn.commit()

    def pending_intents(self, mode: Optional[str] = None) -> list[sqlite3.Row]:
        if mode:
            return self.conn.execute(
                "SELECT * FROM intents WHERE status='pending' AND mode=?", (mode,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM intents WHERE status='pending'"
        ).fetchall()

    def record_fill(
        self,
        client_order_id: str,
        product: str,
        side: str,
        mode: str,
        base_size: float,
        price: float,
        notional: float,
        fee: float = 0.0,
        ts_utc: Optional[str] = None,
    ) -> None:
        """Record a fill and update the position book in one transaction."""
        ts = ts_utc or iso(utcnow())
        cur = self.conn
        cur.execute(
            "INSERT INTO trades(client_order_id,ts_utc,product,side,mode,"
            "base_size,price,notional,fee) VALUES(?,?,?,?,?,?,?,?,?)",
            (client_order_id, ts, product, side, mode, base_size, price, notional, fee),
        )
        cur.execute(
            "UPDATE intents SET status='filled' WHERE client_order_id=?",
            (client_order_id,),
        )
        self._apply_fill_to_position(product, side, base_size, price, ts)
        cur.commit()

    def _apply_fill_to_position(
        self, product: str, side: str, base_size: float, price: float, ts: str
    ) -> None:
        row = self.conn.execute(
            "SELECT base_size, avg_entry FROM positions WHERE product=?", (product,)
        ).fetchone()
        cur_size = row["base_size"] if row else 0.0
        cur_avg = row["avg_entry"] if row else 0.0
        if side == "buy":
            new_size = cur_size + base_size
            new_avg = (
                (cur_size * cur_avg + base_size * price) / new_size if new_size else price
            )
        else:  # sell
            new_size = cur_size - base_size
            new_avg = cur_avg  # avg entry unchanged on a partial sell
        if new_size <= 1e-12:
            self.conn.execute("DELETE FROM positions WHERE product=?", (product,))
        elif row:
            self.conn.execute(
                "UPDATE positions SET base_size=?, avg_entry=? WHERE product=?",
                (new_size, new_avg, product),
            )
        else:
            self.conn.execute(
                "INSERT INTO positions(product,base_size,avg_entry,opened_ts_utc) "
                "VALUES(?,?,?,?)",
                (product, new_size, new_avg, ts),
            )

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
        now = now or utcnow()
        cutoff = iso(now - timedelta(hours=24))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE ts_utc >= ?", (cutoff,)
        ).fetchone()
        return int(row["n"])

    def all_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades ORDER BY ts_utc").fetchall()

    def trades_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades WHERE ts_utc >= ? ORDER BY ts_utc", (iso(since),)
        ).fetchall()

    # -- outcomes / model --------------------------------------------------
    def record_outcome(
        self, product: str, entry_ts_utc: str, features: dict, label: int, realized_pnl: float
    ) -> None:
        self.conn.execute(
            "INSERT INTO outcomes(product,entry_ts_utc,features_json,label,realized_pnl) "
            "VALUES(?,?,?,?,?)",
            (product, entry_ts_utc, json.dumps(features), int(label), realized_pnl),
        )
        self.conn.commit()

    def all_outcomes(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM outcomes ORDER BY entry_ts_utc"
        ).fetchall()
        return [
            {
                "product": r["product"],
                "entry_ts_utc": r["entry_ts_utc"],
                "features": json.loads(r["features_json"]),
                "label": r["label"],
                "realized_pnl": r["realized_pnl"],
            }
            for r in rows
        ]

    def outcome_count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"]
        )

    def record_model_meta(
        self, trained_at_utc: str, n_samples: int, holdout_logloss: float, promoted: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO model_meta(trained_at_utc,n_samples,holdout_logloss,promoted) "
            "VALUES(?,?,?,?)",
            (trained_at_utc, n_samples, holdout_logloss, 1 if promoted else 0),
        )
        self.conn.commit()

    def latest_model_meta(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM model_meta ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # -- benchmark anchor (write-once per epoch) ---------------------------
    def get_benchmark_anchor(self, epoch: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM benchmark WHERE epoch=?", (epoch,)
        ).fetchone()
        return dict(row) if row else None

    def create_benchmark_anchor_if_absent(
        self,
        epoch: str,
        start_bankroll: float,
        btc_price: float,
        eth_price: float,
        sol_price: float,
        anchor_ts_utc: Optional[str] = None,
    ) -> dict:
        """INSERT-if-absent ONLY. Never updates an existing anchor.

        Returns the anchor in force for the epoch (existing or newly created).
        """
        existing = self.get_benchmark_anchor(epoch)
        if existing is not None:
            return existing
        third = start_bankroll / 3.0
        btc_units = third / btc_price
        eth_units = third / eth_price
        sol_units = third / sol_price
        ts = anchor_ts_utc or iso(utcnow())
        # OR IGNORE guards against a race: if another writer inserted first, keep theirs.
        self.conn.execute(
            "INSERT OR IGNORE INTO benchmark(epoch,anchor_ts_utc,start_bankroll,"
            "btc_price,eth_price,sol_price,btc_units,eth_units,sol_units) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (epoch, ts, start_bankroll, btc_price, eth_price, sol_price,
             btc_units, eth_units, sol_units),
        )
        self.conn.commit()
        return self.get_benchmark_anchor(epoch)  # type: ignore[return-value]
