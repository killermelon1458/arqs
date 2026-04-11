from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .auth import require_actor, require_admin, require_capability
from .config import settings
from .db import cleanup_expired_state, fetch_one, init_db, utc_now
from .models import (
    AdapterProvisionCompleteIn,
    AdapterProvisionCompleteOut,
    AdapterProvisionLinkOut,
    AdapterProvisionRequestIn,
    AdapterRevokeOut,
    DeleteIdentityOut,
    HealthOut,
    InboxResponse,
    LinkCompleteIn,
    LinkRequestOut,
    PacketAckIn,
    PacketAckOut,
    PacketIn,
    RegisterOut,
    RegenerateClientOut,
    RotateKeyOut,
    StatsOut,
)
from .services import ActorService, LinkService, PacketService, RateLimitService
from .source import resolve_source_identifier

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title=settings.app_name, version=settings.app_version)
actor_service = ActorService()
packet_service = PacketService()
link_service = LinkService()
rate_limit_service = RateLimitService()


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.middleware("http")
async def cleanup_middleware(request: Request, call_next):
    cleanup_expired_state()
    response = await call_next(request)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.getLogger(__name__).exception("unhandled exception during %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok", app=settings.app_name, version=settings.app_version, timestamp=utc_now())


@app.get("/stats", response_model=StatsOut)
def stats(actor: dict = Depends(require_actor)) -> StatsOut:
    require_capability(actor, "stats.read")

    def count(query: str, params: tuple = ()) -> int:
        row = fetch_one(query, params)
        return int(row["count"]) if row else 0

    return StatsOut(
        actors=count("SELECT COUNT(*) AS count FROM actors"),
        active_clients=count("SELECT COUNT(*) AS count FROM actors WHERE actor_type = 'client' AND state = 'active'"),
        active_adapters=count("SELECT COUNT(*) AS count FROM actors WHERE actor_type = 'adapter' AND state = 'active'"),
        revoked_adapters=count("SELECT COUNT(*) AS count FROM actors WHERE actor_type = 'adapter' AND state = 'revoked'"),
        targets=count("SELECT COUNT(*) AS count FROM targets"),
        routes=count("SELECT COUNT(*) AS count FROM routes"),
        packets=count("SELECT COUNT(*) AS count FROM packets"),
        inbox_pending=count("SELECT COUNT(*) AS count FROM inbox_entries WHERE status != 'acked'"),
        inbox_acked=count("SELECT COUNT(*) AS count FROM inbox_entries WHERE status = 'acked'"),
        active_link_codes=count("SELECT COUNT(*) AS count FROM link_codes WHERE used_at IS NULL AND expires_at >= ?", (utc_now(),)),
    )


@app.post("/register", response_model=RegisterOut)
def register(request: Request) -> RegisterOut:
    if not settings.public_registration_enabled:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="public registration disabled")
    source_id = resolve_source_identifier(request)
    rate_limit_service.check_and_increment_register(f"register:{source_id}")
    result = actor_service.register_client()
    return RegisterOut(**result)


@app.post("/packets", response_model=PacketAckOut)
def post_packet(packet: PacketIn, actor: dict = Depends(require_actor)) -> PacketAckOut:
    require_capability(actor, "packet.send")
    duplicate, waiting_count = packet_service.record_packet(packet, actor)
    return PacketAckOut(status="ok", duplicate=duplicate, packets_waiting=waiting_count > 0, waiting_count=waiting_count)


@app.get("/inbox", response_model=InboxResponse)
def inbox(wait: int = Query(default=0, ge=0, le=60), actor: dict = Depends(require_actor)) -> InboxResponse:
    require_capability(actor, "packet.receive")
    packets = packet_service.get_inbox(actor, wait_seconds=wait)
    return InboxResponse(packets=packets)


@app.post("/packet_ack", response_model=PacketAckOut)
def packet_ack(request: PacketAckIn, actor: dict = Depends(require_actor)) -> PacketAckOut:
    require_capability(actor, "packet.ack")
    waiting_count = packet_service.ack_packet(actor, request.packet_id, request.status)
    return PacketAckOut(status="ok", duplicate=False, packets_waiting=waiting_count > 0, waiting_count=waiting_count)


@app.post("/link_request", response_model=LinkRequestOut)
def link_request(actor: dict = Depends(require_actor)) -> LinkRequestOut:
    require_capability(actor, "link.request")
    code, expires_in = link_service.create_client_link_code(actor)
    return LinkRequestOut(link_code=code, expires_in=expires_in)


@app.post("/link_complete")
def link_complete(request: LinkCompleteIn, actor: dict = Depends(require_actor)) -> dict:
    require_capability(actor, "link.complete")
    return link_service.complete_link(actor, request)


@app.post("/identity/rotate-key", response_model=RotateKeyOut)
def rotate_key(actor: dict = Depends(require_actor)) -> RotateKeyOut:
    require_capability(actor, "self.rotate_api_key")
    return RotateKeyOut(**actor_service.rotate_api_key(actor))


@app.post("/identity/regenerate-client", response_model=RegenerateClientOut)
def regenerate_client(actor: dict = Depends(require_actor)) -> RegenerateClientOut:
    require_capability(actor, "self.regenerate_client_id")
    return RegenerateClientOut(**actor_service.regenerate_client_id(actor))


@app.delete("/identity", response_model=DeleteIdentityOut)
def delete_identity(actor: dict = Depends(require_actor)) -> DeleteIdentityOut:
    require_capability(actor, "self.delete_identity")
    return DeleteIdentityOut(**actor_service.delete_identity(actor))


@app.post("/admin/adapter-provision/request", response_model=AdapterProvisionLinkOut)
def request_adapter_provision_link(payload: AdapterProvisionRequestIn, _: None = Depends(require_admin)) -> AdapterProvisionLinkOut:
    result = actor_service.issue_adapter_provision_link(payload.adapter_type)
    return AdapterProvisionLinkOut(**result)


@app.post("/adapter-register", response_model=AdapterProvisionCompleteOut)
def adapter_register(payload: AdapterProvisionCompleteIn) -> AdapterProvisionCompleteOut:
    result = actor_service.provision_adapter_from_link(payload.link_code, payload.adapter_type, payload.display_name)
    return AdapterProvisionCompleteOut(**result)


@app.post("/admin/adapters/{actor_id}/revoke", response_model=AdapterRevokeOut)
def revoke_adapter(actor_id: str, _: None = Depends(require_admin)) -> AdapterRevokeOut:
    return AdapterRevokeOut(**actor_service.revoke_adapter(actor_id))
