from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal


DeliveryMode = Literal["direct", "queued", "background"]
RetryPolicy = Literal["none", "bounded", "until_expired", "forever"]
AckPolicy = Literal["after_handler_success", "after_store", "always", "manual"]


@dataclass(frozen=True)
class Contact:
    contact_id: str
    label: str
    local_endpoint_id: str
    remote_endpoint_id: str
    link_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SendResult:
    packet_id: str
    delivery_mode: DeliveryMode
    status: str
    delivery_id: str | None = None
    expires_at: datetime | None = None
    outbox_id: str | None = None
    attempts: int = 0
    error: str | None = None


@dataclass(frozen=True)
class ReceivedPacket:
    delivery_id: str
    packet_id: str
    from_endpoint_id: str
    to_endpoint_id: str
    arqs_type: str | None
    headers: dict[str, Any]
    body: str | None
    text: str | None
    data: dict[str, Any]
    meta: dict[str, Any]
    created_at: datetime
    received_at: datetime
    decode_errors: tuple[str, ...] = ()


@dataclass
class CommandContext:
    app: Any
    client: Any
    contact: Contact | None
    delivery: Any
    packet: ReceivedPacket
    ack: Callable[[str], None]
    reply: Callable[..., SendResult | None]


@dataclass(frozen=True)
class CommandResponse:
    ok: bool
    command_id: str
    correlation_id: str
    result: Any | None = None
    error_type: str | None = None
    error_message: str | None = None
    received_at: datetime | None = None
    packet: ReceivedPacket | None = None


@dataclass(frozen=True)
class NotificationPayload:
    notification_id: str
    title: str
    body: str
    level: str
    created_at: datetime
    source: str
    host: str
    script: str | None = None
    tags: tuple[str, ...] = ()
    priority: str | None = None
    dedupe_key: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransportResolution:
    base_url: str
    transport_policy: str
    allow_local_http_auth: bool
    classification: str
    host_key: str
    preference_updates: dict[str, str]


@dataclass(frozen=True)
class OutboxEntry:
    outbox_id: str
    packet_id: str
    from_endpoint_id: str
    to_endpoint_id: str
    headers: dict[str, Any]
    body: str | None
    data: dict[str, Any]
    meta: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime
    attempts: int
    max_attempts: int | None
    expires_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class RuntimePaths:
    state_dir: Path
    config_path: Path
    identity_path: Path
    contacts_path: Path
    outbox_path: Path
    inbox_path: Path
    log_path: Path


__all__ = [
    "AckPolicy",
    "CommandContext",
    "CommandResponse",
    "Contact",
    "DeliveryMode",
    "NotificationPayload",
    "OutboxEntry",
    "ReceivedPacket",
    "RetryPolicy",
    "RuntimePaths",
    "SendResult",
    "TransportResolution",
]
