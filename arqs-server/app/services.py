from __future__ import annotations

import json
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from .admin_services import get_runtime_settings
from .config import AppConfig
from .models import Delivery, DirectedRoute, Endpoint, Link, LinkCode, Node, Packet, SendEvent

ALPHANUM = string.ascii_uppercase + string.digits


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_uuid() -> str:
    return str(uuid.uuid4())


def generate_link_code() -> str:
    return "".join(secrets.choice(ALPHANUM) for _ in range(6))


def payload_size_bytes(*, headers: dict, body: str | None, data: dict, meta: dict) -> int:
    envelope = {
        "headers": headers,
        "body": body,
        "data": data,
        "meta": meta,
    }
    return len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def ensure_node_owns_endpoint(db: Session, node_id: str, endpoint_id: str) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint not found")
    if endpoint.node_id != node_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="endpoint not owned by node")
    if endpoint.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="endpoint not active")
    return endpoint


def ensure_node_active(node: Node) -> None:
    if node.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"node {node.status}")


def active_packet_clause(*, now: datetime | None = None):
    current = now or utcnow()
    return or_(Packet.expires_at.is_(None), Packet.expires_at > current)


def is_packet_expired(packet: Packet, *, now: datetime | None = None) -> bool:
    current = now or utcnow()
    return packet.expires_at is not None and packet.expires_at <= current


def active_link_code_clause(*, now: datetime | None = None):
    current = now or utcnow()
    return and_(LinkCode.status == "active", LinkCode.expires_at > current)


def effective_link_code_status(code: LinkCode, *, now: datetime | None = None) -> str:
    current = now or utcnow()
    if code.status == "active" and code.expires_at <= current:
        return "expired"
    return code.status


def current_storage_usage_bytes(cfg: AppConfig) -> int:
    db_path = Path(cfg.storage.db_path)
    candidates = [
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
        db_path.with_name(f"{db_path.name}-journal"),
    ]
    total = 0
    for candidate in candidates:
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def cleanup_expired(db: Session, cfg: AppConfig) -> dict[str, int]:
    now = utcnow()
    runtime_settings = get_runtime_settings(db)

    expired_codes = int(
        db.execute(
            update(LinkCode)
            .where(LinkCode.status == "active", LinkCode.expires_at <= now)
            .values(status="expired")
        ).rowcount
        or 0
    )

    expired_packets = int(
        db.execute(
            delete(Packet).where(Packet.expires_at.is_not(None), Packet.expires_at <= now)
        ).rowcount
        or 0
    )

    window_floor = now - timedelta(seconds=int(runtime_settings["send_window_seconds"]) * 2)
    old_events = int(
        db.execute(
            delete(SendEvent).where(SendEvent.created_at < window_floor)
        ).rowcount
        or 0
    )

    return {
        "expired_link_codes": expired_codes,
        "expired_packets": expired_packets,
        "pruned_send_events": old_events,
    }

def packet_expiry(now: datetime, cfg: AppConfig, ttl_seconds: int | None) -> datetime | None:
    if cfg.retention.no_expiry and ttl_seconds is None:
        return None
    ttl = ttl_seconds if ttl_seconds is not None else cfg.retention.default_packet_ttl_seconds
    return now + timedelta(seconds=ttl)


def enforce_send_rate_limit(db: Session, cfg: AppConfig, node_id: str) -> None:
    now = utcnow()
    runtime_settings = get_runtime_settings(db)
    floor = now - timedelta(seconds=int(runtime_settings["send_window_seconds"]))
    count = db.scalar(
        select(func.count()).select_from(SendEvent).where(SendEvent.node_id == node_id, SendEvent.created_at >= floor)
    )
    if count >= int(runtime_settings["max_sends_per_window"]):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="send rate limit exceeded")
    db.add(SendEvent(event_id=new_uuid(), node_id=node_id, created_at=now))


