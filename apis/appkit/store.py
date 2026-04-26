from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json
import sqlite3
import uuid

from arqs_api import NodeIdentity

from .types import Contact, ReceivedPacket, RuntimePaths


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if value in (None, ""):
        return None
    normalized = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class RuntimeStore:
    def __init__(self, app_name: str, *, state_root: str | Path | None = None) -> None:
        normalized_name = str(app_name).strip()
        if not normalized_name:
            raise ValueError("app_name is required")
        root = Path(state_root).expanduser() if state_root is not None else Path.home() / ".arqs"
        state_dir = root / normalized_name
        self.app_name = normalized_name
        self.paths = RuntimePaths(
            state_dir=state_dir,
            config_path=state_dir / "config.json",
            identity_path=state_dir / "identity.json",
            contacts_path=state_dir / "contacts.json",
            outbox_path=state_dir / "outbox.sqlite3",
            inbox_path=state_dir / "inbox.sqlite3",
            log_path=state_dir / "appkit.log",
        )

    def ensure_dirs(self) -> RuntimePaths:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        return self.paths

    def default_config(self) -> dict[str, Any]:
        return {
            "ack_policy": "after_handler_success",
            "app_name": self.app_name,
            "base_url": "",
            "default_contact": "",
            "default_endpoint_kind": "message",
            "default_endpoint_name": "default",
            "delivery_mode": "queued",
            "expires_after_seconds": 86400,
            "max_attempts": 20,
            "node_name": self.app_name,
            "poll_limit": 100,
            "poll_wait_seconds": 20,
            "retry_policy": "until_expired",
            "transport_policy": "prefer_https",
            "transport_preferences": {},
        }

    def load_config(self) -> dict[str, Any]:
        self.ensure_dirs()
        config = self.default_config()
        config.update(read_json(self.paths.config_path, default={}))
        config["app_name"] = self.app_name
        return config

    def save_config(self, config: dict[str, Any]) -> Path:
        merged = self.default_config()
        merged.update(dict(config))
        merged["app_name"] = self.app_name
        return write_json(self.paths.config_path, merged)

    def load_identity(self) -> NodeIdentity | None:
        if not self.paths.identity_path.exists():
            return None
        return NodeIdentity.load(self.paths.identity_path)

    def save_identity(self, identity: NodeIdentity) -> Path:
        self.ensure_dirs()
        return identity.save(self.paths.identity_path)

    def save_contact_book(self, payload: dict[str, Any]) -> Path:
        return write_json(self.paths.contacts_path, payload)


class ContactBook:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def _load_raw(self) -> dict[str, Any]:
        return read_json(self.store.paths.contacts_path, default={})

    def _save_raw(self, raw: dict[str, Any]) -> Path:
        return self.store.save_contact_book(raw)

    def list_contacts(self) -> list[Contact]:
        return [self._parse_contact(label, value) for label, value in self._load_raw().items()]

    def get(self, label: str) -> Contact | None:
        raw = self._load_raw().get(str(label))
        if raw is None:
            return None
        return self._parse_contact(str(label), raw)

    def resolve_by_remote_endpoint(self, remote_endpoint_id: str) -> Contact | None:
        remote = str(remote_endpoint_id)
        for contact in self.list_contacts():
            if contact.remote_endpoint_id == remote:
                return contact
        return None

    def upsert(
        self,
        *,
        label: str,
        local_endpoint_id: str,
        remote_endpoint_id: str,
        link_id: str | None = None,
        status: str = "active",
    ) -> Contact:
        normalized_label = str(label).strip()
        if not normalized_label:
            raise ValueError("contact label is required")
        raw = self._load_raw()
        now = utc_now()
        existing = raw.get(normalized_label)
        contact_id = str(existing.get("contact_id")) if isinstance(existing, dict) and existing.get("contact_id") else str(uuid.uuid4())
        created_at = (
            str(existing.get("created_at"))
            if isinstance(existing, dict) and existing.get("created_at")
            else to_iso(now)
        )
        payload = {
            "contact_id": contact_id,
            "label": normalized_label,
            "local_endpoint_id": str(local_endpoint_id),
            "remote_endpoint_id": str(remote_endpoint_id),
            "link_id": None if link_id in (None, "") else str(link_id),
            "status": str(status),
            "created_at": created_at,
            "updated_at": to_iso(now),
        }
        raw[normalized_label] = payload
        self._save_raw(raw)
        return self._parse_contact(normalized_label, payload)

    def _parse_contact(self, label: str, payload: dict[str, Any]) -> Contact:
        return Contact(
            contact_id=str(payload["contact_id"]),
            label=str(payload.get("label") or label),
            local_endpoint_id=str(payload["local_endpoint_id"]),
            remote_endpoint_id=str(payload["remote_endpoint_id"]),
            link_id=None if payload.get("link_id") in (None, "") else str(payload.get("link_id")),
            status=str(payload.get("status") or "active"),
            created_at=parse_datetime(str(payload["created_at"])) or utc_now(),
            updated_at=parse_datetime(str(payload["updated_at"])) or utc_now(),
        )


class InboxStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_packets (
                    delivery_id TEXT PRIMARY KEY,
                    packet_id TEXT NOT NULL,
                    from_endpoint_id TEXT NOT NULL,
                    to_endpoint_id TEXT NOT NULL,
                    arqs_type TEXT,
                    headers_json TEXT NOT NULL,
                    body TEXT,
                    text TEXT,
                    data_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    decode_errors_json TEXT NOT NULL
                )
                """
            )

    def store_packet(self, packet: ReceivedPacket) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO inbox_packets (
                    delivery_id,
                    packet_id,
                    from_endpoint_id,
                    to_endpoint_id,
                    arqs_type,
                    headers_json,
                    body,
                    text,
                    data_json,
                    meta_json,
                    created_at,
                    received_at,
                    decode_errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet.delivery_id,
                    packet.packet_id,
                    packet.from_endpoint_id,
                    packet.to_endpoint_id,
                    packet.arqs_type,
                    json.dumps(packet.headers, sort_keys=True),
                    packet.body,
                    packet.text,
                    json.dumps(packet.data, sort_keys=True),
                    json.dumps(packet.meta, sort_keys=True),
                    to_iso(packet.created_at),
                    to_iso(packet.received_at),
                    json.dumps(list(packet.decode_errors)),
                ),
            )

    def list_recent(self, *, limit: int = 50) -> list[ReceivedPacket]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT delivery_id, packet_id, from_endpoint_id, to_endpoint_id, arqs_type,
                       headers_json, body, text, data_json, meta_json, created_at,
                       received_at, decode_errors_json
                FROM inbox_packets
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        packets: list[ReceivedPacket] = []
        for row in rows:
            packets.append(
                ReceivedPacket(
                    delivery_id=str(row[0]),
                    packet_id=str(row[1]),
                    from_endpoint_id=str(row[2]),
                    to_endpoint_id=str(row[3]),
                    arqs_type=None if row[4] in (None, "") else str(row[4]),
                    headers=json.loads(row[5]),
                    body=row[6],
                    text=row[7],
                    data=json.loads(row[8]),
                    meta=json.loads(row[9]),
                    created_at=parse_datetime(str(row[10])) or utc_now(),
                    received_at=parse_datetime(str(row[11])) or utc_now(),
                    decode_errors=tuple(json.loads(row[12])),
                )
            )
        return packets


def replace_identity_default_endpoint(identity: NodeIdentity, endpoint_id: str) -> NodeIdentity:
    return replace(identity, default_endpoint_id=uuid.UUID(str(endpoint_id)))


__all__ = [
    "ContactBook",
    "InboxStore",
    "RuntimeStore",
    "parse_datetime",
    "read_json",
    "replace_identity_default_endpoint",
    "to_iso",
    "utc_now",
    "write_json",
]
