from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


LinkMode = Literal["bidirectional", "a_to_b", "b_to_a"]
AckStatus = Literal["received", "handled", "rejected", "failed", "invalid", "unsupported"]


class RegisterRequest(BaseModel):
    node_name: str | None = Field(default=None, max_length=255)


class RegisterResponse(BaseModel):
    node_id: UUID
    api_key: str
    default_endpoint_id: UUID


class RotateKeyResponse(BaseModel):
    node_id: UUID
    api_key: str

class DeleteIdentityResponse(BaseModel):
    deleted: bool
    node_id: UUID
    endpoints_deleted: int
    links_deleted: int
    routes_deleted: int
    link_codes_deleted: int
    packets_deleted: int
    deliveries_deleted: int
    send_events_deleted: int

class EndpointCreateRequest(BaseModel):
    endpoint_name: str | None = Field(default=None, max_length=255)
    kind: str | None = Field(default=None, max_length=255)
    meta: dict[str, Any] | None = None


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    endpoint_id: UUID
    node_id: UUID
    endpoint_name: str | None
    kind: str | None
    meta: dict[str, Any] | None
    created_at: datetime
    status: str


class LinkCodeRequest(BaseModel):
    source_endpoint_id: UUID
    requested_mode: LinkMode = "bidirectional"


class LinkCodeResponse(BaseModel):
    link_code_id: UUID
    code: str
    source_endpoint_id: UUID
    requested_mode: LinkMode
    created_at: datetime
    expires_at: datetime
    status: str


class LinkRedeemRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)
    destination_endpoint_id: UUID

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.isalnum():
            raise ValueError("code must be alphanumeric")
        return value


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    link_id: UUID
    endpoint_a_id: UUID
    endpoint_b_id: UUID
    mode: str
    created_at: datetime
    status: str


class PacketSendRequest(BaseModel):
    version: int = 1
    packet_id: UUID
    from_endpoint_id: UUID
    to_endpoint_id: UUID
    headers: dict[str, Any] = Field(default_factory=dict)
    body: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_payload(self):
        if self.version != 1:
            raise ValueError("version must be 1")
        if not self.body and not self.data:
            raise ValueError("at least one of body or non-empty data must be present")
        return self


class PacketSendResponse(BaseModel):
    result: Literal["accepted", "duplicate"]
    packet_id: UUID
    delivery_id: UUID | None = None
    expires_at: datetime | None = None


class DeliveryPacketOut(BaseModel):
    packet_id: UUID
    version: int
    from_endpoint_id: UUID
    to_endpoint_id: UUID
    headers: dict[str, Any]
    body: str | None
    data: dict[str, Any]
    meta: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None


class DeliveryOut(BaseModel):
    delivery_id: UUID
    destination_endpoint_id: UUID
    queued_at: datetime
    state: str
    last_attempt_at: datetime | None
    packet: DeliveryPacketOut


class InboxResponse(BaseModel):
    deliveries: list[DeliveryOut]


class PacketAckRequest(BaseModel):
    delivery_id: UUID | None = None
    packet_id: UUID | None = None
    status: AckStatus

    @model_validator(mode="after")
    def validate_reference(self):
        if not self.delivery_id and not self.packet_id:
            raise ValueError("delivery_id or packet_id is required")
        return self


class PacketAckResponse(BaseModel):
    acked: bool
    packet_id: UUID
    status: AckStatus


class HealthResponse(BaseModel):
    status: str
    app: str
    db_path: str
    time: datetime


class StatsResponse(BaseModel):
    nodes_total: int
    endpoints_total: int
    active_links_total: int
    queued_packets_total: int
    queued_bytes_total: int
    link_codes_active_total: int
