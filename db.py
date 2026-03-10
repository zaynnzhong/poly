"""
SQLite 数据层

- WAL 模式，支持并发读
- 主库: markets, relations, violations, sim_trades, price_ticks, check_logs
- 快照库: orderbook_snapshots 按天分片
- 批量写入缓冲
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    token_id TEXT PRIMARY KEY,
    slug TEXT,
    question TEXT,
    category TEXT,
    active INTEGER DEFAULT 1,
    last_price REAL,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_a TEXT REFERENCES markets(token_id),
    market_b TEXT REFERENCES markets(token_id),
    relation_type TEXT,
    description TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    source TEXT
);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER REFERENCES relations(id),
    price_a REAL,
    price_b REAL,
    violation_amount REAL,
    signal TEXT,
    detected_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    violation_id INTEGER REFERENCES violations(id),
    leg1_token TEXT,
    leg1_side TEXT,
    leg1_price REAL,
    leg1_size REAL,
    leg2_token TEXT,
    leg2_side TEXT,
    leg2_price REAL,
    leg2_size REAL,
    expected_profit REAL,
    status TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT,
    best_bid REAL,
    best_ask REAL,
    mid_price REAL,
    spread REAL,
    bid_depth REAL,
    ask_depth REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS check_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER,
    price_a REAL,
    price_b REAL,
    violated INTEGER,
    violation_amount REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, main_db: str = "data/main.db", snapshots_dir: str = "data/snapshots/"):
        self.main_db_path = main_db
        self.snapshots_dir = Path(snapshots_dir)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        # 批量写入缓冲
        self._tick_buffer: list[tuple] = []
        self._snapshot_buffer: list[tuple] = []
        self._check_buffer: list[tuple] = []
        self._buffer_lock = threading.Lock()
        self._last_flush = time.monotonic()

    def init(self) -> None:
        Path(self.main_db_path).parent.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.main_db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, params)
            self._conn.commit()

    # ── 批量缓冲写入 ──

    def buffer_tick(self, token_id: str, best_bid: float, best_ask: float,
                    mid: float, spread: float, bid_depth: float, ask_depth: float) -> None:
        should_flush = False
        with self._buffer_lock:
            self._tick_buffer.append((
                token_id, best_bid, best_ask, mid, spread, bid_depth, ask_depth,
                datetime.now(timezone.utc).isoformat()
            ))
            should_flush = self._should_flush()
        if should_flush:
            self.flush()

    def buffer_snapshot(self, token_id: str, side: str, price: float, size: float) -> None:
        should_flush = False
        with self._buffer_lock:
            self._snapshot_buffer.append((
                token_id, side, price, size, datetime.now(timezone.utc).isoformat()
            ))
            should_flush = self._should_flush()
        if should_flush:
            self.flush()

    def buffer_check(self, relation_id: int, price_a: float, price_b: float,
                     violated: bool, amount: float) -> None:
        should_flush = False
        with self._buffer_lock:
            self._check_buffer.append((
                relation_id, price_a, price_b, int(violated), amount,
                datetime.now(timezone.utc).isoformat()
            ))
            should_flush = self._should_flush()
        if should_flush:
            self.flush()

    def _should_flush(self) -> bool:
        total = len(self._tick_buffer) + len(self._snapshot_buffer) + len(self._check_buffer)
        elapsed = time.monotonic() - self._last_flush
        return total >= 100 or elapsed >= 0.2

    def flush(self) -> None:
        with self._buffer_lock:
            ticks = self._tick_buffer[:]
            snapshots = self._snapshot_buffer[:]
            checks = self._check_buffer[:]
            self._tick_buffer.clear()
            self._snapshot_buffer.clear()
            self._check_buffer.clear()
            self._last_flush = time.monotonic()

        if ticks:
            self.executemany(
                "INSERT INTO price_ticks (token_id,best_bid,best_ask,mid_price,spread,bid_depth,ask_depth,timestamp) VALUES (?,?,?,?,?,?,?,?)",
                ticks,
            )
        if checks:
            self.executemany(
                "INSERT INTO check_logs (relation_id,price_a,price_b,violated,violation_amount,timestamp) VALUES (?,?,?,?,?,?)",
                checks,
            )
        if snapshots:
            self._write_snapshots(snapshots)

    def _write_snapshots(self, rows: list[tuple]) -> None:
        today = date.today().isoformat().replace("-", "_")
        db_path = self.snapshots_dir / f"snapshots_{today}.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SNAPSHOT_SCHEMA)
        conn.executemany(
            "INSERT INTO orderbook_snapshots (token_id,side,price,size,timestamp) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    # ── 便捷写入 ──

    def insert_market(self, token_id: str, slug: str, question: str, category: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO markets (token_id,slug,question,category,last_updated) VALUES (?,?,?,?,?)",
            (token_id, slug, question, category, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def insert_relation(self, market_a: str, market_b: str, relation_type: str,
                        description: str, source: str = "llm_auto") -> int:
        cur = self.execute(
            "INSERT INTO relations (market_a,market_b,relation_type,description,source) VALUES (?,?,?,?,?)",
            (market_a, market_b, relation_type, description, source),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_relation_status(self, relation_id: int, status: str) -> None:
        self.execute("UPDATE relations SET status=? WHERE id=?", (status, relation_id))
        self._conn.commit()

    def get_approved_relations(self) -> list[dict]:
        cur = self.execute(
            "SELECT id,market_a,market_b,relation_type,description FROM relations WHERE status='approved'"
        )
        return [
            {"id": r[0], "market_a": r[1], "market_b": r[2], "relation_type": r[3], "description": r[4]}
            for r in cur.fetchall()
        ]

    def insert_violation(self, relation_id: int, price_a: float, price_b: float,
                         violation_amount: float, signal: str) -> int:
        cur = self.execute(
            "INSERT INTO violations (relation_id,price_a,price_b,violation_amount,signal) VALUES (?,?,?,?,?)",
            (relation_id, price_a, price_b, violation_amount, signal),
        )
        self._conn.commit()
        return cur.lastrowid

    def insert_sim_trade(self, violation_id: int, leg1_token: str, leg1_side: str,
                         leg1_price: float, leg1_size: float, leg2_token: str,
                         leg2_side: str, leg2_price: float, leg2_size: float,
                         expected_profit: float, status: str) -> int:
        cur = self.execute(
            "INSERT INTO sim_trades (violation_id,leg1_token,leg1_side,leg1_price,leg1_size,"
            "leg2_token,leg2_side,leg2_price,leg2_size,expected_profit,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (violation_id, leg1_token, leg1_side, leg1_price, leg1_size,
             leg2_token, leg2_side, leg2_price, leg2_size, expected_profit, status),
        )
        self._conn.commit()
        return cur.lastrowid

    def close(self) -> None:
        self.flush()
        if self._conn:
            self._conn.close()
