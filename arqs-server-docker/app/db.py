from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from .config import settings

logger = logging.getLogger(__name__)


def utc_now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    if settings.enable_sql_trace:
        conn.set_trace_callback(logger.debug)
    return conn


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_conn() as conn:
        return row_to_dict(conn.execute(query, params).fetchone())


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def json_loads(text: str | None) -> Any:
    if not text:
        return {}
    return json.loads(text)


def init_db() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS actors (
            actor_id TEXT PRIMARY KEY,
            actor_type TEXT NOT NULL,
            api_key_hash TEXT NOT NULL UNIQUE,
            capabilities_json TEXT NOT NULL,
            adapter_type TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            display_name TEXT,
            created_at INTEGER NOT NULL,
            revoked_at INTEGER
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            owner_actor_id TEXT NOT NULL UNIQUE,
            client_name TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(owner_actor_id) REFERENCES actors(actor_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS targets (
            target_id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            config_json TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(type, external_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS routes (
            route_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(client_id, target_id),
            FOREIGN KEY(client_id) REFERENCES clients(client_id) ON DELETE CASCADE,
            FOREIGN KEY(target_id) REFERENCES targets(target_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS link_codes (
            link_code TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            client_id TEXT,
            adapter_type TEXT,
            capabilities_json TEXT NOT NULL,
            created_by_actor_id TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            FOREIGN KEY(client_id) REFERENCES clients(client_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_actor_id) REFERENCES actors(actor_id) ON DELETE SET NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS packets (
            packet_id TEXT PRIMARY KEY,
            origin_actor_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            body_text TEXT,
            data_json TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            idempotency_expires_at INTEGER NOT NULL,
            FOREIGN KEY(origin_actor_id) REFERENCES actors(actor_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS inbox_entries (
            inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT NOT NULL,
            packet_id TEXT NOT NULL,
            delivery_kind TEXT NOT NULL,
            delivery_meta_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            ack_status TEXT,
            created_at INTEGER NOT NULL,
            delivered_at INTEGER,
            acked_at INTEGER,
            UNIQUE(actor_id, packet_id, delivery_kind),
            FOREIGN KEY(actor_id) REFERENCES actors(actor_id) ON DELETE CASCADE,
            FOREIGN KEY(packet_id) REFERENCES packets(packet_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            bucket_key TEXT NOT NULL,
            window_started_at INTEGER NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY(bucket_key, window_started_at)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_inbox_actor_status ON inbox_entries(actor_id, status, created_at);",
        "CREATE INDEX IF NOT EXISTS idx_packets_expiry ON packets(idempotency_expires_at);",
        "CREATE INDEX IF NOT EXISTS idx_link_codes_expiry ON link_codes(expires_at, used_at);",
        "CREATE INDEX IF NOT EXISTS idx_actors_type_state ON actors(actor_type, adapter_type, state);",
    ]
    with get_conn() as conn:
        for statement in statements:
            conn.executescript(statement)
    logger.info("database initialized at %s", settings.database_path)


def cleanup_expired_state() -> None:
    now = utc_now()
    cutoff = now - max(settings.register_rate_limit_window_seconds * 3, 3600)
    with get_conn() as conn:
        conn.execute("DELETE FROM link_codes WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM packets WHERE idempotency_expires_at < ?", (now,))
        conn.execute("DELETE FROM rate_limits WHERE window_started_at < ?", (cutoff,))
