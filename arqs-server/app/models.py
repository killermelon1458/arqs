from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        Index("ix_nodes_key_id", "key_id", unique=True),
    )

    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(36), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        Index("ix_endpoints_node_id", "node_id"),
    )

    endpoint_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.node_id", ondelete="CASCADE"), nullable=False)
    endpoint_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (
        Index("ix_links_endpoint_a_id", "endpoint_a_id"),
        Index("ix_links_endpoint_b_id", "endpoint_b_id"),
    )

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    endpoint_a_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    endpoint_b_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class DirectedRoute(Base):
    __tablename__ = "directed_routes"
    __table_args__ = (
        Index("ix_routes_from_to_status", "from_endpoint_id", "to_endpoint_id", "status"),
    )

    route_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    from_endpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    to_endpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by_link_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("links.link_id", ondelete="SET NULL"), nullable=True)


class LinkCode(Base):
    __tablename__ = "link_codes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_link_codes_code"),
        Index("ix_link_codes_source_endpoint_id", "source_endpoint_id"),
    )

    link_code_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    source_endpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    requested_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class Packet(Base):
    __tablename__ = "packets"
    __table_args__ = (
        Index("ix_packets_to_endpoint_id", "to_endpoint_id"),
        Index("ix_packets_sender_node_id", "sender_node_id"),
    )

    packet_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sender_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.node_id", ondelete="CASCADE"), nullable=False)
    from_endpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    to_endpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("packet_id", name="uq_deliveries_packet_id"),
        Index("ix_deliveries_destination_node_state", "destination_node_id", "state"),
        Index("ix_deliveries_destination_endpoint_state", "destination_endpoint_id", "state"),
    )

    delivery_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    packet_id: Mapped[str] = mapped_column(String(36), ForeignKey("packets.packet_id", ondelete="CASCADE"), nullable=False)
    destination_node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.node_id", ondelete="CASCADE"), nullable=False)
    destination_endpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SendEvent(Base):
    __tablename__ = "send_events"
    __table_args__ = (
        Index("ix_send_events_node_created_at", "node_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), ForeignKey("nodes.node_id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
