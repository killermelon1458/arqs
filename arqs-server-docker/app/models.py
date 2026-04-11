from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ActorType = Literal["client", "adapter"]
AckStatus = Literal["received", "handled", "rejected", "failed", "unsupported", "invalid"]


class PacketIn(BaseModel):
    version: int = Field(default=1, ge=1)
    packet_id: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=128)
    timestamp: int = Field(ge=0)
    headers: dict[str, Any] = Field(default_factory=dict)
    body: str = Field(default="", max_length=10000)
    data: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload_present(self) -> "PacketIn":
        if not self.body and not self.data:
            raise ValueError("packet must include body or non-empty data")
        return self


class PacketAckIn(BaseModel):
    packet_id: str = Field(min_length=1, max_length=128)
    status: AckStatus


class PacketAckOut(BaseModel):
    status: str = "ok"
    duplicate: bool = False
    packets_waiting: bool = False
    waiting_count: int = 0


class InboxPacketOut(BaseModel):
    packet_id: str
    client_id: str
    timestamp: int
    headers: dict[str, Any] = Field(default_factory=dict)
    body: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    delivery_kind: str
    delivery: dict[str, Any] = Field(default_factory=dict)


class InboxResponse(BaseModel):
    packets: list[InboxPacketOut]


class LinkRequestOut(BaseModel):
    link_code: str
    expires_in: int


class LinkCompleteIn(BaseModel):
    link_code: str = Field(min_length=4, max_length=32)
    adapter: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    filters: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class RegisterOut(BaseModel):
    actor_id: str
    client_id: str
    api_key: str


class RotateKeyOut(BaseModel):
    actor_id: str
    client_id: str
    api_key: str


class RegenerateClientOut(BaseModel):
    actor_id: str
    old_client_id: str
    client_id: str
    routes_cleared: bool = True
    relink_required: bool = True


class DeleteIdentityOut(BaseModel):
    status: str = "deleted"
    actor_id: str
    client_id: str


class HealthOut(BaseModel):
    status: str
    app: str
    version: str
    timestamp: int


class StatsOut(BaseModel):
    actors: int
    active_clients: int
    active_adapters: int
    revoked_adapters: int
    targets: int
    routes: int
    packets: int
    inbox_pending: int
    inbox_acked: int
    active_link_codes: int


class AdapterProvisionRequestIn(BaseModel):
    adapter_type: str = Field(min_length=1, max_length=64)
    display_name: str | None = None


class AdapterProvisionLinkOut(BaseModel):
    link_code: str
    adapter_type: str
    expires_in: int


class AdapterProvisionCompleteIn(BaseModel):
    link_code: str = Field(min_length=4, max_length=32)
    adapter_type: str = Field(min_length=1, max_length=64)
    display_name: str | None = None


class AdapterProvisionCompleteOut(BaseModel):
    actor_id: str
    actor_type: Literal["adapter"] = "adapter"
    adapter_type: str
    api_key: str


class AdapterRevokeOut(BaseModel):
    status: str = "revoked"
    actor_id: str
    adapter_type: str
