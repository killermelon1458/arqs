from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import json
import logging
import random
import sqlite3
import threading
import uuid

from arqs_api import (
    ARQSClient,
    ARQSConnectionError,
    ARQSError,
    ARQSHTTPError,
    ARQSInsecureTransportError,
)

from .store import parse_datetime, to_iso, utc_now
from .types import OutboxEntry, RetryPolicy, SendResult


logger = logging.getLogger("arqs.appkit.outbox")


class SQLiteOutbox:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id TEXT PRIMARY KEY,
                    packet_id TEXT NOT NULL UNIQUE,
                    from_endpoint_id TEXT NOT NULL,
                    to_endpoint_id TEXT NOT NULL,
                    headers_json TEXT NOT NULL,
                    body TEXT,
                    data_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER,
                    expires_at TEXT,
                    last_error TEXT
                )
                """
            )

    def enqueue(
        self,
        *,
        from_endpoint_id: str,
        to_endpoint_id: str,
        headers: dict[str, Any],
        body: str | None,
        data: dict[str, Any],
        meta: dict[str, Any],
        retry_policy: RetryPolicy,
        max_attempts: int | None,
        expires_after_seconds: int | None,
        packet_id: str | None = None,
    ) -> OutboxEntry:
        created_at = utc_now()
        normalized_max_attempts = max_attempts
        normalized_expires_after_seconds = expires_after_seconds
        if retry_policy == "none":
            normalized_max_attempts = 1
        elif retry_policy == "forever":
            normalized_max_attempts = None
            normalized_expires_after_seconds = None
        expires_at = (
            None
            if normalized_expires_after_seconds in (None, 0)
            else created_at + timedelta(seconds=int(normalized_expires_after_seconds))
        )
        entry = OutboxEntry(
            outbox_id=str(uuid.uuid4()),
            packet_id=str(packet_id or uuid.uuid4()),
            from_endpoint_id=str(from_endpoint_id),
            to_endpoint_id=str(to_endpoint_id),
            headers=dict(headers),
            body=body,
            data=dict(data),
            meta=dict(meta),
            status="queued",
            created_at=created_at,
            updated_at=created_at,
            next_attempt_at=created_at,
            attempts=0,
            max_attempts=normalized_max_attempts,
            expires_at=expires_at,
            last_error=None,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbox (
                    outbox_id, packet_id, from_endpoint_id, to_endpoint_id,
                    headers_json, body, data_json, meta_json, status,
                    created_at, updated_at, next_attempt_at, attempts,
                    max_attempts, expires_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.outbox_id,
                    entry.packet_id,
                    entry.from_endpoint_id,
                    entry.to_endpoint_id,
                    json.dumps(entry.headers, sort_keys=True),
                    entry.body,
                    json.dumps(entry.data, sort_keys=True),
                    json.dumps(entry.meta, sort_keys=True),
                    entry.status,
                    to_iso(entry.created_at),
                    to_iso(entry.updated_at),
                    to_iso(entry.next_attempt_at),
                    entry.attempts,
                    entry.max_attempts,
                    None if entry.expires_at is None else to_iso(entry.expires_at),
                    entry.last_error,
                ),
            )
        return entry

    def flush_due(self, client: ARQSClient, *, limit: int = 100) -> list[SendResult]:
        entries = self._select_due(limit=limit)
        return [self._flush_entry(client, entry) for entry in entries]

    def flush_packet(self, client: ARQSClient, packet_id: str) -> SendResult:
        entry = self.get_by_packet_id(packet_id)
        if entry is None:
            return SendResult(packet_id=str(packet_id), delivery_mode="queued", status="missing")
        return self._flush_entry(client, entry)

    def list_dead_letters(self, *, limit: int = 100) -> list[OutboxEntry]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, packet_id, from_endpoint_id, to_endpoint_id,
                       headers_json, body, data_json, meta_json, status,
                       created_at, updated_at, next_attempt_at, attempts,
                       max_attempts, expires_at, last_error
                FROM outbox
                WHERE status = 'dead_letter'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def get_by_packet_id(self, packet_id: str) -> OutboxEntry | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT outbox_id, packet_id, from_endpoint_id, to_endpoint_id,
                       headers_json, body, data_json, meta_json, status,
                       created_at, updated_at, next_attempt_at, attempts,
                       max_attempts, expires_at, last_error
                FROM outbox
                WHERE packet_id = ?
                """,
                (str(packet_id),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def _select_due(self, *, limit: int) -> list[OutboxEntry]:
        now_iso = to_iso(utc_now())
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, packet_id, from_endpoint_id, to_endpoint_id,
                       headers_json, body, data_json, meta_json, status,
                       created_at, updated_at, next_attempt_at, attempts,
                       max_attempts, expires_at, last_error
                FROM outbox
                WHERE status IN ('queued', 'failed')
                  AND next_attempt_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now_iso, int(limit)),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def _flush_entry(self, client: ARQSClient, entry: OutboxEntry) -> SendResult:
        now = utc_now()
        if entry.expires_at is not None and entry.expires_at <= now:
            self._mark_dead_letter(entry.outbox_id, "packet expired before delivery attempt")
            return SendResult(
                packet_id=entry.packet_id,
                delivery_mode="queued",
                status="dead_letter",
                outbox_id=entry.outbox_id,
                attempts=entry.attempts,
                error="packet expired before delivery attempt",
            )

        self._set_status(entry.outbox_id, "sending", attempts=entry.attempts, last_error=entry.last_error)
        try:
            result = client.send_packet(
                from_endpoint_id=entry.from_endpoint_id,
                to_endpoint_id=entry.to_endpoint_id,
                body=entry.body,
                data=entry.data,
                headers=entry.headers,
                meta=entry.meta,
                packet_id=entry.packet_id,
            )
        except Exception as exc:
            return self._handle_send_failure(entry, exc)

        self._delete(entry.outbox_id)
        return SendResult(
            packet_id=str(result.packet_id),
            delivery_mode="queued",
            status=result.result,
            delivery_id=None if result.delivery_id is None else str(result.delivery_id),
            expires_at=result.expires_at,
            outbox_id=entry.outbox_id,
            attempts=entry.attempts + 1,
        )

    def _handle_send_failure(self, entry: OutboxEntry, exc: Exception) -> SendResult:
        classification, message = classify_send_error(exc)
        next_attempts = entry.attempts + 1
        should_retry = classification == "retryable" and self._can_retry(entry, next_attempts)
        if should_retry:
            next_attempt_at = utc_now() + timedelta(seconds=_backoff_seconds(next_attempts))
            self._set_status(
                entry.outbox_id,
                "queued",
                attempts=next_attempts,
                next_attempt_at=next_attempt_at,
                last_error=message,
            )
            logger.warning("outbox retry scheduled for packet %s: %s", entry.packet_id, message)
            return SendResult(
                packet_id=entry.packet_id,
                delivery_mode="queued",
                status="queued",
                outbox_id=entry.outbox_id,
                attempts=next_attempts,
                error=message,
            )

        self._mark_dead_letter(entry.outbox_id, message, attempts=next_attempts)
        logger.error("outbox dead-lettered packet %s: %s", entry.packet_id, message)
        return SendResult(
            packet_id=entry.packet_id,
            delivery_mode="queued",
            status="dead_letter",
            outbox_id=entry.outbox_id,
            attempts=next_attempts,
            error=message,
        )

    def _can_retry(self, entry: OutboxEntry, attempts: int) -> bool:
        if entry.max_attempts is not None and attempts >= entry.max_attempts:
            return False
        if entry.expires_at is not None and entry.expires_at <= utc_now():
            return False
        return True

    def _set_status(
        self,
        outbox_id: str,
        status: str,
        *,
        attempts: int,
        next_attempt_at: datetime | None = None,
        last_error: str | None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET status = ?,
                    updated_at = ?,
                    next_attempt_at = ?,
                    attempts = ?,
                    last_error = ?
                WHERE outbox_id = ?
                """,
                (
                    str(status),
                    to_iso(utc_now()),
                    to_iso(next_attempt_at or utc_now()),
                    int(attempts),
                    last_error,
                    str(outbox_id),
                ),
            )

    def _mark_dead_letter(self, outbox_id: str, last_error: str, *, attempts: int | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET status = 'dead_letter',
                    updated_at = ?,
                    next_attempt_at = ?,
                    attempts = COALESCE(?, attempts),
                    last_error = ?
                WHERE outbox_id = ?
                """,
                (
                    to_iso(utc_now()),
                    to_iso(utc_now()),
                    attempts,
                    last_error,
                    str(outbox_id),
                ),
            )

    def _delete(self, outbox_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM outbox WHERE outbox_id = ?", (str(outbox_id),))

    def _row_to_entry(self, row: tuple[Any, ...]) -> OutboxEntry:
        return OutboxEntry(
            outbox_id=str(row[0]),
            packet_id=str(row[1]),
            from_endpoint_id=str(row[2]),
            to_endpoint_id=str(row[3]),
            headers=json.loads(row[4]),
            body=row[5],
            data=json.loads(row[6]),
            meta=json.loads(row[7]),
            status=str(row[8]),
            created_at=parse_datetime(str(row[9])) or utc_now(),
            updated_at=parse_datetime(str(row[10])) or utc_now(),
            next_attempt_at=parse_datetime(str(row[11])) or utc_now(),
            attempts=int(row[12]),
            max_attempts=None if row[13] is None else int(row[13]),
            expires_at=parse_datetime(row[14]),
            last_error=None if row[15] in (None, "") else str(row[15]),
        )


def classify_send_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ARQSHTTPError):
        detail = exc.detail
        message = f"http {exc.status_code}: {detail}"
        if exc.status_code in {429, 500, 502, 503, 504}:
            return "retryable", message
        return "permanent", message
    if isinstance(exc, ARQSConnectionError):
        return "retryable", str(exc)
    if isinstance(exc, ARQSInsecureTransportError):
        return "permanent", str(exc)
    if isinstance(exc, (ValueError, TypeError)):
        return "permanent", str(exc)
    if isinstance(exc, ARQSError):
        return "permanent", str(exc)
    return "retryable", str(exc)


def _backoff_seconds(attempts: int) -> int:
    if attempts <= 1:
        return 0
    if attempts == 2:
        return 10
    if attempts == 3:
        return 30
    if attempts == 4:
        return 60
    base = min(300, 2 ** min(attempts, 8))
    return base + random.randint(0, 5)


__all__ = ["SQLiteOutbox", "classify_send_error"]
