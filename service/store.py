"""SQLite 持久化。

引擎采用写穿（write-through）策略：每次状态变更在同一事务内
更新业务表并追加事件日志，提交后才对外可见。因此：
- 服务重启后从本模块完整恢复，已锁定的价格不会丢失；
- events 表是只增不删的审计日志，可按序号重放全部历史。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    institution TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (institution, quote_id)
);
CREATE TABLE IF NOT EXISTS institutions (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Store:
    """单连接写穿存储。所有写操作必须在 engine 的锁内调用。"""

    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._tx_lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """显式事务：一批写操作要么全部落库，要么全部回滚。"""
        with self._tx_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # ---- 通用读写 ----

    def upsert(self, table: str, key_cols: dict[str, Any], payload: dict) -> None:
        cols = list(key_cols) + ["payload"]
        values = list(key_cols.values()) + [json.dumps(payload, ensure_ascii=False)]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols)
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT DO UPDATE SET {updates}",
            values,
        )

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def append_event(self, seq: int, type_: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO events (seq, type, payload) VALUES (?, ?, ?)",
            (seq, type_, json.dumps(payload, ensure_ascii=False)),
        )

    # ---- 启动恢复 ----

    def load_table(self, table: str) -> list[dict]:
        rows = self._conn.execute(f"SELECT payload FROM {table}").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def load_events(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seq, type, payload FROM events ORDER BY seq"
        ).fetchall()
        return [
            {"seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload"])}
            for r in rows
        ]
