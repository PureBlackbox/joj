"""SQLite storage: duplicate protection, audit trail and tiny key/value state.

Everything the relay must remember across restarts lives in one file: data/relay.db
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    key         TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    trade_id    TEXT,
    event       TEXT,
    leg         TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_received ON signals(received_at);
CREATE TABLE IF NOT EXISTS state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)  # autocommit
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    # ---- signals ----------------------------------------------------------------------
    def register(self, key: str, trade_id: str, event: str, leg: str, payload: dict) -> bool:
        """Insert a new signal. Returns False if this key was already seen (a duplicate)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO signals(key, received_at, trade_id, event, leg, status, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, utc_now_iso(), trade_id, event, leg, "RECEIVED", json.dumps(payload)[:2000]),
            )
            return cur.rowcount == 1

    def set_status(self, key: str, status: str, detail: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE signals SET status=?, detail=? WHERE key=?", (status, detail[:500], key))

    def get_status(self, key: str) -> Optional[tuple]:
        with self._lock:
            row = self._db.execute("SELECT status, detail FROM signals WHERE key=?", (key,)).fetchone()
        return tuple(row) if row else None

    def count_opens_since(self, iso_ts: str) -> int:
        """How many BUY/SELL/ADD orders were executed (or dry-run) since a UTC timestamp."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM signals WHERE event IN ('BUY','SELL','ADD') "
                "AND status IN ('DONE','DRY_RUN') AND received_at >= ?",
                (iso_ts,),
            ).fetchone()
        return int(row[0])

    def recent(self, n: int = 20) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT received_at, event, trade_id, leg, status, detail FROM signals "
                "ORDER BY received_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(zip(("received_at", "event", "trade_id", "leg", "status", "detail"), r)) for r in rows]

    # ---- state ---------------------------------------------------------------------------
    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM state WHERE k=?", (key,))
            else:
                self._db.execute("INSERT INTO state(k, v) VALUES (?,?) "
                                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    def close(self) -> None:
        with self._lock:
            self._db.close()