def enforce_queue_limits(db: Session, cfg: AppConfig, dest_endpoint_id: str, dest_node_id: str, packet_bytes: int) -> None:
    now = utcnow()
    runtime_settings = get_runtime_settings(db)
    active_packets = active_packet_clause(now=now)

    endpoint_counts = db.execute(
        select(func.count(Delivery.delivery_id), func.coalesce(func.sum(Packet.payload_bytes), 0))
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(Delivery.destination_endpoint_id == dest_endpoint_id, active_packets)
    ).one()
    endpoint_packets, endpoint_bytes = int(endpoint_counts[0]), int(endpoint_counts[1] or 0)
    if endpoint_packets >= int(runtime_settings["max_queued_packets_per_endpoint"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="destination endpoint queue packet cap reached")
    if endpoint_bytes + packet_bytes > int(runtime_settings["max_queued_bytes_per_endpoint"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="destination endpoint queue byte cap reached")

    node_bytes = int(
        db.scalar(
            select(func.coalesce(func.sum(Packet.payload_bytes), 0))
            .select_from(Delivery)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(Delivery.destination_node_id == dest_node_id, active_packets)
        )
        or 0
    )
    if node_bytes + packet_bytes > int(runtime_settings["max_queued_bytes_per_node"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="destination node queue byte cap reached")

    total_counts = db.execute(
        select(func.count(Delivery.delivery_id), func.coalesce(func.sum(Packet.payload_bytes), 0))
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(active_packets)
    ).one()
    total_packets, total_bytes = int(total_counts[0]), int(total_counts[1] or 0)
    if total_packets >= int(runtime_settings["max_total_queued_packets"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="global queue packet cap reached")
    if total_bytes + packet_bytes > int(runtime_settings["max_total_queued_bytes"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="global queue byte cap reached")

    storage_bytes = current_storage_usage_bytes(cfg)
    max_storage_bytes = int(runtime_settings["max_storage_bytes"])
    if storage_bytes >= max_storage_bytes or storage_bytes + packet_bytes > max_storage_bytes:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="storage cap reached")


def active_route_exists(db: Session, from_endpoint_id: str, to_endpoint_id: str) -> bool:
    route = db.scalar(
        select(DirectedRoute.route_id).where(
            DirectedRoute.from_endpoint_id == from_endpoint_id,
            DirectedRoute.to_endpoint_id == to_endpoint_id,
            DirectedRoute.status == "active",
        )
    )
    return route is not None


def resolve_redeem_routes(source_endpoint_id: str, destination_endpoint_id: str, mode: str) -> list[tuple[str, str]]:
    if mode == "bidirectional":
        return [(source_endpoint_id, destination_endpoint_id), (destination_endpoint_id, source_endpoint_id)]
    if mode == "a_to_b":
        return [(source_endpoint_id, destination_endpoint_id)]
    if mode == "b_to_a":
        return [(destination_endpoint_id, source_endpoint_id)]
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid link mode")


def exact_active_link_exists(db: Session, source_endpoint_id: str, destination_endpoint_id: str, mode: str) -> bool:
    if mode == "bidirectional":
        existing = db.scalar(
            select(Link.link_id).where(
                Link.status == "active",
                Link.mode == "bidirectional",
                or_(
                    (Link.endpoint_a_id == source_endpoint_id) & (Link.endpoint_b_id == destination_endpoint_id),
                    (Link.endpoint_a_id == destination_endpoint_id) & (Link.endpoint_b_id == source_endpoint_id),
                ),
            )
        )
        return existing is not None
    existing = db.scalar(
        select(Link.link_id).where(
            Link.status == "active",
            Link.mode == mode,
            Link.endpoint_a_id == source_endpoint_id,
            Link.endpoint_b_id == destination_endpoint_id,
        )
    )
    return existing is not None


def packet_matches(existing: Packet, *, sender_node_id: str, from_endpoint_id: str, to_endpoint_id: str, headers: dict, body: str | None, data: dict, meta: dict, version: int) -> bool:
    return (
        existing.sender_node_id == sender_node_id
        and existing.from_endpoint_id == from_endpoint_id
        and existing.to_endpoint_id == to_endpoint_id
        and existing.headers == headers
        and existing.body == body
        and existing.data == data
        and existing.meta == meta
        and existing.version == version
    )
